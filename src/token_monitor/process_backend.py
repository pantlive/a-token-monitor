"""跨平台进程发现后端：Linux 用 ``/proc``，macOS 用系统自带的 ``ps`` 与 ``lsof``。

会话适配器、Codex 进程扫描和异常流量监控都只依赖这里产出的统一观测结果：

* :class:`ObservedProcess` —— 进程树、命令行、启动时间与工作目录；
* :func:`scan_agents` —— 带“打开的会话文件”的 agent 进程（Linux fd 符号链接 /
  macOS ``lsof``）；
* :func:`netlink_available` —— 字节级 TCP 统计是否可用（``INET_DIAG`` 只有 Linux 提供）。

macOS 的 ``ps`` / ``lsof`` 结果带秒级 TTL 缓存：一次 Dashboard 刷新里多个 provider
适配器会重复请求同样的进程信息，缓存可以避免反复 fork 子进程。
"""

from __future__ import annotations

import ipaddress
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注，避免与 agents 循环导入
    from .agents import RunningAgent

PROC_ROOT = Path("/proc")

# 中文注释：缓存只用于合并同一轮刷新里的重复查询，宁可短一点。
_CACHE_TTL_SECONDS = 2.0
_PS_TIMEOUT_SECONDS = 5.0
_LSOF_TIMEOUT_SECONDS = 15.0
_MAX_QUERY_PIDS = 256

_CACHE: dict[str, tuple[float, object]] = {}


@dataclass(frozen=True)
class ObservedProcess:
    """一个进程的统一观测结果，两个平台字段一致。"""

    pid: int
    ppid: int
    command: tuple[str, ...]
    comm: str
    start_token: str
    cwd: Path | None
    open_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ObservedConnection:
    """``lsof`` 观测到的一条 TCP 连接（macOS 拿不到字节数）。"""

    local_port: int | None
    remote_ip: str | None
    remote_port: int | None
    listening: bool = False

    @property
    def remote(self) -> str | None:
        """返回 ``ip:port`` 形式的对端地址。"""

        if self.remote_ip is None or self.remote_port is None:
            return None
        return f"{self.remote_ip}:{self.remote_port}"

    @property
    def loopback(self) -> bool:
        """判断对端是否为本机回环。"""

        if self.remote_ip is None:
            return False
        try:
            return ipaddress.ip_address(self.remote_ip).is_loopback
        except ValueError:
            return self.remote_ip.startswith("127.") or self.remote_ip in {
                ":1",
                "::1",
                "localhost",
            }


# ---------------------------------------------------------------- 后端选择


def select_backend(proc_root: Path | None = None) -> str:
    """选择进程后端。

    显式传入 ``proc_root`` 时一律按 Linux ``/proc`` 语义处理——测试和容器都会
    直接给出合成目录；只有使用默认值且 ``/proc`` 不存在时才切到 macOS 后端。
    """

    if proc_root is not None:
        return "proc"
    if PROC_ROOT.is_dir():
        return "proc"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return "proc"


def backend_name(proc_root: Path | None = None) -> str:
    """返回当前生效的后端名称，供日志与 Dashboard 说明使用。"""

    return select_backend(proc_root)


def process_root(proc_root: Path | None = None) -> Path:
    """把可选的 ``proc_root`` 规范化成具体路径。"""

    return Path(proc_root) if proc_root is not None else PROC_ROOT


def netlink_available() -> bool:
    """内核是否提供 netlink ``INET_DIAG``（只有 Linux 有）。"""

    return hasattr(socket, "AF_NETLINK")


def netlink_reason() -> str | None:
    """返回字节级流量统计不可用的原因；可用时返回 ``None``。"""

    if netlink_available():
        return None
    if sys.platform == "darwin":
        return (
            "macOS 没有 netlink（INET_DIAG），只能显示 agent 进程与远端连接，"
            "无法统计 TCP 外发字节"
        )
    if sys.platform == "win32":
        return (
            "Windows 没有 netlink（INET_DIAG），只能显示 agent 进程与远端连接，"
            "无法统计 TCP 外发字节"
        )
    return "当前平台没有 netlink（INET_DIAG），无法统计 TCP 外发字节"


# ---------------------------------------------------------------- 缓存


def reset_cache() -> None:
    """清空进程查询缓存（测试用）。"""

    _CACHE.clear()


def _cached(
    key: str,
    factory: Callable[[], object],
    now: float | None = None,
) -> object:
    moment = time.monotonic() if now is None else now
    found = _CACHE.get(key)
    if found is not None and moment - found[0] < _CACHE_TTL_SECONDS:
        return found[1]
    value = factory()
    _CACHE[key] = (moment, value)
    return value


# ---------------------------------------------------------------- 进程列表


