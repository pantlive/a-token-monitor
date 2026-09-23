"""Claude Code 本地数据目录识别与 JSONL 用量解析。

Claude Code 把每个会话写成 ``projects/<项目>/<会话 UUID>.jsonl``，子代理的
记录放在同名的 ``<会话 UUID>/subagents/**`` 目录里。用量只读取 assistant 行
的 ``message.usage``（单次请求的 token 数，不是累计值）以及时间戳、模型和
``cwd``；不读取提示词、工具输出或附件内容。

不同版本的 Claude Code 布局不同：有的把子代理消息内联写在主会话文件里
（``isSidechain: true``），有的只写在 ``subagents`` 目录。前者只索引主文件，
后者两边都索引，避免重复计数。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .agents import scan_running_agents
from .multi_models import DetectionConfidence, SessionStatus, TrackedSession

# 中文注释：一次解析最多保留的 message.id，用于跨轮次去重。
_MAX_SEEN_IDS = 400
# 中文注释：单行上限，避免异常大行拖垮解析。
_MAX_LINE_BYTES = 4 * 1024 * 1024
_SIDECHAIN_MARKER = b'"isSidechain":true'
# 中文注释：检测内联侧链时每个主会话最多读这么多字节。
_SIDECHAIN_SCAN_BYTES = 4 * 1024 * 1024
_SIDECHAIN_CACHE_SECONDS = 600.0


@dataclass(frozen=True)
class ClaudeAccount:
    """一个 Claude Code 数据目录的安全身份信息。"""

    home: Path
    account_id: str | None
    display_name: str
    profile_name: str = "claude"
    has_credentials: bool = False
    email: str | None = None

    @property
    def account_key(self) -> str:
        """返回优先使用本地 userID 的归组键。"""

        return self.account_id or f"profile:{self.profile_name}"


@dataclass(frozen=True)
class ClaudeUsageEvent:
    """一次 Claude 模型请求的 token 用量。"""

    timestamp: float
    model: str
    project: str | None
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    total_tokens: int
    message_id: str | None = None


@dataclass(frozen=True)
class ClaudeParseResult:
    """一次增量解析的结果。"""

    next_offset: int
    events: tuple[ClaudeUsageEvent, ...]
    project: str | None
    seen_ids: tuple[str, ...]
    bytes_read: int
    reached_eof: bool
    discarding_oversized_line: bool = False


def default_claude_home() -> Path:
    """返回 Claude Code 默认数据目录。"""

    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".claude"


def resolve_claude_homes(
    homes: Sequence[Path] | None = None,
) -> tuple[Path, ...]:
    """解析 Claude Code 数据目录列表。

    与其他 provider 保持同一套语义：``None`` 表示自动探测（``CLAUDE_CONFIG_DIR``
    或 ``~/.claude``，存在才用），显式空列表表示不监控 Claude Code。
    """

    if homes is not None:
        resolved: list[Path] = []
        seen: set[Path] = set()
        for home in homes:
            path = Path(home).expanduser()
            if path in seen:
                continue
            seen.add(path)
            resolved.append(path)
        return tuple(resolved)
    default = default_claude_home()
    return (default,) if default.is_dir() else ()


def claude_projects_root(home: Path) -> Path:
    """返回某个数据目录下的 projects 根目录。"""

    return Path(home).expanduser() / "projects"


def main_transcripts(home: Path) -> tuple[Path, ...]:
    """列出主会话 JSONL（projects/<项目>/<会话>.jsonl）。"""

    root = claude_projects_root(home)
    if not root.is_dir():
        return ()
    files: list[Path] = []
    for project in sorted(root.iterdir()):
        if not project.is_dir():
            continue
        files.extend(sorted(path for path in project.glob("*.jsonl") if path.is_file()))
    return tuple(files)


def subagent_transcripts(home: Path) -> tuple[Path, ...]:
    """列出子代理 JSONL（projects/<项目>/<会话>/subagents/**/*.jsonl）。"""

    root = claude_projects_root(home)
    if not root.is_dir():
        return ()
    files: list[Path] = []
    for project in sorted(root.iterdir()):
        if not project.is_dir():
            continue
        for session_dir in sorted(project.iterdir()):
            if not session_dir.is_dir():
                continue
            subagents = session_dir / "subagents"
            if not subagents.is_dir():
                continue
            files.extend(
                sorted(path for path in subagents.rglob("*.jsonl") if path.is_file())
            )
    return tuple(files)


def list_claude_transcripts(
    home: Path,
    *,
    include_subagents: bool = True,
) -> tuple[Path, ...]:
    """列出要索引的会话文件；主会话永远包含。"""

    files = list(main_transcripts(home))
    if include_subagents:
        files.extend(subagent_transcripts(home))
    return tuple(files)


def claude_has_inline_sidechains(
    home: Path,
    *,
    max_bytes_per_file: int = _SIDECHAIN_SCAN_BYTES,
) -> bool:
    """判断主会话文件是否已经内联写入了子代理消息。

    内联写入时 ``subagents`` 目录里的记录是重复的，只索引主文件即可；
    无法判断时返回 False（宁可按“两边都有用量”处理，也不漏掉子代理用量）。
    """

    for path in main_transcripts(home):
        try:
            with path.open("rb") as handle:
                remaining = max_bytes_per_file
                while remaining > 0:
                    chunk = handle.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if _SIDECHAIN_MARKER in chunk.replace(b" ", b""):
                        return True
        except OSError:
            continue
    return False


def resolve_sidechain_policy(
    homes: Sequence[Path],
    *,
    now: float | None = None,
) -> dict[Path, bool]:
    """返回每个数据目录是否要把子代理文件计入索引，带短时缓存。"""

    observed_at = time.time() if now is None else float(now)
    with _POLICY_LOCK:
        for home in homes:
            cached = _POLICY_CACHE.get(home)
            if (
                cached is not None
                and observed_at - cached[0] < _SIDECHAIN_CACHE_SECONDS
            ):
                continue
            include = not claude_has_inline_sidechains(home)
            _POLICY_CACHE[home] = (observed_at, include)
    with _POLICY_LOCK:
        return {home: _POLICY_CACHE[home][1] for home in homes}


_CWD_PATTERN = re.compile(r'"cwd"\s*:\s*"([^"]{1,1024})"')
_MODEL_PATTERN = re.compile(r'"model"\s*:\s*"([a-z0-9][a-z0-9._/-]{1,80})"')
_FIRST_TIMESTAMP_PATTERN = re.compile(r'"timestamp"\s*:\s*"([^"]{8,40})"')
_HEADER_READ_BYTES = 64 * 1024


def list_claude_active_sessions(
    claude_home: Path,
    proc_root: Path | None = None,
    now: float | None = None,
) -> tuple[TrackedSession, ...]:
    """列出当前有 Claude Code 进程打开会话 JSONL 的活动会话。

    与 Kimi / DSH / Grok 对齐：以 ``/proc/<pid>/fd`` 里实际打开的会话文件为准，
    同一会话被多个进程打开时合并 pids，进程退出后自然消失；项目、模型和开始
    时间只从会话文件头部读取，不读取提示词或工具输出。
    """

    home = _normalize_path(claude_home)
    projects_root = claude_projects_root(home)
    observed_at = time.time() if now is None else float(now)
    agents = scan_running_agents(proc_root=proc_root, products=("claude",))
    grouped: dict[str, list[int]] = {}
    path_by_session: dict[str, Path] = {}
    for agent in agents:
        for path in agent.open_paths:
            session_id = claude_session_id_from_open_path(path, projects_root)
            if session_id is None:
                continue
            grouped.setdefault(session_id, [])
            if agent.pid not in grouped[session_id]:
                grouped[session_id].append(agent.pid)
            path_by_session.setdefault(session_id, path)

    sessions: list[TrackedSession] = []
    for session_id, pids in grouped.items():
        transcript = path_by_session.get(session_id)
        header = _read_claude_header(transcript)
        last_event_at = header["modified_at"]
        sessions.append(
            TrackedSession(
                thread_id=f"claude:{session_id}",
                session_id=session_id,
                jsonl_path=str(transcript) if transcript is not None else None,
                cwd=header["project"],
                source="claude-cli",
                status=SessionStatus.RUNNING,
                confidence=DetectionConfidence.OPEN_FILE,
                first_seen_at=header["started_at"] or observed_at,
                last_seen_at=observed_at,
                pids=tuple(sorted(pids)),
                last_event_at=last_event_at,
                last_event_type="assistant" if header["model"] else "session",
                product="claude",
                model=header["model"],
                project=header["project"],
            )
        )
    sessions.sort(key=lambda item: item.last_seen_at, reverse=True)
    return tuple(sessions)


def claude_session_id_from_open_path(
    path: Path,
    projects_root: Path,
) -> str | None:
    """从进程打开的路径解析会话 ID；路径必须在该项目会话根下。"""

    normalized = _normalize_path(path)
    root = _normalize_path(projects_root)
    if root not in normalized.parents:
        return None
    return claude_session_id(normalized)


def _read_claude_header(
    path: Path | None,
    *,
    max_bytes: int = _HEADER_READ_BYTES,
) -> dict[str, Any]:
    """只读会话文件头部，取出项目、模型和开始时间。"""

    header: dict[str, Any] = {
        "project": None,
        "model": None,
        "started_at": None,
        "modified_at": None,
    }
    if path is None:
        return header
    try:
        stat_result = path.stat()
        header["modified_at"] = stat_result.st_mtime
        with path.open("rb") as handle:
            chunk = handle.read(max_bytes)
    except OSError:
        return header
    project = _CWD_PATTERN.search(chunk.decode("utf-8", errors="replace"))
    if project is not None:
        header["project"] = project.group(1)
    model = _MODEL_PATTERN.search(chunk.decode("utf-8", errors="replace"))
    if model is not None:
        header["model"] = model.group(1)
    started = _FIRST_TIMESTAMP_PATTERN.search(
        chunk.decode("utf-8", errors="replace")
    )
    if started is not None:
        header["started_at"] = _timestamp(started.group(1))
    return header


def read_claude_account(home: Path) -> ClaudeAccount:
    """读取本地 userID 与登录邮箱；不读取任何凭据内容。"""

    path = Path(home).expanduser()
    payload = _read_json_object(_config_file(path))
    account_id = _text(payload.get("userID")) if payload else None
    email: str | None = None
    if payload is not None:
        oauth = payload.get("oauthAccount")
        if isinstance(oauth, Mapping):
            email = _text(oauth.get("emailAddress"))
    has_credentials = (path / ".credentials.json").is_file()
    return ClaudeAccount(
        home=path,
        account_id=account_id,
        display_name=email or account_id or "Claude Code",
        has_credentials=has_credentials,
        email=email,
    )


def claude_home_for(path: Path, homes: Sequence[Path]) -> Path | None:
    """判断一个 JSONL 是否属于某个 Claude 数据目录。"""

    candidate = _normalize_path(path)
    for home in homes:
        root = _normalize_path(claude_projects_root(home))
        if candidate == root or root in candidate.parents:
            return home
    return None


def claude_session_id(path: Path) -> str | None:
    """从文件名或子代理目录推断会话 ID。"""

    name = path.name
    if name.endswith(".jsonl"):
        name = name[: -len(".jsonl")]
    if name.startswith("agent-"):
        # 子代理文件：<会话>/subagents/[workflows/<wf>/]agent-xxx.jsonl
        for parent in path.parents:
            if parent.name == "subagents":
                return parent.parent.name or None
        return None
    return name or None


def parse_claude_chunk(
    path: Path,
    offset: int,
    *,
    seen_ids: Sequence[str] = (),
    discarding_oversized_line: bool = False,
    maximum_bytes: int | None = None,
) -> ClaudeParseResult:
    """从已确认的字节偏移继续解析 Claude Code 会话 JSONL。"""

    if offset < 0:
        raise ValueError("offset 不能小于 0")
    if maximum_bytes is not None and maximum_bytes <= 0:
        raise ValueError("maximum_bytes 必须大于 0")

    known = set(seen_ids)
    events: list[ClaudeUsageEvent] = []
    project: str | None = None
    bytes_read = 0
    reached_eof = True
    line_buffer = b""
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            while True:
                if maximum_bytes is not None and bytes_read >= maximum_bytes:
                    # 预算用尽：文件还没读完，下一轮从 next_offset 继续。
                    reached_eof = False
                    break
                chunk = handle.read(64 * 1024)
                if not chunk:
                    # 物理 EOF：即使最后一行还没写完，也认为这一轮读完了；
                    # 文件继续追加时由偏移量决定要不要重读那一行。
                    reached_eof = True
                    break
                bytes_read += len(chunk)
                line_buffer += chunk
                while True:
                    newline = line_buffer.find(b"\n")
                    if newline < 0:
                        break
                    raw_line = line_buffer[:newline]
                    line_buffer = line_buffer[newline + 1 :]
                    if discarding_oversized_line:
                        discarding_oversized_line = False
                        continue
                    event, note = _parse_claude_line(raw_line, known)
                    if note == "oversized":
                        discarding_oversized_line = True
                    elif event is not None:
                        if project is None and event.project is not None:
                            project = event.project
                        if event.message_id:
                            known.add(event.message_id)
                        events.append(event)
                if len(line_buffer) > _MAX_LINE_BYTES:
                    line_buffer = b""
                    discarding_oversized_line = True
    except OSError:
        return ClaudeParseResult(
            next_offset=offset,
            events=(),
            project=None,
            seen_ids=tuple(seen_ids),
            bytes_read=0,
            reached_eof=False,
            discarding_oversized_line=discarding_oversized_line,
        )
    next_offset = offset + bytes_read
    if line_buffer and not discarding_oversized_line:
        # 中文注释：最后一行没有换行说明还在写，退回去等下一次读取；
        # 文件本轮仍视为读完，否则索引永远无法进入 complete 状态。
        next_offset -= len(line_buffer)
    recent = tuple(list(seen_ids) + [item.message_id for item in events if item.message_id])
    return ClaudeParseResult(
        next_offset=next_offset,
        events=tuple(events),
        project=project,
        seen_ids=recent[-_MAX_SEEN_IDS:],
        bytes_read=bytes_read,
        reached_eof=reached_eof,
        discarding_oversized_line=discarding_oversized_line,
    )


def _parse_claude_line(
    raw_line: bytes,
    known: set[str],
) -> tuple[ClaudeUsageEvent | None, str | None]:
    """解析一行 Claude 会话记录；返回事件和可选的处理提示。"""

    if not raw_line.strip():
        return None, None
    if len(raw_line) > _MAX_LINE_BYTES:
        return None, "oversized"
    if b'"usage"' not in raw_line or b'"assistant"' not in raw_line:
        return None, None
    try:
        event = json.loads(raw_line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None, None
    if not isinstance(event, Mapping) or event.get("type") != "assistant":
        return None, None
    message = event.get("message")
    if not isinstance(message, Mapping):
        return None, None
    usage = message.get("usage")
    if not isinstance(usage, Mapping):
        return None, None
    message_id = _text(message.get("id"))
    if message_id and message_id in known:
        # 中文注释：同一 message.id 会被重复写入（流式/重放），只计一次。
        return None, None
    model = _text(message.get("model")) or "未知模型"
    if model.startswith("<"):
        return None, None
    cached = _int(usage.get("cache_read_input_tokens"))
    cache_write = _int(usage.get("cache_creation_input_tokens"))
    raw_input = _int(usage.get("input_tokens"))
    output = _int(usage.get("output_tokens"))
    # 中文注释：Claude 的 input_tokens 不含缓存部分，这里换算成本项目
    # 「input_tokens = 总输入」的口径，成本计算才能正确拆分缓存。
    total_input = raw_input + cached + cache_write
    total_tokens = total_input + output
    if total_tokens <= 0:
        return None, None
    timestamp = _timestamp(event.get("timestamp")) or time.time()
    return (
        ClaudeUsageEvent(
            timestamp=timestamp,
            model=model,
            project=_text(event.get("cwd")),
            input_tokens=total_input,
            cached_input_tokens=cached,
            cache_write_input_tokens=cache_write,
            output_tokens=output,
            total_tokens=total_tokens,
            message_id=message_id,
        ),
        None,
    )


def _config_file(home: Path) -> Path:
    """返回 Claude Code 身份配置文件的位置。"""

    return Path(home).expanduser().parent / ".claude.json"


def _read_json_object(path: Path) -> Mapping[str, Any] | None:
    """读取 JSON 对象；文件缺失或损坏时返回 None。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _int(value: Any) -> int:
    """把 token 数转成非负整数。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _text(value: Any) -> str | None:
    """把可选字段转成去空白的字符串。"""

    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _timestamp(value: Any) -> float | None:
    """解析 ISO8601 时间戳。"""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _normalize_path(path: Path) -> Path:
    """展开用户目录并尽量解析真实路径。"""

    expanded = Path(path).expanduser()
    try:
        return expanded.resolve()
    except OSError:
        return expanded


_POLICY_LOCK = threading.Lock()
_POLICY_CACHE: dict[Path, tuple[float, bool]] = {}
