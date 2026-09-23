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

    if select_backend(proc_root) == "macos":
        cached = _cached("macos:ps", _scan_macos_process_table, now)
        processes = dict(cached)  # type: ignore[arg-type]
        return _attach_macos_details(processes)
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
) -> tuple[RunningAgent, ...]:
    """按产品过滤的 agent 进程；在两个平台上返回同样的 :class:`RunningAgent`。"""

    if select_backend(proc_root) == "macos":
        return scan_macos_agents(products=products, ignore_pids=ignore_pids)
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