def scan_processes(
    proc_root: Path | None = None,
    now: float | None = None,
) -> dict[int, ObservedProcess]:
    """返回本机进程的统一观测结果，键为 PID。"""

    backend = select_backend(proc_root)
    if backend == "macos":
        cached = _cached("macos:ps", _scan_macos_process_table, now)
        processes = dict(cached)  # type: ignore[arg-type]
        return _attach_macos_details(processes)
    if backend == "windows":
        return scan_windows_processes(now)
    return _scan_proc_processes(process_root(proc_root))


def _scan_proc_processes(proc_root: Path) -> dict[int, ObservedProcess]:
    """读取 ``/proc`` 下可读进程的身份，不打开 socket 描述符。"""

    processes: dict[int, ObservedProcess] = {}
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return processes
    for directory in entries:
        if not directory.name.isdigit():
            continue
        try:
            pid = int(directory.name)
            command = _read_command(directory / "cmdline")
            comm = _read_text(directory / "comm")
            ppid, start_token = _read_stat(directory / "stat")
            cwd = _read_cwd(directory / "cwd")
        except (OSError, ValueError):
            continue
        processes[pid] = ObservedProcess(
            pid=pid,
            ppid=ppid,
            comm=comm,
            command=command,
            start_token=start_token,
            cwd=cwd,
        )
    return processes


def _scan_macos_process_table() -> dict[int, ObservedProcess]:
    """用 ``ps`` 读取进程树，启动时间用于避免 PID 复用。"""

    stdout = _run(
        ["ps", "-axo", "pid=,ppid=,lstart=,command="],
        _PS_TIMEOUT_SECONDS,
    )
    processes: dict[int, ObservedProcess] = {}
    for line in stdout.splitlines():
        fields = line.split(None, 7)
        if len(fields) < 8:
            continue
        try:
            pid = int(fields[0])
            ppid = int(fields[1])
        except ValueError:
            continue
        command = _split_command(fields[7])
        processes[pid] = ObservedProcess(
            pid=pid,
            ppid=ppid,
            command=command,
            comm=Path(command[0]).name if command else "",
            # lstart 固定五段：Mon Jan  1 00:00:00 2026
            start_token=f"macos:{' '.join(fields[2:7])}",
            cwd=None,
            open_paths=(),
        )
    return processes


def _attach_macos_details(
    processes: dict[int, ObservedProcess],
) -> dict[int, ObservedProcess]:
    """给进程补上 ``lsof`` 提供的工作目录与打开文件。"""

    if not processes:
        return processes
    details = _macos_lsof_details(tuple(sorted(processes)))
    if not details:
        return processes
    enriched: dict[int, ObservedProcess] = {}
    for pid, info in processes.items():
        detail = details.get(pid)
        if not detail:
            enriched[pid] = info
            continue
        enriched[pid] = ObservedProcess(
            pid=info.pid,
            ppid=info.ppid,
            command=info.command,
            comm=info.comm,
            start_token=info.start_token,
            cwd=detail.get("cwd") or info.cwd,
            open_paths=tuple(detail.get("files", ())),
        )
    return enriched


# ---------------------------------------------------------------- agent 进程


def scan_agents(
    proc_root: Path | None = None,
    products: Sequence[str] | None = None,
    ignore_pids: Sequence[int] | None = None,
    session_roots: Sequence[Path] | None = None,
) -> tuple[RunningAgent, ...]:
    """按产品过滤的 agent 进程；各平台返回同样的 :class:`RunningAgent`。

    ``session_roots`` 只在 Windows 上使用：没有 ``/proc`` 时需要用
    Restart Manager 反查「哪个进程持有这些目录下的会话文件」。
    """

    backend = select_backend(proc_root)
    if backend == "macos":
        return scan_macos_agents(products=products, ignore_pids=ignore_pids)
    if backend == "windows":
        return scan_windows_agents(
            products=products,
            ignore_pids=ignore_pids,
            session_roots=session_roots,
        )
    return _scan_proc_agents(process_root(proc_root), products, ignore_pids)


def _scan_proc_agents(
    proc_root: Path,
    products: Sequence[str] | None,
    ignore_pids: Sequence[int] | None,
) -> tuple[RunningAgent, ...]:
    """Linux：从 ``/proc`` 目录、命令行和 fd 符号链接识别 agent 进程。"""

    from .agents import RunningAgent, identify_agent

    wanted = set(products) if products is not None else None
    ignored = set(ignore_pids or ())
    found: list[RunningAgent] = []
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return ()
    for directory in entries:
        if not directory.name.isdigit():
            continue
        try:
            pid = int(directory.name)
        except ValueError:
            continue
        if pid in ignored:
            continue
        command = _read_command(directory / "cmdline")
        comm = _read_text(directory / "comm")
        product = identify_agent(command, comm)
        if product is None:
            continue
        if wanted is not None and product not in wanted:
            continue
        found.append(
            RunningAgent(
                pid=pid,
                product=product,
                start_token=_read_start_token(directory / "stat"),
                cwd=_read_cwd(directory / "cwd"),
                command=command,
                open_paths=_read_open_paths(directory / "fd"),
            )
        )
    return tuple(found)


