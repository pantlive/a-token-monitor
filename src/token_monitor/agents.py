"""识别本机正在运行的 code agent 进程。

只根据命令行和 comm 判断产品类型，不读取提示词、环境变量或凭据。
解释器启动的 CLI（例如 ``node .../dsh``）按后续可执行文件名识别。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


# 中文注释：监控器自身命令行里会出现这些标记，不能当成被监控的 Codex。
_MONITOR_MARKERS = ("token_monitor", "token-monitor")

# 中文注释：Windows 上 npm 安装的 CLI 是 .cmd/.exe 包装，脚本型 CLI 也会以
# ``node .../codex.js`` 的形式出现，识别前统一去掉这些后缀。
_EXECUTABLE_SUFFIXES = (
    ".exe",
    ".cmd",
    ".bat",
    ".com",
    ".ps1",
    ".js",
    ".mjs",
    ".cjs",
    ".py",
)

_INTERPRETERS = {
    "node",
    "nodejs",
    "python",
    "python3",
    "python3.10",
    "python3.11",
    "python3.12",
    "python3.13",
    "bun",
    "deno",
    # Windows 的 shell 包装（cmd /c codex.cmd、powershell -File ...）
    "cmd",
    "powershell",
    "pwsh",
    "wsl",
}

_GENERIC_COMM = _INTERPRETERS | {
    "MainThread",
    "sh",
    "bash",
    "zsh",
    "fish",
    "dash",
}

# npm 包的入口常叫 cli.js / index.js，用包目录名兜底识别（仅限 node_modules 路径）。
_AGENT_DIRECTORIES = {
    "claude-code": "claude",
    "codex": "codex",
    "kimi-code": "kimi",
    "deepseek-harness": "dsh",
    "command-code": "command-code",
    "grok": "grok",
    "grok-cli": "grok",
}

# argv0 / 解释器脚本名 -> 产品 ID。
_AGENT_BINARIES = {
    "codex": "codex",
    "codex-code-mode": "codex",
    "codex-code-mode-host": "codex",
    "grok": "grok",
    "kimi": "kimi",
    "kimi-code": "kimi",
    "dsh": "dsh",
    "deepseek-harness": "dsh",
    "deepseek": "dsh",
    "command-code": "command-code",
    "commandcode": "command-code",
    "cmdc": "command-code",
    "claude": "claude",
    "opencode": "opencode",
    "cursor-agent": "cursor",
    "aider": "aider",
    "gemini": "gemini",
    "qwen": "qwen",
    "qwen-code": "qwen",
}

PRODUCT_LABELS = {
    "codex": "Codex CLI",
    "grok": "Grok CLI",
    "kimi": "Kimi Code",
    "dsh": "DeepSeek Harness",
    "command-code": "Command Code",
    "claude": "Claude Code",
    "opencode": "OpenCode",
    "cursor": "Cursor Agent",
    "aider": "Aider",
    "gemini": "Gemini CLI",
    "qwen": "Qwen Code",
}


@dataclass(frozen=True)
class RunningAgent:
    """一个已识别的 code agent 进程及其打开的文件。"""

    pid: int
    product: str
    start_token: str
    cwd: Path | None
    command: tuple[str, ...]
    open_paths: tuple[Path, ...]


def product_label(product: str) -> str:
    """返回产品的展示名称。"""

    return PRODUCT_LABELS.get(product, product)


def identify_agent(
    command: Sequence[str],
    comm: str = "",
) -> str | None:
    """从命令行识别 code agent 产品；无法识别时返回 None。

    不把搜索命令的正则参数（例如 ``rg grok``）误判成 Grok CLI。
    """

    parts = tuple(part for part in command if part)
    joined = " ".join(parts)
    if any(marker in joined for marker in _MONITOR_MARKERS):
        return None
    argv0 = _basename(parts[0]) if parts else ""
    product = _AGENT_BINARIES.get(argv0)
    if product is not None:
        return product
    comm_name = _basename(comm)
    product = _AGENT_BINARIES.get(comm_name)
    if product is not None:
        return product
    if argv0 in _INTERPRETERS or comm_name in _GENERIC_COMM:
        for part in parts[1:]:
            if part.startswith("-"):
                continue
            product = _AGENT_BINARIES.get(_basename(part))
            if product is not None:
                return product
        product = _identify_from_node_modules(parts[1:])
        if product is not None:
            return product
    return None


def _identify_from_node_modules(parts: Sequence[str]) -> str | None:
    """解释器 + 通用入口脚本时，用 node_modules 之后的包目录名识别产品。"""

    for part in parts:
        if part.startswith("-"):
            continue
        normalized = part.replace("\\", "/")
        if "node_modules/" not in normalized and "/node_modules" not in normalized:
            continue
        segments = [
            item
            for item in normalized.split("/")
            if item and item != "node_modules"
        ]
        for segment in reversed(segments):
            product = _AGENT_DIRECTORIES.get(segment.lower())
            if product is not None:
                return product
    return None


def scan_running_agents(
    proc_root: Path | None = None,
    products: Sequence[str] | None = None,
    ignore_pids: Sequence[int] | None = None,
    session_roots: Sequence[Path] | None = None,
) -> tuple[RunningAgent, ...]:
    """返回指定产品的进程和它们打开的普通文件。

    Linux 走 ``/proc``；macOS 没有 ``/proc``，改用 ``ps`` + ``lsof``；
    Windows 用 Toolhelp32 + Restart Manager。显式传入 ``proc_root``
    （测试与容器）时始终按 ``/proc`` 语义处理。

    ``session_roots`` 只在 Windows 上使用：句柄反查需要知道会话文件在哪些目录下，
    其它平台会忽略它。
    """

    from .process_backend import scan_agents

    return tuple(scan_agents(proc_root, products, ignore_pids, session_roots))


def _basename(value: str) -> str:
    """读取可执行名：同时兼容 POSIX 与 Windows 路径和可执行后缀。"""

    text = value.strip()
    if not text:
        return ""
    # 中文注释：POSIX 上 Path 不把反斜杠当分隔符，这里显式统一，
    # 让 Windows 风格命令行（或测试数据）在两个平台都能识别。
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    lowered = text.lower()
    for suffix in _EXECUTABLE_SUFFIXES:
        if lowered.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


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
    """读取 comm 等单行文本。"""

    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_start_token(path: Path) -> str:
    """读取 starttime，避免 PID 复用。"""

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    closing = content.rfind(")")
    if closing < 0:
        return ""
    fields = content[closing + 1 :].split()
    return fields[19] if len(fields) > 19 else ""


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
    """读取进程打开的普通文件路径，忽略 socket/pipe。"""

    paths: list[Path] = []
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
            paths.append(path.resolve())
        except OSError:
            paths.append(path)
    return tuple(paths)
