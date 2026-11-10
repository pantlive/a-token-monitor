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
import logging
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .agents import scan_running_agents
from .health import sanitize_error
from .multi_models import DetectionConfidence, SessionStatus, TrackedSession
from .quota import QuotaSnapshot, QuotaWindow

_LOGGER = logging.getLogger(__name__)

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

# 中文注释：transcript 最近这么多秒内被写过，就当作活动会话（进程证据缺失时兜底）。
# 中文注释：Claude Code 一轮对话里可能几分钟才写一次，90 秒太短，按 15 分钟判断。
_FILE_ACTIVITY_SECONDS = 900.0


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
    agents = scan_running_agents(
        proc_root=proc_root,
        products=("claude",),
        session_roots=(projects_root,),
    )
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
    # 中文注释：Claude Code 是 Node 进程，写完一行就关掉句柄，采样瞬间
    # ``/proc/<pid>/fd`` 里常常看不到 transcript；这时按「文件刚被写过」兜底，
    # 标成 RECENT_FILE，避免正在用的会话显示成 0 个活动。
    covered = {item.session_id for item in sessions}
    try:
        candidates = sorted(
            projects_root.glob("**/*.jsonl"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        candidates = []
    for transcript in candidates:
        session_id = claude_session_id_from_open_path(transcript, projects_root)
        if session_id is None or session_id in covered:
            continue
        header = _read_claude_header(transcript)
        modified = header["modified_at"] or 0.0
        if observed_at - modified > _FILE_ACTIVITY_SECONDS:
            break
        covered.add(session_id)
        sessions.append(
            TrackedSession(
                thread_id=f"claude:{session_id}",
                session_id=session_id,
                jsonl_path=str(transcript),
                cwd=header["project"],
                source="claude-file-activity",
                status=SessionStatus.RUNNING,
                confidence=DetectionConfidence.RECENT_FILE,
                first_seen_at=header["started_at"] or modified,
                last_seen_at=observed_at,
                last_event_at=modified,
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
    completed_line = False
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            while True:
                if (
                    maximum_bytes is not None
                    and bytes_read >= maximum_bytes
                    and (completed_line or not line_buffer)
                ):
                    # 预算用尽：文件还没读完，下一轮从 next_offset 继续（末尾半行退回）。
                    # 本轮一整行都还没读完时不能停：长于预算的行（例如带截图的工具
                    # 结果）会让偏移每轮退回原处，索引永远停在未完成。此时继续读完
                    # 这一行，超过 _MAX_LINE_BYTES 的行仍按丢弃处理。
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
                    completed_line = True
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
    """返回 Claude Code 身份配置文件的位置。

    用 ``CLAUDE_CONFIG_DIR`` 另开的数据目录（例如 ``~/.claude-work``）把
    ``.claude.json`` 写在目录内；默认的 ``~/.claude`` 则写在上一级
    ``~/.claude.json``。目录内有就用目录内的，否则多个目录会读到同一份
    身份，被 Dashboard 合并成一个账号。
    """

    path = Path(home).expanduser()
    inside = path / ".claude.json"
    if inside.is_file():
        return inside
    return path.parent / ".claude.json"


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


# ---------------------------------------------------------------- 订阅额度
#
# Claude Code 没有公开的额度接口；它的 /usage 命令读取的是未公开的
# https://api.anthropic.com/api/oauth/usage（社区抓包发现，上游可能变动）。
# 请求用本地 OAuth access token 做 Bearer 鉴权：Linux / Windows 读
# ``~/.claude/.credentials.json``，macOS 读 Keychain 的
# 「Claude Code-credentials」条目。access token 只用于鉴权，refresh token
# 只用于续期和本机凭据写回；额度返回值及日志不含 token。该接口限流很激进，
# 所以成功结果缓存 5 分钟、失败结果缓存 1 分钟，429 另退避 10–30 分钟。
# 文件凭据在到期前 5 分钟持 Claude Code 同款目录锁主动续期、原子写回；
# Keychain 只读。失败时最多保留 6 小时的旧快照，并保留真实观测时间。

_CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CLAUDE_USAGE_BETA = "oauth-2025-04-20"
_CLAUDE_USAGE_TIMEOUT_SECONDS = 8.0
_CLAUDE_CREDENTIALS_NAME = ".credentials.json"
_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
_CLAUDE_KEYCHAIN_TIMEOUT_SECONDS = 5.0
_CLAUDE_MAX_RESPONSE_BYTES = 256 * 1024
# 中文注释：OAuth 地址和公开客户端 ID 来自社区抓包，上游可能变动；并非密钥。
_CLAUDE_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
_CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_CLAUDE_REFRESH_THRESHOLD_SECONDS = 300.0
_CLAUDE_REFRESH_TIMEOUT_SECONDS = 10.0
_CLAUDE_LOCK_RETRIES = 120
# 中文注释：采用 proper-lockfile 默认 10 秒，而非 Kimi 定制的 5 秒，
# 避免误删 Claude Code 每 5 秒才保活一次的有效锁。
_CLAUDE_LOCK_STALE_SECONDS = 10.0
_CLAUDE_LOCK_MIN_DELAY_SECONDS = 0.5
_CLAUDE_LOCK_MAX_DELAY_SECONDS = 1.0
_CLAUDE_LAST_GOOD_TTL_SECONDS = 6 * 3600.0
_CLAUDE_BACKOFF_INITIAL_SECONDS = 600.0
_CLAUDE_BACKOFF_MAX_SECONDS = 1800.0
_QUOTA_CACHE_SUCCESS_TTL_SECONDS = 300.0
_QUOTA_CACHE_FAILURE_TTL_SECONDS = 60.0

_HttpJsonCaller = Callable[[str, Mapping[str, str], float], tuple[int, Any]]
_HttpPostJsonCaller = Callable[[str, Mapping[str, Any], float], tuple[int, Any]]


class _QuotaFailure(Enum):
    """安全的固定失败原因，可直接用于日志和页面提示。"""

    CREDENTIALS_EXPIRED = "credentials_expired"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"


_quota_cache: dict[Path, tuple[float, QuotaSnapshot | None]] = {}
_quota_cache_lock = threading.Lock()
_quota_request_locks: dict[Path, Any] = {}
_last_good: dict[Path, QuotaSnapshot] = {}
_backoff_until: dict[Path, tuple[float, float]] = {}
_quota_logged: set[tuple[Path, str]] = set()


def read_claude_quota(
    home: Path,
    now: float | None = None,
    *,
    http_get_json: _HttpJsonCaller | None = None,
    keychain_reader: Callable[[], Mapping[str, Any] | None] | None = None,
    http_post_json: _HttpPostJsonCaller | None = None,
    sleep: Callable[[float], None] = time.sleep,
    allow_refresh: bool = True,
) -> QuotaSnapshot | None:
    """读取额度并主动续期文件凭据；失败时返回带标记的最后成功值。"""

    root = _normalize_path(home)
    moment = time.time() if now is None else now
    # 中文注释：同一 home 串行请求，避免并发轮询穿透 429 退避；不同 home 互不阻塞。
    with _quota_cache_lock:
        request_lock = _quota_request_locks.setdefault(root, threading.Lock())
    with request_lock:
        return _read_claude_quota_locked(
            root,
            moment,
            http_get_json=http_get_json,
            keychain_reader=keychain_reader,
            http_post_json=http_post_json,
            sleep=sleep,
            allow_refresh=allow_refresh,
        )


def _read_claude_quota_locked(
    root: Path,
    moment: float,
    *,
    http_get_json: _HttpJsonCaller | None,
    keychain_reader: Callable[[], Mapping[str, Any] | None] | None,
    http_post_json: _HttpPostJsonCaller | None,
    sleep: Callable[[float], None],
    allow_refresh: bool,
) -> QuotaSnapshot | None:
    """持单账号请求锁执行缓存、续期、退避及降级。"""

    use_cache = (
        http_get_json is None and keychain_reader is None and http_post_json is None
    )
    with _quota_cache_lock:
        backoff = _backoff_until.get(root)
    if backoff is not None and moment < backoff[0]:
        return _stale_claude_quota(root, moment, _QuotaFailure.RATE_LIMITED)
    if use_cache:
        with _quota_cache_lock:
            cached = _quota_cache.get(root)
        if cached is not None:
            cached_at, cached_value = cached
            ttl = (
                _QUOTA_CACHE_SUCCESS_TTL_SECONDS
                if cached_value is not None and not cached_value.metadata.get("stale")
                else _QUOTA_CACHE_FAILURE_TTL_SECONDS
            )
            if moment - cached_at < ttl:
                if cached_value is not None and cached_value.metadata.get("stale"):
                    return _stale_claude_quota(
                        root,
                        moment,
                        _QuotaFailure(cached_value.metadata["stale_reason"]),
                    )
                return cached_value
    try:
        snapshot, reason = _fetch_claude_quota(
            root,
            moment,
            http_get_json=http_get_json or _http_get_json,
            keychain_reader=keychain_reader or _read_macos_keychain,
            http_post_json=http_post_json or _http_post_json,
            sleep=sleep,
            allow_refresh=allow_refresh,
        )
    except Exception as error:  # noqa: BLE001 - 额度是可选增强，任何意外都不能让监控崩溃
        # 中文注释：sanitize_error 不会过滤任意 token，故只记录脱敏后的异常类型。
        _log_quota_once(
            root,
            "unexpected",
            sanitize_error(type(error).__name__),
            level=logging.WARNING,
        )
        snapshot = None
        reason = _QuotaFailure.UNAVAILABLE
    if snapshot is not None:
        with _quota_cache_lock:
            _last_good[root] = snapshot
            _backoff_until.pop(root, None)
    else:
        reason = reason or _QuotaFailure.UNAVAILABLE
        _log_quota_once(root, reason.value, reason.value)
        if reason is _QuotaFailure.RATE_LIMITED:
            with _quota_cache_lock:
                previous = _backoff_until.get(root)
                delay = min(
                    previous[1] * 2 if previous else _CLAUDE_BACKOFF_INITIAL_SECONDS,
                    _CLAUDE_BACKOFF_MAX_SECONDS,
                )
                _backoff_until[root] = (moment + delay, delay)
        snapshot = _stale_claude_quota(root, moment, reason)
    if use_cache:
        with _quota_cache_lock:
            _quota_cache[root] = (moment, snapshot)
    return snapshot


def _log_quota_once(
    home: Path,
    event: str,
    message: str,
    *,
    level: int = logging.INFO,
) -> None:
    """每个账号每类事件只记一次，不记录账号路径、凭据或响应正文。"""

    with _quota_cache_lock:
        key = (home, event)
        if key in _quota_logged:
            return
        _quota_logged.add(key)
    _LOGGER.log(level, "Claude Code 额度: %s", message)


def _stale_claude_quota(
    home: Path,
    now: float,
    reason: _QuotaFailure,
) -> QuotaSnapshot | None:
    """过滤失效窗口并复制旧快照，不把旧值伪装成本轮观测值。"""

    with _quota_cache_lock:
        snapshot = _last_good.get(home)
    if snapshot is None or now - snapshot.observed_at > _CLAUDE_LAST_GOOD_TTL_SECONDS:
        return None
    windows = tuple(
        window
        for window in snapshot.windows
        if window.resets_at is None or window.resets_at > now
    )
    if not windows:
        return None
    return replace(
        snapshot,
        windows=windows,
        raw_limit_ids=tuple(window.limit_id for window in windows),
        metadata={**snapshot.metadata, "stale": "1", "stale_reason": reason.value},
    )


def _clear_quota_cache() -> None:
    """清空短期请求缓存，保留退避记录和最后一次成功值。"""

    with _quota_cache_lock:
        _quota_cache.clear()


def _clear_quota_backoff() -> None:
    """重置跨请求状态，仅供测试；手动刷新不能绕过退避。"""

    with _quota_cache_lock:
        _backoff_until.clear()
        _last_good.clear()
        _quota_logged.clear()


def _fetch_claude_quota(
    home: Path,
    now: float,
    *,
    http_get_json: _HttpJsonCaller,
    keychain_reader: Callable[[], Mapping[str, Any] | None],
    http_post_json: _HttpPostJsonCaller,
    sleep: Callable[[float], None],
    allow_refresh: bool,
) -> tuple[QuotaSnapshot | None, _QuotaFailure | None]:
    """续期并请求额度，最多续期一次、最多重发一次用量请求。"""

    oauth, source = _read_claude_oauth(home, keychain_reader)
    if oauth is None:
        return None, _QuotaFailure.CREDENTIALS_EXPIRED
    can_refresh = allow_refresh and source == "file"
    refreshed = False
    if can_refresh and not _claude_token_fresh(oauth, now):
        updated = _refresh_claude_credentials(
            home,
            oauth,
            now,
            http_post_json=http_post_json,
            sleep=sleep,
        )
        if updated is None:
            return None, _QuotaFailure.CREDENTIALS_EXPIRED
        oauth = updated
        refreshed = True
    for attempt in range(2):
        access_token = oauth.get("accessToken")
        if not isinstance(access_token, str) or not access_token:
            return None, _QuotaFailure.CREDENTIALS_EXPIRED
        status, payload = http_get_json(
            _CLAUDE_USAGE_URL,
            {
                "Authorization": f"Bearer {access_token}",
                "anthropic-beta": _CLAUDE_USAGE_BETA,
                "Accept": "application/json",
            },
            _CLAUDE_USAGE_TIMEOUT_SECONDS,
        )
        if status not in (401, 403):
            break
        if status != 401 or not can_refresh or refreshed or attempt:
            return None, _QuotaFailure.CREDENTIALS_EXPIRED
        updated = _refresh_claude_credentials(
            home,
            oauth,
            now,
            http_post_json=http_post_json,
            sleep=sleep,
            force=True,
        )
        if updated is None:
            return None, _QuotaFailure.CREDENTIALS_EXPIRED
        oauth = updated
        refreshed = True
    if status == 429:
        return None, _QuotaFailure.RATE_LIMITED
    if status != 200:
        return None, _QuotaFailure.UNAVAILABLE
    snapshot = _parse_claude_usage(
        payload,
        observed_at=now,
        plan_type=_text(oauth.get("subscriptionType")),
    )
    return snapshot, None if snapshot is not None else _QuotaFailure.UNAVAILABLE


def _read_claude_oauth(
    home: Path,
    keychain_reader: Callable[[], Mapping[str, Any] | None],
) -> tuple[Mapping[str, Any] | None, str]:
    """返回 OAuth 段及来源；文件缺失时退回只读的 macOS Keychain。"""

    payload = _read_json_object(home / _CLAUDE_CREDENTIALS_NAME)
    source = "file"
    if payload is None:
        payload = keychain_reader()
        source = "keychain"
    if payload is None:
        return None, source
    oauth = payload.get("claudeAiOauth")
    return (oauth if isinstance(oauth, Mapping) else None), source


def _finite_number(value: Any) -> float | None:
    """只接受有限数字，避免异常过期时间触发错误写回。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _claude_token_fresh(oauth: Mapping[str, Any], now: float) -> bool:
    """expiresAt 为毫秒；没有到期字段时继续使用已有 access token。"""

    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        return False
    expires_at = _finite_number(oauth.get("expiresAt"))
    return (
        expires_at is None
        or expires_at / 1000 > now + _CLAUDE_REFRESH_THRESHOLD_SECONDS
    )


def _write_claude_credentials(home: Path, payload: Mapping[str, Any]) -> None:
    """在原目录中以 0600 权限原子替换完整凭据，异常时清理临时文件。"""

    descriptor, temporary = tempfile.mkstemp(
        dir=home, prefix=".claude-oauth-", suffix=".tmp"
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False)
            handle.write("\n")
        temporary_path.chmod(0o600)
        temporary_path.replace(home / _CLAUDE_CREDENTIALS_NAME)
    finally:
        temporary_path.unlink(missing_ok=True)


def _refresh_claude_credentials(
    home: Path,
    oauth: Mapping[str, Any],
    now: float,
    *,
    http_post_json: _HttpPostJsonCaller,
    sleep: Callable[[float], None],
    force: bool = False,
) -> Mapping[str, Any] | None:
    """持兼容目录锁续期并保存轮换凭据；任何失败均不重试。"""

    lock = _ClaudeRefreshLock(home, sleep)
    succeeded = False
    try:
        if not lock.acquire():
            return None
        # 中文注释：绝不回退到锁前凭据，避免使用已经被其它进程轮换的 refresh token。
        original = _read_json_object(home / _CLAUDE_CREDENTIALS_NAME)
        latest = original.get("claudeAiOauth") if original else None
        if not isinstance(latest, Mapping):
            return None
        if _claude_token_fresh(latest, now) and (
            not force or latest.get("accessToken") != oauth.get("accessToken")
        ):
            succeeded = True
            return latest
        refresh_token = latest.get("refreshToken")
        if not isinstance(refresh_token, str) or not refresh_token or not lock.touch():
            return None
        status, data = http_post_json(
            _CLAUDE_TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": _CLAUDE_CLIENT_ID,
            },
            _CLAUDE_REFRESH_TIMEOUT_SECONDS,
        )
        if status != 200 or not isinstance(data, Mapping):
            return None
        access_token = data.get("access_token")
        new_refresh = data.get("refresh_token", refresh_token)
        expires_in = _finite_number(data.get("expires_in"))
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(new_refresh, str)
            or not new_refresh
            or expires_in is None
            or expires_in <= 0
        ):
            return None
        # 中文注释：只替换三项凭据字段，保留 scopes、套餐及所有未知字段。
        updated = dict(latest)
        updated.update(
            accessToken=access_token,
            refreshToken=new_refresh,
            expiresAt=int((now + expires_in) * 1000),
        )
        # 中文注释：锁丢失或有不遵守锁的写入者时不覆盖文件，也不重发续期请求。
        if (
            not lock.touch()
            or _read_json_object(home / _CLAUDE_CREDENTIALS_NAME) != original
        ):
            return None
        _write_claude_credentials(home, {**original, "claudeAiOauth": updated})
        succeeded = True
        return updated
    except Exception as error:  # noqa: BLE001 - 凭据操作必须失败关闭
        _log_quota_once(home, "refresh_exception", sanitize_error(type(error).__name__))
        return None
    finally:
        lock.release()
        event = "refresh_success" if succeeded else "refresh_failed"
        _log_quota_once(home, event, event)


class _ClaudeRefreshLock:
    """与 proper-lockfile 的 lock(configDir) 默认路径兼容的目录锁。

    mkdir 原子取锁，10 秒陈旧判定；心跳贯穿 HTTP 请求，避免 10 秒请求期间
    被误抢占。持有目录的身份或 mtime 改变时停止写回，也不删除新持有者的锁。
    """

    def __init__(
        self,
        home: Path,
        sleep: Callable[[float], None],
        stale_seconds: float = _CLAUDE_LOCK_STALE_SECONDS,
        retries: int = _CLAUDE_LOCK_RETRIES,
    ) -> None:
        self._lock_dir = home.with_name(home.name + ".lock")
        self._sleep = sleep
        self._stale_seconds = stale_seconds
        self._retries = retries
        self._identity: tuple[int, int, int] | None = None
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None

    def _stat_identity(self) -> tuple[int, int, int]:
        """同时检查目录 inode 和 mtime，识别锁已被其它持有者替换。"""

        stat = self._lock_dir.stat()
        return stat.st_dev, stat.st_ino, stat.st_mtime_ns

    def acquire(self) -> bool:
        """有限次等待目录锁；拿不到锁时绝不发起续期。"""

        for _ in range(self._retries):
            try:
                self._lock_dir.mkdir()
            except FileExistsError:
                self._break_if_stale()
            except OSError:
                return False
            else:
                try:
                    self._identity = self._stat_identity()
                except OSError:
                    return False
                self._heartbeat = threading.Thread(target=self._renew, daemon=True)
                self._heartbeat.start()
                return True
            self._sleep(
                random.uniform(
                    _CLAUDE_LOCK_MIN_DELAY_SECONDS,
                    _CLAUDE_LOCK_MAX_DELAY_SECONDS,
                )
            )
        return False

    def _break_if_stale(self) -> None:
        """仅清理确认陈旧且未被续期的空锁目录。"""

        try:
            identity = self._stat_identity()
            if identity[2] / 1e9 >= time.time() - self._stale_seconds:
                return
            if self._stat_identity() == identity:
                self._lock_dir.rmdir()
        except OSError:
            pass

    def _renew(self) -> None:
        """请求阻塞时仍持续更新 mtime，退出时及时停止。"""

        interval = self._stale_seconds / 2
        while not self._stop.wait(interval):
            if not self.touch():
                return

    def touch(self) -> bool:
        """确认仍持有原锁后更新时间；失败后禁止继续使用此锁。"""

        with self._guard:
            if self._identity is None:
                return False
            try:
                if self._stat_identity() != self._identity:
                    self._identity = None
                    return False
                # 中文注释：pathlib 尚无 utime；仅此处用 os 更新目录 mtime。
                os.utime(self._lock_dir, None)
                self._identity = self._stat_identity()
                return True
            except OSError:
                self._identity = None
                return False

    def release(self) -> None:
        """停止心跳，仅删除自己仍持有的锁目录。"""

        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join()
        with self._guard:
            try:
                if (
                    self._identity is not None
                    and self._stat_identity() == self._identity
                ):
                    self._lock_dir.rmdir()
            except OSError:
                pass
            self._identity = None


def _read_macos_keychain() -> Mapping[str, Any] | None:
    """从 macOS Keychain 读取 Claude Code 的 OAuth 凭据（其它平台返回 None）。"""

    if sys.platform != "darwin" or shutil.which("security") is None:
        return None
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                _CLAUDE_KEYCHAIN_SERVICE,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=_CLAUDE_KEYCHAIN_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, Mapping) else None


def _parse_claude_usage(
    payload: Any,
    *,
    observed_at: float,
    plan_type: str | None = None,
) -> QuotaSnapshot | None:
    """解析 usage 接口返回的 ``five_hour`` / ``seven_day`` 等窗口。"""

    data = payload if isinstance(payload, Mapping) else {}
    windows = [
        window
        for window in (
            _usage_window("claude", "5-hour", 300.0, data.get("five_hour")),
            _usage_window("claude", "Weekly", 10080.0, data.get("seven_day")),
            _usage_window(
                "claude-design",
                "Claude Design（周）",
                10080.0,
                data.get("seven_day_omelette"),
            ),
        )
        if window is not None
    ]
    if not windows:
        return None
    return QuotaSnapshot(
        observed_at=observed_at,
        windows=tuple(windows),
        plan_type=plan_type,
        source="claude-oauth",
        raw_limit_ids=tuple(window.limit_id for window in windows),
    )


def _usage_window(
    limit_id: str,
    name: str,
    minutes: float,
    value: Any,
) -> QuotaWindow | None:
    """把 ``{"utilization": 21.0, "resets_at": "..."}`` 转成额度窗口。"""

    data = value if isinstance(value, Mapping) else {}
    raw_used = data.get("utilization")
    used_percent = (
        float(raw_used)
        if isinstance(raw_used, (int, float)) and not isinstance(raw_used, bool)
        else None
    )
    resets_at = _timestamp(data.get("resets_at"))
    if used_percent is None and resets_at is None:
        return None
    return QuotaWindow(
        limit_id=limit_id,
        name=name,
        used_percent=used_percent,
        window_minutes=minutes,
        resets_at=resets_at,
    )


def _http_get_json(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
) -> tuple[int, Any]:
    """GET 一个 JSON 接口；所有网络错误都收敛为 ``(0, None)``。"""

    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _read_json_body(response)
    except urllib.error.HTTPError as error:
        return error.code, _read_json_body(error)
    except OSError:
        return 0, None


def _http_post_json(
    url: str,
    payload: Mapping[str, Any],
    timeout: float,
) -> tuple[int, Any]:
    """POST JSON；限制响应大小且不记录请求、响应或网络异常正文。"""

    request = urllib.request.Request(
        url,
        data=json.dumps(dict(payload)).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _read_json_body(response)
    except urllib.error.HTTPError as error:
        return error.code, _read_json_body(error)
    except OSError:
        return 0, None


def _read_json_body(response: Any) -> Any:
    try:
        body = response.read(_CLAUDE_MAX_RESPONSE_BYTES + 1)
    except OSError:
        return None
    if len(body) > _CLAUDE_MAX_RESPONSE_BYTES:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