def scan_macos_agents(
    products: Sequence[str] | None = None,
    ignore_pids: Sequence[int] | None = None,
    now: float | None = None,
) -> tuple[RunningAgent, ...]:
    """macOS：``ps`` 找进程，``lsof`` 找工作目录和打开的会话文件。"""

    from .agents import RunningAgent, identify_agent

    wanted = set(products) if products is not None else None
    ignored = set(ignore_pids or ())
    key = (
        "macos:agents:"
        f"{','.join(sorted(wanted)) if wanted is not None else '*'}:"
        f"{','.join(str(pid) for pid in sorted(ignored))}"
    )

    def collect() -> tuple[RunningAgent, ...]:
        table = _cached("macos:ps", _scan_macos_process_table, now)
        assert isinstance(table, Mapping)
        candidates: dict[int, tuple[str, ObservedProcess]] = {}
        for pid, info in table.items():  # type: ignore[union-attr]
            if pid in ignored:
                continue
            product = identify_agent(info.command, info.comm)
            if product is None:
                continue
            if wanted is not None and product not in wanted:
                continue
            candidates[pid] = (product, info)
        details = _macos_lsof_details(tuple(sorted(candidates)), now)
        agents: list[RunningAgent] = []
        for pid, (product, info) in candidates.items():
            detail = details.get(pid, {})
            agents.append(
                RunningAgent(
                    pid=pid,
                    product=product,
                    start_token=info.start_token,
                    cwd=detail.get("cwd") or info.cwd,
                    command=info.command,
                    open_paths=tuple(detail.get("files", ())),
                )
            )
        return tuple(agents)

    cached = _cached(key, collect, now)
    return tuple(cached)  # type: ignore[arg-type]


# ---------------------------------------------------------------- lsof


def _macos_lsof_details(
    pids: Sequence[int],
    now: float | None = None,
) -> dict[int, dict[str, object]]:
    """一次 ``lsof`` 调用取回这些进程的工作目录与打开文件。"""

    if not pids:
        return {}
    selected = tuple(pids[:_MAX_QUERY_PIDS])
    key = f"macos:lsof:{','.join(str(pid) for pid in selected)}"

    def collect() -> dict[int, dict[str, object]]:
        # -a 让 -p 与其它选择条件取交集；-Ffn 输出「进程/文件/名字」三段字段。
        stdout = _run(
            ["lsof", "-a", "-p", ",".join(str(pid) for pid in selected), "-Ffn"],
            _LSOF_TIMEOUT_SECONDS,
        )
        return _parse_lsof(stdout)

    cached = _cached(key, collect, now)
    assert isinstance(cached, dict)
    return cached  # type: ignore[return-value]


def _parse_lsof(stdout: str) -> dict[int, dict[str, object]]:
    """解析 ``lsof -Ffn`` 输出：``p`` 进程、``f`` 描述符、``n`` 名字。"""

    details: dict[int, dict[str, object]] = {}
    pid: int | None = None
    descriptor = ""
    for line in stdout.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid = int(value) if value.isdigit() else None
            if pid is not None:
                details.setdefault(pid, {"cwd": None, "files": []})
            continue
        if pid is None:
            continue
        if tag == "f":
            descriptor = value
            continue
        if tag != "n" or not value:
            continue
        record = details[pid]
        if descriptor == "cwd" and value.startswith("/"):
            record["cwd"] = Path(value)
            continue
        if value.startswith("/") and _looks_like_file(value):
            files = record["files"]
            assert isinstance(files, list)
            files.append(Path(value))
    return details


def _looks_like_file(value: str) -> bool:
    """只保留已经消失或真实存在的普通文件，过滤设备与伪文件。"""

    candidate = Path(value)
    try:
        return candidate.is_file()
    except OSError:
        return True


def scan_macos_connections(
    pids: Sequence[int],
    now: float | None = None,
) -> dict[int, tuple[ObservedConnection, ...]]:
    """用 ``lsof -i`` 读取这些进程的 TCP 连接（没有字节数）。"""

    if not pids:
        return {}
    selected = tuple(pids[:_MAX_QUERY_PIDS])
    key = f"macos:conn:{','.join(str(pid) for pid in selected)}"

    def collect() -> dict[int, tuple[ObservedConnection, ...]]:
        stdout = _run(
            [
                "lsof",
                "-a",
                "-p",
                ",".join(str(pid) for pid in selected),
                "-i",
                "-n",
                "-P",
                "-Ffn",
            ],
            _LSOF_TIMEOUT_SECONDS,
        )
        connections: dict[int, list[ObservedConnection]] = {}
        for pid, names in _parse_lsof_names(stdout).items():
            parsed = [
                item
                for item in (_parse_connection(name) for name in names)
                if item is not None
            ]
            if parsed:
                connections[pid] = parsed
        return {pid: tuple(items) for pid, items in connections.items()}

    cached = _cached(key, collect, now)
    assert isinstance(cached, dict)
    return cached  # type: ignore[return-value]


