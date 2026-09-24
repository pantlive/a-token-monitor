"""Grok CLI 本地用量、会话元数据和额度快照。

数据来自 ``GROK_HOME/logs/unified.jsonl`` 的 ``shell.turn.inference_done``
和 ``billing: fetched credits config``。只读取身份字段和 token 计数，
不读取 access/refresh token，也不读取会话正文或工具输出。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from urllib.parse import unquote

from .agents import scan_running_agents
from .multi_models import DetectionConfidence, SessionStatus, TrackedSession
from .quota import QuotaSnapshot, QuotaWindow


_GROK_INFERENCE_DONE = b"shell.turn.inference_done"
_GROK_BILLING = b"billing: fetched credits config"
_MAX_BILLING_TAIL_BYTES = 256 * 1024
_MAX_LINE_BYTES = 64 * 1024
_TOKEN_FIELDS = (
    "prompt_tokens",
    "cached_prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
)


@dataclass(frozen=True)
class GrokAccount:
    """一个 GROK_HOME 的安全身份信息。"""

    home: Path
    account_id: str | None
    display_name: str
    profile_name: str = "grok"

    @property
    def account_key(self) -> str:
        """返回优先使用真实用户 ID 的归组键。"""

        return self.account_id or f"profile:{self.profile_name}"


@dataclass(frozen=True)
class GrokSessionInfo:
    """从 summary.json 提取的会话模型、项目和目录。"""

    session_id: str
    model: str | None
    cwd: str | None
    directory: Path | None = None
    created_at: float | None = None
    updated_at: float | None = None


@dataclass(frozen=True)
class GrokUsageEvent:
    """一次 Grok 模型请求的 token 用量。"""

    timestamp: float
    model: str
    project: str | None
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class GrokLogParseResult:
    """unified.jsonl 一段追加内容的解析结果。"""

    next_offset: int
    events: tuple[GrokUsageEvent, ...]
    bytes_read: int
    reached_eof: bool
    discarding_oversized_line: bool


def default_grok_home() -> Path:
    """返回 Grok 默认主目录。"""

    configured = os.environ.get("GROK_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".grok"


def resolve_grok_homes(homes: Sequence[Path] | None = None) -> tuple[Path, ...]:
    """解析要扫描的 GROK_HOME；未传入时若默认目录存在则使用它。"""

    if homes is not None:
        unique: list[Path] = []
        seen: set[Path] = set()
        for home in homes:
            normalized = _normalize_path(home)
            if normalized in seen:
                continue
            seen.add(normalized)
            unique.append(normalized)
        return tuple(unique)
    default_home = _normalize_path(default_grok_home())
    if default_home.is_dir():
        return (default_home,)
    return ()


def grok_unified_log(grok_home: Path) -> Path:
    """返回一个 GROK_HOME 的统一日志路径。"""

    return _normalize_path(grok_home) / "logs" / "unified.jsonl"


def read_grok_account(grok_home: Path) -> GrokAccount:
    """读取 Grok 用户 ID 和显示名，忽略令牌字段。"""

    home = _normalize_path(grok_home)
    auth_path = home / "auth.json"
    account_id: str | None = None
    display_name = "grok"
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, Mapping):
        chosen = _choose_auth_record(payload)
        if chosen is not None:
            account_id = _text(chosen.get("user_id")) or _text(
                chosen.get("principal_id")
            )
            email = _text(chosen.get("email"))
            first_name = _text(chosen.get("first_name"))
            last_name = _text(chosen.get("last_name"))
            named = " ".join(part for part in (first_name, last_name) if part)
            display_name = email or named or account_id or "grok"
    return GrokAccount(
        home=home,
        account_id=account_id,
        display_name=display_name,
    )


def load_session_index(grok_home: Path) -> dict[str, GrokSessionInfo]:
    """扫描 summary.json，建立 session id 到模型和项目的映射。"""

    index: dict[str, GrokSessionInfo] = {}
    sessions_root = _normalize_path(grok_home) / "sessions"
    try:
        summaries = sessions_root.rglob("summary.json")
    except OSError:
        return index
    for path in summaries:
        session_id = path.parent.name
        if not session_id:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        info = payload.get("info")
        cwd = None
        if isinstance(info, Mapping):
            cwd = _text(info.get("cwd"))
        model = _text(payload.get("current_model_id"))
        if model is None:
            model = _model_from_signals(path.with_name("signals.json"))
        index[session_id] = GrokSessionInfo(
            session_id=session_id,
            model=model,
            cwd=cwd,
            directory=path.parent,
            created_at=_timestamp(payload.get("created_at")),
            updated_at=_timestamp(
                payload.get("updated_at") or payload.get("last_active_at")
            ),
        )
    return index


def decode_grok_project(name: str) -> str | None:
    """把会话目录名（URL 编码的项目路径）还原成项目路径。"""

    text = name.strip()
    if not text:
        return None
    try:
        decoded = unquote(text)
    except (TypeError, ValueError):  # pragma: no cover - unquote 很少失败
        return None
    if not decoded.startswith("/"):
        return None
    return decoded


def list_grok_active_sessions(
    grok_home: Path,
    proc_root: Path | None = None,
    now: float | None = None,
) -> tuple[TrackedSession, ...]:
    """列出当前有 Grok CLI 进程打开会话文件的会话。

    参照 Kimi / DSH 的做法：以 ``/proc/<pid>/fd`` 里实际打开的会话文件为准，
    同一个会话被多个进程打开时合并 pids；进程退出后会话自然从列表消失。
    进程只打开被轮转或删除的旧路径时，仍按路径里的会话目录报告。
    """

    home = _normalize_path(grok_home)
    sessions_root = home / "sessions"
    observed_at = time.time() if now is None else float(now)
    agents = scan_running_agents(
        proc_root=proc_root,
        products=("grok",),
        session_roots=(sessions_root,),
    )
    index = load_session_index(home)
    grouped: dict[str, list[int]] = {}
    open_by_session: dict[str, Path] = {}
    directory_by_session: dict[str, Path] = {}
    for agent in agents:
        for path in agent.open_paths:
            located = _grok_session_from_path(path, sessions_root)
            if located is None:
                continue
            session_id, directory = located
            grouped.setdefault(session_id, [])
            if agent.pid not in grouped[session_id]:
                grouped[session_id].append(agent.pid)
            open_by_session.setdefault(session_id, path)
            directory_by_session.setdefault(session_id, directory)
        if agent.cwd is not None:
            # 中文注释：CLI 有时不持有会话文件句柄，退回按工作目录匹配。
            cwd_text = str(agent.cwd)
            for session_id, info in index.items():
                if info.cwd and _same_path(info.cwd, cwd_text):
                    grouped.setdefault(session_id, [])
                    if agent.pid not in grouped[session_id]:
                        grouped[session_id].append(agent.pid)
                    if info.directory is not None:
                        directory_by_session.setdefault(session_id, info.directory)

    sessions: list[TrackedSession] = []
    for session_id, pids in grouped.items():
        info = index.get(session_id)
        directory = directory_by_session.get(session_id)
        if directory is None and info is not None:
            directory = info.directory
        project = (
            decode_grok_project(directory.parent.name)
            if directory is not None
            else None
        )
        cwd = (info.cwd if info is not None else None) or project
        last_event_at, last_event_type = _grok_session_activity(directory)
        if last_event_at is None and info is not None:
            last_event_at = info.updated_at or info.created_at
        log_path = _grok_session_log(directory, open_by_session.get(session_id))
        sessions.append(
            TrackedSession(
                thread_id=f"grok:{session_id}",
                session_id=session_id,
                jsonl_path=str(log_path) if log_path is not None else None,
                cwd=cwd,
                source="grok-cli",
                status=SessionStatus.RUNNING,
                confidence=DetectionConfidence.OPEN_FILE,
                first_seen_at=(
                    (info.created_at if info is not None else None)
                    or last_event_at
                    or observed_at
                ),
                last_seen_at=observed_at,
                pids=tuple(sorted(pids)),
                last_event_at=last_event_at,
                last_event_type=last_event_type,
                product="grok",
                model=info.model if info is not None else None,
                project=project,
            )
        )
    sessions.sort(key=lambda item: item.last_seen_at, reverse=True)
    return tuple(sessions)


def _grok_session_from_path(
    path: Path,
    sessions_root: Path,
) -> tuple[str, Path] | None:
    """从打开的路径解析出会话 ID 和会话目录。"""

    normalized = _normalize_path(path)
    root = _normalize_path(sessions_root)
    if root not in normalized.parents:
        return None
    relative = normalized.relative_to(root)
    parts = relative.parts
    if len(parts) < 2:
        return None
    session_id = parts[1]
    if not session_id:
        return None
    return session_id, root / parts[0] / session_id


def _grok_session_activity(
    directory: Path | None,
) -> tuple[float | None, str | None]:
    """返回会话目录里最近改动的文件和它的修改时间。"""

    if directory is None or not directory.is_dir():
        return None, None
    latest_at: float | None = None
    latest_name: str | None = None
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        return None, None
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            modified = entry.stat().st_mtime
        except OSError:
            continue
        if latest_at is None or modified > latest_at:
            latest_at = modified
            latest_name = entry.name
    return latest_at, latest_name


def _grok_session_log(
    directory: Path | None,
    open_path: Path | None,
) -> Path | None:
    """优先展示会话的 chat_history.jsonl，其次退回进程实际打开的路径。"""

    if directory is not None:
        for name in ("chat_history.jsonl", "updates.jsonl", "events.jsonl"):
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return open_path


def _same_path(left: str, right: str) -> bool:
    """比较两个路径是否指向同一位置（忽略符号链接差异）。"""

    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except OSError:
        return str(left) == str(right)


def parse_grok_log_chunk(
    path: Path,
    offset: int,
    session_index: Mapping[str, GrokSessionInfo],
    default_model: str,
    discarding_oversized_line: bool = False,
    maximum_bytes: int | None = None,
) -> GrokLogParseResult:
    """从已确认偏移继续解析 unified.jsonl 中的模型请求用量。"""

    if offset < 0:
        raise ValueError("offset 不能小于 0")
    if maximum_bytes is not None and maximum_bytes <= 0:
        raise ValueError("maximum_bytes 必须大于 0")
    events: list[GrokUsageEvent] = []
    next_offset = offset
    bytes_read = 0
    reached_eof = False
    discarding = discarding_oversized_line
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            content = (
                handle.read(maximum_bytes)
                if maximum_bytes is not None
                else handle.read()
            )
            bytes_read = len(content)
            reached_physical_eof = maximum_bytes is None or bytes_read < maximum_bytes
            content_offset = 0
            if discarding:
                first_newline = content.find(b"\n")
                if first_newline < 0:
                    return GrokLogParseResult(
                        next_offset=offset + bytes_read,
                        events=(),
                        bytes_read=bytes_read,
                        reached_eof=reached_physical_eof,
                        discarding_oversized_line=True,
                    )
                content_offset = first_newline + 1
                discarding = False
            remaining = content[content_offset:]
            last_newline = remaining.rfind(b"\n")
            if last_newline < 0:
                if reached_physical_eof:
                    next_offset = offset + content_offset
                    reached_eof = True
                    complete_content = b""
                else:
                    next_offset = offset + bytes_read
                    discarding = True
                    complete_content = b""
            else:
                complete_content = remaining[: last_newline + 1]
                next_offset = offset + content_offset + len(complete_content)
                reached_eof = reached_physical_eof
            for raw_line in complete_content.splitlines(keepends=True):
                if _GROK_INFERENCE_DONE not in raw_line:
                    continue
                if len(raw_line) > _MAX_LINE_BYTES:
                    continue
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                parsed = _usage_event_from_log(
                    event,
                    session_index,
                    default_model,
                )
                if parsed is not None:
                    events.append(parsed)
    except (OSError, UnicodeError):
        return GrokLogParseResult(
            next_offset=offset,
            events=(),
            bytes_read=0,
            reached_eof=False,
            discarding_oversized_line=discarding_oversized_line,
        )
    return GrokLogParseResult(
        next_offset=next_offset,
        events=tuple(events),
        bytes_read=bytes_read,
        reached_eof=reached_eof,
        discarding_oversized_line=discarding,
    )


def read_grok_quota(
    grok_home: Path,
    now: float | None = None,
) -> QuotaSnapshot | None:
    """从 unified.jsonl 尾部读取最近一次 Grok 额度配置。"""

    log_path = grok_unified_log(grok_home)
    try:
        size = log_path.stat().st_size
    except OSError:
        return None
    offset = max(0, size - _MAX_BILLING_TAIL_BYTES)
    try:
        with log_path.open("rb") as handle:
            handle.seek(offset)
            content = handle.read(_MAX_BILLING_TAIL_BYTES)
    except OSError:
        return None
    if offset > 0:
        newline = content.find(b"\n")
        if newline >= 0:
            content = content[newline + 1 :]
    latest: Mapping[str, Any] | None = None
    latest_ts: float | None = None
    for raw_line in content.splitlines():
        if _GROK_BILLING not in raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        if event.get("msg") != "billing: fetched credits config":
            continue
        ctx = event.get("ctx")
        if not isinstance(ctx, Mapping):
            continue
        config = ctx.get("config")
        if not isinstance(config, Mapping):
            continue
        timestamp = _event_timestamp(event) or (now if now is not None else time.time())
        if latest_ts is None or timestamp >= latest_ts:
            latest = ctx
            latest_ts = timestamp
    if latest is None or latest_ts is None:
        return None
    return _quota_from_billing(latest, latest_ts)


def _quota_from_billing(
    ctx: Mapping[str, Any],
    observed_at: float,
) -> QuotaSnapshot | None:
    """把 Grok credits 配置转换成额度窗口。"""

    config = ctx.get("config")
    if not isinstance(config, Mapping):
        return None
    used_percent = _number(config.get("creditUsagePercent"))
    period = config.get("currentPeriod")
    period_end = None
    period_type = None
    if isinstance(period, Mapping):
        period_end = _timestamp(period.get("end"))
        period_type = _text(period.get("type"))
    window_minutes = 10_080.0 if period_type and "WEEKLY" in period_type else None
    plan = _text(ctx.get("subscriptionTier")) or _text(config.get("subscriptionTier"))
    window = QuotaWindow(
        limit_id="grok",
        name="weekly",
        used_percent=used_percent,
        window_minutes=window_minutes,
        resets_at=period_end,
    )
    return QuotaSnapshot(
        observed_at=observed_at,
        windows=(window,),
        plan_type=plan,
        source="grok-unified-log",
        raw_limit_ids=("grok",),
        metadata={"freshness": "latest billing: fetched credits config"},
    )


def _usage_event_from_log(
    event: Mapping[str, Any],
    session_index: Mapping[str, GrokSessionInfo],
    default_model: str,
) -> GrokUsageEvent | None:
    """从 inference_done 日志提取一次请求的 token 计数。"""

    if event.get("msg") != "shell.turn.inference_done":
        return None
    ctx = event.get("ctx")
    if not isinstance(ctx, Mapping):
        return None
    prompt_tokens = _token_int(ctx.get("prompt_tokens"))
    completion_tokens = _token_int(ctx.get("completion_tokens"))
    if prompt_tokens is None and completion_tokens is None:
        return None
    input_tokens = prompt_tokens or 0
    cached_input = _token_int(ctx.get("cached_prompt_tokens")) or 0
    output_tokens = completion_tokens or 0
    reasoning_tokens = _token_int(ctx.get("reasoning_tokens")) or 0
    if input_tokens == 0 and output_tokens == 0:
        return None
    session_id = _text(event.get("sid"))
    session = session_index.get(session_id or "")
    model = (
        _text(event.get("model"))
        or _text(ctx.get("model"))
        or _text(ctx.get("model_id"))
        or (session.model if session is not None else None)
        or default_model
    )
    project = session.cwd if session is not None else None
    timestamp = _event_timestamp(event)
    if timestamp is None:
        return None
    return GrokUsageEvent(
        timestamp=timestamp,
        model=model,
        project=project,
        input_tokens=input_tokens,
        cached_input_tokens=min(cached_input, input_tokens),
        output_tokens=output_tokens,
        reasoning_output_tokens=min(reasoning_tokens, output_tokens),
        total_tokens=input_tokens + output_tokens,
    )


def _choose_auth_record(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """选择一个用户身份记录，跳过令牌字段。"""

    chosen: Mapping[str, Any] | None = None
    chosen_time = ""
    for value in payload.values():
        if not isinstance(value, Mapping):
            continue
        if _text(value.get("principal_type")) not in {None, "User"}:
            continue
        if _text(value.get("user_id")) is None and _text(
            value.get("principal_id")
        ) is None:
            continue
        created = _text(value.get("create_time")) or ""
        if chosen is None or created >= chosen_time:
            chosen = value
            chosen_time = created
    return chosen


def _model_from_signals(path: Path) -> str | None:
    """在 summary 缺少模型时从 signals.json 读取主模型。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    return _text(payload.get("primaryModelId")) or _text(payload.get("primary_model_id"))


def _event_timestamp(event: Mapping[str, Any]) -> float | None:
    """解析 Grok 日志时间戳。"""

    return _timestamp(event.get("ts")) or _timestamp(event.get("timestamp"))


def _timestamp(value: Any) -> float | None:
    """解析 Unix 秒或 ISO 8601 时间。"""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            return number / 1000
        return number
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            number = float(stripped)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        return number / 1000 if number > 10_000_000_000 else number
    return None


def _token_int(value: Any) -> int | None:
    """把 token 字段转换为非负整数。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return int(number)


def _number(value: Any) -> float | None:
    """把额度百分比转换为浮点数。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    """读取非空字符串。"""

    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _normalize_path(path: Path) -> Path:
    """把路径展开为绝对路径。"""

    expanded = path.expanduser()
    try:
        return expanded.resolve()
    except (OSError, RuntimeError):
        return expanded.absolute()