def _parse_lsof_names(stdout: str) -> dict[int, list[str]]:
    """收集 ``lsof -i`` 输出里每个进程的网络名字段。"""

    names: dict[int, list[str]] = {}
    pid: int | None = None
    for line in stdout.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid = int(value) if value.isdigit() else None
            if pid is not None:
                names.setdefault(pid, [])
            continue
        if tag != "n" or pid is None or not value:
            continue
        if "->" in value or value.startswith("*:"):
            names[pid].append(value)
    return names


def _parse_connection(name: str) -> ObservedConnection | None:
    """解析 lsof 的 ``local->remote`` 或 ``*:port`` 形式。"""

    text = name.strip()
    if not text:
        return None
    if "->" not in text:
        local_port = _endpoint_port(text)
        return ObservedConnection(
            local_port=local_port,
            remote_ip=None,
            remote_port=None,
            listening=True,
        )
    local_text, _, remote_text = text.partition("->")
    remote_ip, remote_port = _split_endpoint(remote_text)
    if remote_ip is None:
        return None
    return ObservedConnection(
        local_port=_endpoint_port(local_text),
        remote_ip=remote_ip,
        remote_port=remote_port,
        listening=False,
    )


def _split_endpoint(value: str) -> tuple[str | None, int | None]:
    """拆分 ``ip:port``；IPv6 可能带方括号。"""

    text = value.strip()
    if not text:
        return None, None
    host, separator, port_text = text.rpartition(":")
    if not separator:
        return text or None, None
    host = host.strip("[]")
    try:
        port = int(port_text)
    except ValueError:
        port = None
    return host or None, port


def _endpoint_port(value: str) -> int | None:
    """只取端点里的端口号。"""

    _, port = _split_endpoint(value)
    return port


# ---------------------------------------------------------------- 底层读取


def _run(command: Sequence[str], timeout: float) -> str:
    """执行只读子进程，任何失败都返回空字符串。"""

    if shutil.which(command[0]) is None:
        return ""
    try:
        result = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout


def _split_command(value: str) -> tuple[str, ...]:
    """把 ``ps`` 的命令行拆成 argv，失败时退回按空白切分。"""

    try:
        parts = shlex.split(value)
    except ValueError:
        parts = value.split()
    return tuple(parts)


def _read_command(path: Path) -> tuple[str, ...]:
    """读取 NUL 分隔命令行。"""

    try:
        raw = path.read_bytes()
    except OSError:
        return ()
    return tuple(
        item.decode("utf-8", errors="replace") for item in raw.split(b"\0") if item
    )


def _read_text(path: Path) -> str:
    """读取单行文本（comm 等）。"""

    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_stat(path: Path) -> tuple[int, str]:
    """读取 ppid 与 starttime，避免 PID 复用。"""

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, ""
    closing = content.rfind(")")
    if closing < 0:
        return 0, ""
    fields = content[closing + 1 :].split()
    ppid = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else 0
    start_token = fields[19] if len(fields) > 19 else ""
    return ppid, start_token


def _read_start_token(path: Path) -> str:
    """只读取 starttime。"""

    return _read_stat(path)[1]


def _read_cwd(path: Path) -> Path | None:
    """读取进程工作目录。"""

    try:
        target = path.readlink()
    except OSError:
        return None
    target_path = Path(str(target))
    try:
        return target_path.resolve()
    except OSError:
        return target_path


def _read_open_paths(directory: Path) -> tuple[Path, ...]:
    """读取进程打开的普通文件路径，忽略 socket/pipe。

    这里刻意不检查目标是否存在：日志轮转或会话目录被移走后，fd 仍指向旧路径，
    上游按路径前缀识别会话，需要保留这类证据。
    """

    paths: list[Path] = []
    seen: set[Path] = set()
    try:
        descriptors = tuple(directory.iterdir())
    except OSError:
        return ()
    for descriptor in descriptors:
        try:
            target = descriptor.readlink()
        except OSError:
            continue
        text = str(target)
        if text.startswith(("socket:", "pipe:", "anon_inode:")):
            continue
        if text.endswith(" (deleted)"):
            text = text[: -len(" (deleted)")]
        path = Path(text)
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        paths.append(resolved)
    return tuple(paths)


# ---------------------------------------------------------------- Windows 后端
#
# Windows 既没有 /proc 也没有 lsof，这里用系统自带能力拼出同样的观测结果：
# * 进程表：``CreateToolhelp32Snapshot`` 快照 + ``QueryFullProcessImageNameW``
#   镜像路径 + ``GetProcessTimes`` 启动时间，命令行尽量读 PEB（权限不足就退回镜像路径）；
# * 谁打开了会话文件：``Restart Manager``（rstrtmgr.dll）逐个文件反查持有进程，
#   不需要管理员权限；
# * TCP 连接：``GetExtendedTcpTable``（iphlpapi）能拿到「连接 + 归属 PID」，
#   但没有每连接字节数，所以 Windows 与 macOS 一样退化成 process-only。
#
# 所有 ctypes 调用都集中在下面几个 ``_windows_*`` 原语里，解析与组装逻辑是纯 Python，
# 便于在非 Windows 平台上用假数据测试。

_WINDOWS_SESSION_SUFFIXES = (".jsonl", ".json", ".lock")
_WINDOWS_SESSION_FILE_LIMIT = 200
_WINDOWS_SESSION_WINDOW_SECONDS = 3600.0
_ERROR_MORE_DATA = 234
_ERROR_ACCESS_DENIED = 5
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
_TH32CS_SNAPPROCESS = 0x00000002
_TCP_TABLE_OWNER_PID_ALL = 5
_TCP_STATE_LISTEN = 2
_TCP_STATE_ESTABLISHED = 5


def process_alive(pid: int | None) -> bool:
    """判断进程是否存活。

    中文注释：Windows 上 ``os.kill(pid, 0)`` 会直接结束目标进程（CPython 把它
    变成 ``TerminateProcess``），所以存活探测必须走 OpenProcess。
    """

    if pid is None or pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def launch_command(executable: str, *arguments: str) -> list[str]:
    """构造可以直接交给 ``subprocess`` 的命令行。

    中文注释：Windows 上 npm 安装的 CLI 是 ``.cmd`` / ``.bat`` 批处理包装，
    ``CreateProcess`` 不解析 PATHEXT，直接 spawn ``codex`` 会找不到文件；
    这里解析出真实路径并用 ``cmd.exe /c`` 启动，其它平台原样返回。
    """

    if sys.platform != "win32":
        return [executable, *arguments]
    resolved = executable
    if not os.path.dirname(executable):
        located = shutil.which(executable)
        if located:
            resolved = located
    if resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", resolved, *arguments]
    return [resolved, *arguments]


def scan_windows_processes(now: float | None = None) -> dict[int, ObservedProcess]:
    """Windows 进程表，键为 PID。"""

    cached = _cached("windows:processes", _windows_process_table, now)
    assert isinstance(cached, dict)
    return cached  # type: ignore[return-value]


def scan_windows_agents(
    products: Sequence[str] | None = None,
    ignore_pids: Sequence[int] | None = None,
    session_roots: Sequence[Path] | None = None,
    now: float | None = None,
) -> tuple[RunningAgent, ...]:
    """Windows：Toolhelp32 找进程，Restart Manager 反查会话文件持有者。"""

    from .agents import RunningAgent, identify_agent

    wanted = set(products) if products is not None else None
    ignored = set(ignore_pids or ())
    roots = tuple(Path(root) for root in (session_roots or ()))
    key = (
        "windows:agents:"
        f"{','.join(sorted(wanted)) if wanted is not None else '*'}:"
        f"{','.join(str(pid) for pid in sorted(ignored))}:"
        f"{','.join(str(root) for root in roots)}"
    )

    def collect() -> tuple[RunningAgent, ...]:
        table = scan_windows_processes(now)
        candidates: dict[int, tuple[str, ObservedProcess]] = {}
        for pid, info in table.items():
            if pid in ignored:
                continue
            product = identify_agent(info.command, info.comm)
            if product is None:
                continue
            if wanted is not None and product not in wanted:
                continue
            candidates[pid] = (product, info)
        paths_by_pid: dict[int, list[Path]] = {}
        if candidates and roots:
            for path, pids in _windows_session_owners(roots, now).items():
                for pid in pids:
                    if pid in candidates:
                        paths_by_pid.setdefault(pid, []).append(path)
        agents: list[RunningAgent] = []
        for pid, (product, info) in candidates.items():
            agents.append(
                RunningAgent(
                    pid=pid,
                    product=product,
                    start_token=info.start_token,
                    cwd=info.cwd,
                    command=info.command,
                    open_paths=tuple(
                        sorted(set(paths_by_pid.get(pid, ())), key=str)
                    ),
                )
            )
        return tuple(agents)

    cached = _cached(key, collect, now)
    return tuple(cached)  # type: ignore[arg-type]


def scan_windows_connections(
    pids: Sequence[int],
    now: float | None = None,
) -> dict[int, tuple[ObservedConnection, ...]]:
    """用 ``GetExtendedTcpTable`` 读取这些进程的 TCP 连接（没有字节数）。"""

    if not pids:
        return {}
    selected = tuple(int(pid) for pid in pids[:_MAX_QUERY_PIDS])
    key = f"windows:conn:{','.join(str(pid) for pid in sorted(selected))}"

    def collect() -> dict[int, tuple[ObservedConnection, ...]]:
        return _parse_windows_connections(_windows_tcp_rows(), selected)

    cached = _cached(key, collect, now)
    assert isinstance(cached, dict)
    return cached  # type: ignore[return-value]


def scan_process_connections(
    pids: Sequence[int],
    proc_root: Path | None = None,
    now: float | None = None,
) -> dict[int, tuple[ObservedConnection, ...]]:
    """按平台读取这些进程的 TCP 连接（Linux 的字节统计不走这里）。"""

    backend = select_backend(proc_root)
    if backend == "macos":
        return scan_macos_connections(pids, now)
    if backend == "windows":
        return scan_windows_connections(pids, now)
    return {}


def _windows_process_table() -> dict[int, ObservedProcess]:
    """Toolhelp32 + 镜像路径 + 启动时间 → 统一观测结构。"""

    return _parse_windows_processes(_windows_process_rows())


def _parse_windows_processes(
    rows: Sequence[Mapping[str, object]],
) -> dict[int, ObservedProcess]:
    """把 ctypes 原语读到的行转换成统一观测结构。"""

    processes: dict[int, ObservedProcess] = {}
    for row in rows:
        try:
            pid = int(row.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        raw_command = row.get("command") or ()
        command = tuple(str(part) for part in raw_command if part)
        image = str(row.get("image") or row.get("exe") or "")
        if not command and image:
            command = (image,)
        comm = _file_name(image) or (_file_name(command[0]) if command else "")
        processes[pid] = ObservedProcess(
            pid=pid,
            ppid=int(row.get("ppid") or 0),
            command=command,
            comm=comm,
            start_token=str(row.get("created") or ""),
            cwd=None,
            open_paths=(),
        )
    return processes


def _parse_windows_connections(
    rows: Sequence[Mapping[str, object]],
    pids: Sequence[int],
) -> dict[int, tuple[ObservedConnection, ...]]:
    """把 TCP 表行按归属 PID 分组。"""

    wanted = {int(pid) for pid in pids}
    grouped: dict[int, list[ObservedConnection]] = {}
    for row in rows:
        try:
            pid = int(row.get("pid") or 0)
            state = int(row.get("state") or 0)
        except (TypeError, ValueError):
            continue
        if pid not in wanted:
            continue
        remote_ip = row.get("remote_ip")
        remote_port = row.get("remote_port")
        listening = state == _TCP_STATE_LISTEN or not remote_ip or not remote_port
        grouped.setdefault(pid, []).append(
            ObservedConnection(
                local_port=_optional_int(row.get("local_port")),
                remote_ip=None if listening else str(remote_ip),
                remote_port=None if listening else _optional_int(remote_port),
                listening=listening,
            )
        )
    return {pid: tuple(items) for pid, items in grouped.items()}


def _windows_session_owners(
    roots: Sequence[Path],
    now: float | None = None,
) -> dict[Path, tuple[int, ...]]:
    """返回最近改动过的会话文件 → 持有它的 PID。"""

    moment = time.time() if now is None else float(now)
    files = _recent_session_files(roots, moment)
    if not files:
        return {}
    owners = _cached(
        "windows:owners:" + ",".join(str(path) for path in files),
        lambda: _windows_file_owners(files),
        now,
    )
    assert isinstance(owners, dict)
    return owners  # type: ignore[return-value]


def _recent_session_files(
    roots: Sequence[Path],
    now: float,
    *,
    limit: int = _WINDOWS_SESSION_FILE_LIMIT,
    window_seconds: float = _WINDOWS_SESSION_WINDOW_SECONDS,
) -> tuple[Path, ...]:
    """列出会话目录下最近改动过的候选文件。

    句柄反查要逐个文件调用系统 API，所以只取最近改动的一批，避免扫描整棵目录。
    """

    cutoff = now - window_seconds
    found: list[tuple[float, Path]] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for path in root.rglob("*"):
                try:
                    if path.suffix.lower() not in _WINDOWS_SESSION_SUFFIXES:
                        continue
                    if not path.is_file():
                        continue
                    modified = path.stat().st_mtime
                except OSError:
                    continue
                if modified < cutoff:
                    continue
                found.append((modified, path))
        except OSError:
            continue
    found.sort(key=lambda item: item[0], reverse=True)
    return tuple(path for _, path in found[:limit])


def _windows_file_owners(files: Sequence[Path]) -> dict[Path, tuple[int, ...]]:
    """逐个文件查询 Restart Manager，返回持有该文件的进程。"""

    owners: dict[Path, tuple[int, ...]] = {}
    for path in files:
        try:
            pids = _windows_file_owner_pids(str(path))
        except OSError:
            continue
        if pids:
            owners[path] = pids
    return owners


def _file_name(value: str) -> str:
    """取 Windows/POSIX 路径的文件名（两个平台都可调用）。"""

    return value.replace("\\", "/").rsplit("/", 1)[-1].strip()


def _optional_int(value: object) -> int | None:
    """尽力把值转成整数，失败返回 None。"""

    if value in (None, ""):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _kernel32() -> object | None:
    """加载 kernel32（只有 Windows 有）。"""

    if sys.platform != "win32":
        return None
    import ctypes

    try:
        return ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:  # pragma: no cover - 只在异常 Windows 环境触发
        return None


def _windows_process_alive(pid: int) -> bool:
    """OpenProcess + GetExitCodeProcess 判断进程是否仍在运行。"""

    import ctypes
    from ctypes import wintypes

    kernel32 = _kernel32()
    if kernel32 is None:
        return False
    handle = kernel32.OpenProcess(  # type: ignore[attr-defined]
        _PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        int(pid),
    )
    if not handle:
        # 权限不足（例如更高完整性级别的进程）按“仍在运行”处理，避免误判退出
        last_error = getattr(ctypes, "get_last_error", lambda: 0)()
        return last_error == _ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(  # type: ignore[attr-defined]
            handle,
            ctypes.byref(code),
        ):
            return False
        return int(code.value) == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)  # type: ignore[attr-defined]


def _windows_process_rows() -> tuple[dict[str, object], ...]:
    """读取 Windows 进程表（Toolhelp32 快照）。"""

    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = _kernel32()
    if kernel32 is None:
        return ()
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)  # type: ignore[attr-defined]
    if not snapshot:
        return ()
    rows: list[dict[str, object]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):  # type: ignore[attr-defined]
            return ()
        while True:
            pid = int(entry.th32ProcessID)
            rows.append(
                {
                    "pid": pid,
                    "ppid": int(entry.th32ParentProcessID),
                    "exe": entry.szExeFile,
                    "image": _windows_image_path(kernel32, pid),
                    "created": _windows_started_at(kernel32, pid),
                    "command": _windows_command_line(pid),
                }
            )
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):  # type: ignore[attr-defined]
                break
    finally:
        kernel32.CloseHandle(snapshot)  # type: ignore[attr-defined]
    return tuple(rows)


def _windows_image_path(kernel32: object, pid: int) -> str:
    """读取进程镜像的完整路径。"""

    import ctypes
    from ctypes import wintypes

    handle = kernel32.OpenProcess(  # type: ignore[attr-defined]
        _PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        pid,
    )
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(  # type: ignore[attr-defined]
            handle,
            0,
            buffer,
            ctypes.byref(size),
        ):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)  # type: ignore[attr-defined]


def _windows_started_at(kernel32: object, pid: int) -> str:
    """读取进程创建时间，作为跨轮次稳定的 start_token。"""

    import ctypes
    from ctypes import wintypes

    handle = kernel32.OpenProcess(  # type: ignore[attr-defined]
        _PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        pid,
    )
    if not handle:
        return ""
    try:
        created = wintypes.FILETIME()
        empty = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(  # type: ignore[attr-defined]
            handle,
            ctypes.byref(created),
            ctypes.byref(empty),
            ctypes.byref(empty),
            ctypes.byref(empty),
        ):
            return ""
        value = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        return f"windows:{value}"
    finally:
        kernel32.CloseHandle(handle)  # type: ignore[attr-defined]


def _windows_command_line(pid: int) -> tuple[str, ...]:
    """尽量读取完整命令行（PEB），权限不足时返回空让上层退回镜像路径。"""

    import ctypes
    from ctypes import wintypes

    kernel32 = _kernel32()
    if kernel32 is None:
        return ()
    handle = kernel32.OpenProcess(  # type: ignore[attr-defined]
        _PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ,
        False,
        pid,
    )
    if not handle:
        return ()
    try:
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        peb_parameters_offset = 0x20 if pointer_size == 8 else 0x10
        command_line_offset = 0x70 if pointer_size == 8 else 0x40

        class PROCESS_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("Reserved1", ctypes.c_void_p),
                ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2),
                ("UniqueProcessId", ctypes.c_void_p),
                ("Reserved3", ctypes.c_void_p),
            ]

        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p),
            ]

        ntdll = ctypes.WinDLL("ntdll")
        info = PROCESS_BASIC_INFORMATION()
        returned = wintypes.ULONG()
        status = ntdll.NtQueryInformationProcess(
            handle,
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        )
        if status != 0 or not info.PebBaseAddress:
            return ()
        parameters = ctypes.c_void_p()
        if not kernel32.ReadProcessMemory(  # type: ignore[attr-defined]
            handle,
            ctypes.c_void_p(info.PebBaseAddress + peb_parameters_offset),
            ctypes.byref(parameters),
            pointer_size,
            None,
        ):
            return ()
        if not parameters.value:
            return ()
        command = UNICODE_STRING()
        if not kernel32.ReadProcessMemory(  # type: ignore[attr-defined]
            handle,
            ctypes.c_void_p(parameters.value + command_line_offset),
            ctypes.byref(command),
            ctypes.sizeof(command),
            None,
        ):
            return ()
        if not command.Buffer or not command.Length:
            return ()
        buffer = ctypes.create_unicode_buffer(int(command.Length) // 2 + 1)
        if not kernel32.ReadProcessMemory(  # type: ignore[attr-defined]
            handle,
            ctypes.c_void_p(command.Buffer),
            buffer,
            int(command.Length),
            None,
        ):
            return ()
        return _split_command(buffer.value)
    except OSError:  # pragma: no cover - 只在异常 Windows 环境触发
        return ()
    finally:
        kernel32.CloseHandle(handle)  # type: ignore[attr-defined]


def _windows_file_owner_pids(path: str) -> tuple[int, ...]:
    """Restart Manager：返回正在占用该文件的进程 PID。"""

    import ctypes
    from ctypes import wintypes

    if sys.platform != "win32":
        return ()

    class RM_UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [
            ("dwProcessId", wintypes.DWORD),
            ("ProcessStartTime", wintypes.FILETIME),
        ]

    class RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [
            ("Process", RM_UNIQUE_PROCESS),
            ("strAppName", wintypes.WCHAR * 256),
            ("strServiceShortName", wintypes.WCHAR * 64),
            ("ApplicationType", ctypes.c_uint),
            ("AppStatus", wintypes.ULONG),
            ("TSSessionId", wintypes.DWORD),
            ("bRestartable", wintypes.BOOL),
        ]

    try:
        rstrtmgr = ctypes.WinDLL("rstrtmgr", use_last_error=True)
    except OSError:  # pragma: no cover - 只在异常 Windows 环境触发
        return ()
    session = wintypes.DWORD()
    session_key = ctypes.create_unicode_buffer(256)
    if rstrtmgr.RmStartSession(ctypes.byref(session), 0, session_key) != 0:
        return ()
    try:
        resources = (wintypes.LPCWSTR * 1)(path)
        if rstrtmgr.RmRegisterResources(session, 1, resources, 0, None, 0, None) != 0:
            return ()
        needed = wintypes.UINT(0)
        count = wintypes.UINT(0)
        reasons = wintypes.DWORD(0)
        result = rstrtmgr.RmGetList(
            session,
            ctypes.byref(needed),
            ctypes.byref(count),
            None,
            ctypes.byref(reasons),
        )
        if needed.value == 0:
            return ()
        if result not in (0, _ERROR_MORE_DATA):
            return ()
        array = (RM_PROCESS_INFO * needed.value)()
        count = wintypes.UINT(needed.value)
        if (
            rstrtmgr.RmGetList(
                session,
                ctypes.byref(needed),
                ctypes.byref(count),
                array,
                ctypes.byref(reasons),
            )
            != 0
        ):
            return ()
        return tuple(
            sorted({int(array[index].Process.dwProcessId) for index in range(count.value)})
        )
    finally:
        rstrtmgr.RmEndSession(session)


def _windows_tcp_rows() -> tuple[dict[str, object], ...]:
    """用 GetExtendedTcpTable 读取「连接 + 归属 PID」（IPv4 与 IPv6）。"""

    import ctypes
    import struct as struct_module
    from ctypes import wintypes

    if sys.platform != "win32":
        return ()
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    except OSError:  # pragma: no cover - 只在异常 Windows 环境触发
        return ()
    rows: list[dict[str, object]] = []
    for family in (socket.AF_INET, socket.AF_INET6):
        size = wintypes.DWORD(0)
        iphlpapi.GetExtendedTcpTable(
            None,
            ctypes.byref(size),
            False,
            family,
            _TCP_TABLE_OWNER_PID_ALL,
            0,
        )
        if size.value == 0:
            continue
        buffer = ctypes.create_string_buffer(size.value)
        if (
            iphlpapi.GetExtendedTcpTable(
                buffer,
                ctypes.byref(size),
                False,
                family,
                _TCP_TABLE_OWNER_PID_ALL,
                0,
            )
            != 0
        ):
            continue
        count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
        offset = ctypes.sizeof(wintypes.DWORD)
        if family == socket.AF_INET:
            for index in range(count):
                fields = struct_module.unpack_from("<6I", buffer.raw, offset + index * 24)
                rows.append(
                    {
                        "state": fields[0],
                        "local_ip": socket.inet_ntoa(struct_module.pack("<I", fields[1])),
                        "local_port": socket.ntohs(fields[2] & 0xFFFF),
                        "remote_ip": socket.inet_ntoa(struct_module.pack("<I", fields[3])),
                        "remote_port": socket.ntohs(fields[4] & 0xFFFF),
                        "pid": fields[5],
                    }
                )
        else:
            for index in range(count):
                base = offset + index * 56
                state = struct_module.unpack_from("<I", buffer.raw, base)[0]
                local = buffer.raw[base + 4 : base + 20]
                local_port = struct_module.unpack_from("<I", buffer.raw, base + 24)[0]
                remote = buffer.raw[base + 28 : base + 44]
                remote_port = struct_module.unpack_from("<I", buffer.raw, base + 48)[0]
                pid = struct_module.unpack_from("<I", buffer.raw, base + 52)[0]
                rows.append(
                    {
                        "state": state,
                        "local_ip": socket.inet_ntop(socket.AF_INET6, local),
                        "local_port": socket.ntohs(local_port & 0xFFFF),
                        "remote_ip": socket.inet_ntop(socket.AF_INET6, remote),
                        "remote_port": socket.ntohs(remote_port & 0xFFFF),
                        "pid": pid,
                    }
                )
    return tuple(rows)
