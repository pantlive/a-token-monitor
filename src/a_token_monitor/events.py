"""Codex JSONL/文本输出解析和额度耗尽识别。

Codex CLI 的事件格式会随版本演进，因此这里采用向后兼容的字段提取方式，
只依赖官方文档明确支持的 JSONL 输出和常见的失败字段，不读取私有数据库。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping


_FAILURE_EVENT_TYPES = {
    "error",
    "stream_error",
    "turn.failed",
    "turn_failed",
    "turn.aborted",
    "turn_aborted",
    "response.failed",
    "response_failed",
    "response.incomplete",
    "response_incomplete",
}

_QUOTA_COMPLETE_EVENT_TYPES = {
    "task_complete",
    "task_completed",
    "turn_completed",
    "turn.complete",
}

_QUOTA_ERROR_CODES = {
    "usage_limit_exceeded",
    "usage_limit_reached",
    "rate_limit_exceeded",
    "rate_limit_reached",
    "quota_exhausted",
    "credits_depleted",
    "credits_exhausted",
}

_CLOCK_RESET = re.compile(
    r"(?:try\s+again|reset)\s+at\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*"
    r"(?P<ampm>a\.?m\.?|p\.?m\.?)?",
    re.IGNORECASE,
)

_RATE_LIMIT_TEXT = re.compile(
    r"(?:"
    r"usage\s+limit|"
    r"rate[- ]limit|"
    r"rate_limit|"
    r"quota|"
    r"out\s+of\s+credits|"
    r"credits?\s+(?:depleted|exhausted)|"
    r"too\s+many\s+requests|"
    r"http\s*429|"
    r"\b429\b"
    r")",
    re.IGNORECASE,
)

_STRONG_RATE_LIMIT_TEXT = re.compile(
    r"(?:"
    r"you(?:'|’)?ve\s+hit|"
    r"limit\s+(?:reached|exceeded)|"
    r"quota\s+(?:reached|exhausted|exceeded)|"
    r"credits?\s+(?:depleted|exhausted)|"
    r"out\s+of\s+credits|"
    r"usage_limit_reached|"
    r"rate_limit_reached|"
    r"too\s+many\s+requests|"
    r"http\s*429|"
    r"\b429\b"
    r")",
    re.IGNORECASE,
)

_DURATION_PART = re.compile(
    r"(?:(?P<hours>\d+(?:\.\d+)?)\s*h(?:ours?)?)?\s*"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)\s*m(?:in(?:utes?)?)?)?\s*"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)\s*s(?:ec(?:onds?)?)?)?",
    re.IGNORECASE,
)

_DURATION_CONTEXT = re.compile(
    r"(?:reset|retry|try\s+again)[^\n]{0,80}?"
    r"(?:in|after)\s+(?P<duration>"
    r"(?:(?:\d+(?:\.\d+)?)\s*h(?:ours?)?\s*)?"
    r"(?:(?:\d+(?:\.\d+)?)\s*m(?:in(?:utes?)?)?\s*)?"
    r"(?:(?:\d+(?:\.\d+)?)\s*s(?:ec(?:onds?)?)?)?"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RateLimitWindow:
    """一次事件中观察到的额度窗口。"""

    name: str
    used_percent: float | None
    window_minutes: float | None
    reset_at: float | None


@dataclass(frozen=True)
class EventObservation:
    """对一行 Codex 输出的标准化观察结果。"""

    raw_line: str
    event_type: str | None
    session_id: str | None
    quota_exhausted: bool
    reset_at: float | None
    reason: str | None
    is_json: bool
    rate_limits: tuple[RateLimitWindow, ...] = ()


def _normalize_key(key: str) -> str:
    """将 camelCase、短横线和空格字段统一为 snake_case。"""

    key_with_boundaries = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return re.sub(r"[^a-zA-Z0-9]+", "_", key_with_boundaries).strip("_").lower()


def _walk_mappings(
    value: Any,
    path: tuple[str, ...] = (),
) -> Iterator[tuple[tuple[str, ...], Any]]:
    """深度遍历 JSON 对象，返回规范化字段路径和值。"""

    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = _normalize_key(str(raw_key))
            child_path = path + (key,)
            yield child_path, child
            yield from _walk_mappings(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = path + (str(index),)
            yield from _walk_mappings(child, child_path)


def _flatten_strings(value: Any, maximum_length: int = 12000) -> str:
    """提取有限长度的文本，避免把大段输入内容用于正则扫描。"""

    pieces: list[str] = []
    remaining = maximum_length

    def visit(item: Any) -> None:
        nonlocal remaining
        if remaining <= 0:
            return
        if isinstance(item, str):
            piece = item[:remaining]
            pieces.append(piece)
            remaining -= len(piece)
        elif isinstance(item, Mapping):
            for child in item.values():
                visit(child)
                if remaining <= 0:
                    return
        elif isinstance(item, list):
            for child in item:
                visit(child)
                if remaining <= 0:
                    return

    visit(value)
    return " ".join(pieces)


def _parse_timestamp(value: Any) -> float | None:
    """解析 Unix 秒、Unix 毫秒或 ISO 8601 时间。"""

    if isinstance(value, bool) or value is None:
        return None

    if isinstance(value, (int, float)):
        timestamp = float(value)
        return timestamp / 1000 if timestamp > 10_000_000_000 else timestamp

    if not isinstance(value, str):
        return None

    stripped = value.strip()
    if not stripped:
        return None

    try:
        numeric = float(stripped)
    except ValueError:
        numeric = None
    if numeric is not None:
        return numeric / 1000 if numeric > 10_000_000_000 else numeric

    try:
        parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _extract_values(payload: Any, names: set[str]) -> list[tuple[tuple[str, ...], Any]]:
    """提取指定字段名对应的所有路径和值。"""

    return [
        (path, value) for path, value in _walk_mappings(payload) if path[-1] in names
    ]


def _extract_event_type(payload: Mapping[str, Any]) -> str | None:
    """提取事件类型字段。"""

    for key in ("type", "event", "kind"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            normalized = value.strip().lower()
            # Codex rollout JSONL 会把语义事件包在 event_msg 或
            # response_item.payload 中，优先使用内层类型参与失败判定。
            if normalized in {"event_msg", "response_item", "event"}:
                nested = payload.get("payload")
                if isinstance(nested, Mapping):
                    for nested_key in ("type", "event", "kind"):
                        nested_value = nested.get(nested_key)
                        if isinstance(nested_value, str) and nested_value.strip():
                            return nested_value.strip().lower()
            return normalized
    return None


def _extract_session_id(
    payload: Mapping[str, Any],
    event_type: str | None,
) -> str | None:
    """从 thread.started 或兼容字段中提取 session ID。"""

    session_names = {
        "session_id",
        "sessionid",
        "thread_id",
        "threadid",
        "conversation_id",
        "conversationid",
    }
    candidates = _extract_values(payload, session_names)
    for _, value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()

    if event_type == "thread.started":
        thread = payload.get("thread")
        if isinstance(thread, Mapping):
            value = thread.get("id")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _extract_reached_type(payload: Mapping[str, Any]) -> str | None:
    """提取命中的额度窗口名称，例如 primary 或 secondary。"""

    names = {
        "rate_limit_reached_type",
        "ratelimitreachedtype",
        "limit_reached_type",
        "limitreachedtype",
    }
    for _, value in _extract_values(payload, names):
        if isinstance(value, str) and value.strip():
            lowered = value.strip().lower()
            if lowered not in {"none", "null", "false", "unknown"}:
                return lowered
    return None


def _extract_reset_at(
    payload: Mapping[str, Any],
    now: float,
) -> float | None:
    """提取命中窗口对应的 reset 时间。"""

    reset_names = {
        "resets_at",
        "reset_at",
        "resetsat",
        "resetat",
        "reset_timestamp",
        "resettimestamp",
    }
    candidates: list[tuple[tuple[str, ...], float]] = []
    for path, value in _extract_values(payload, reset_names):
        timestamp = _parse_timestamp(value)
        if timestamp is not None:
            candidates.append((path, timestamp))

    if not candidates:
        relative_names = {
            "reset_after_seconds",
            "resetafterseconds",
            "retry_after_seconds",
            "retryafterseconds",
        }
        for _, value in _extract_values(payload, relative_names):
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                continue
            if seconds > 0:
                return now + seconds
        return None

    reached_type = _extract_reached_type(payload)
    if reached_type is not None:
        targeted = [
            timestamp
            for path, timestamp in candidates
            if reached_type in path
            or any(token in path for token in reached_type.split("_"))
        ]
        if targeted:
            return min(targeted)

    # 没有窗口标识时选择最早时间，避免无谓地等待更长的 secondary 窗口。
    return min(timestamp for _, timestamp in candidates)


def _numeric_field(payload: Mapping[str, Any], names: set[str]) -> float | None:
    """从对象中提取一个数字字段。"""

    for key in names:
        value = payload.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _extract_rate_limit_windows(
    payload: Mapping[str, Any],
    now: float,
) -> tuple[RateLimitWindow, ...]:
    """提取 rate_limits 下的窗口，不假设窗口名称对应具体周期。"""

    windows: list[RateLimitWindow] = []
    for _, value in _extract_values(payload, {"rate_limits", "ratelimits"}):
        if not isinstance(value, Mapping):
            continue
        for raw_name, raw_window in value.items():
            if not isinstance(raw_window, Mapping):
                continue
            name = _normalize_key(str(raw_name))
            used_percent = _numeric_field(
                raw_window,
                {"used_percent", "usedpercent"},
            )
            window_minutes = _numeric_field(
                raw_window,
                {"window_minutes", "windowminutes"},
            )
            reset_at = _extract_reset_at(raw_window, now)
            if used_percent is None and window_minutes is None and reset_at is None:
                continue
            windows.append(
                RateLimitWindow(
                    name=name,
                    used_percent=used_percent,
                    window_minutes=window_minutes,
                    reset_at=reset_at,
                )
            )
    return tuple(windows)


def _payload_timestamp(payload: Mapping[str, Any]) -> float | None:
    """提取事件自身的时间，供相对和钟点 reset 文案使用。"""

    for key in ("timestamp", "time", "created_at", "createdat"):
        timestamp = _parse_timestamp(payload.get(key))
        if timestamp is not None:
            return timestamp
    nested = payload.get("payload")
    if isinstance(nested, Mapping):
        for key in ("timestamp", "completed_at", "completedat"):
            timestamp = _parse_timestamp(nested.get(key))
            if timestamp is not None:
                return timestamp
    return None


def _extract_clock_reset(text: str, now: float) -> float | None:
    """从 “try again at 7:03 PM” 这类本地钟点推算 reset 时间。"""

    match = _CLOCK_RESET.search(text)
    if match is None:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if hour > 23 or minute > 59:
        return None
    ampm = (match.group("ampm") or "").lower().replace(".", "")
    if ampm.startswith("p") and hour != 12:
        hour += 12
    elif ampm.startswith("a") and hour == 12:
        hour = 0
    local = datetime.fromtimestamp(now).astimezone()
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now:
        candidate = candidate + timedelta(days=1)
    return candidate.timestamp()


def _extract_duration_reset(text: str, now: float) -> float | None:
    """从“reset in 2h 10m”之类文本中推算 reset 时间。"""

    match = _DURATION_CONTEXT.search(text)
    if match is None:
        return None

    duration_text = match.group("duration")
    duration_match = _DURATION_PART.fullmatch(duration_text.strip())
    if duration_match is None:
        return None

    hours = float(duration_match.group("hours") or 0)
    minutes = float(duration_match.group("minutes") or 0)
    seconds = float(duration_match.group("seconds") or 0)
    duration = hours * 3600 + minutes * 60 + seconds
    return now + duration if duration > 0 else None


def _explicit_quota_flag(payload: Mapping[str, Any]) -> bool:
    """检查结构化额度耗尽标志，避免依赖英文错误文案。"""

    names = {
        "quota_exhausted",
        "quotaexhausted",
        "usage_limit_reached",
        "usagelimitreached",
        "rate_limit_reached",
        "ratelimitreached",
        "credits_depleted",
        "creditsdepleted",
        "credits_exhausted",
        "creditsexhausted",
        "limit_reached",
        "limitreached",
    }
    for path, value in _extract_values(payload, names):
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in {
            "true",
            "yes",
            "1",
        }:
            return True
    for path, value in _walk_mappings(payload):
        if not isinstance(value, str):
            continue
        normalized = value.strip().lower().replace("-", "_")
        if normalized not in _QUOTA_ERROR_CODES:
            continue
        if path[-1] in {
            "codex_error_info",
            "error_info",
            "errorinfo",
            "code",
            "error_code",
            "errorcode",
        } or "error" in path:
            return True
    return False


def parse_event_line(line: str, now: float | None = None) -> EventObservation:
    """解析单行 Codex 输出并判断是否为额度耗尽失败。"""

    current_time = now if now is not None else datetime.now(timezone.utc).timestamp()
    raw_line = line.rstrip("\n")
    stripped = raw_line.strip()
    if not stripped:
        return EventObservation(
            raw_line=raw_line,
            event_type=None,
            session_id=None,
            quota_exhausted=False,
            reset_at=None,
            reason=None,
            is_json=False,
        )

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        quota_exhausted = bool(_STRONG_RATE_LIMIT_TEXT.search(stripped))
        return EventObservation(
            raw_line=raw_line,
            event_type=None,
            session_id=None,
            quota_exhausted=quota_exhausted,
            reset_at=_extract_duration_reset(stripped, current_time),
            reason=stripped[:1000] if quota_exhausted else None,
            is_json=False,
        )

    if not isinstance(payload, Mapping):
        text = _flatten_strings(payload)
        quota_exhausted = bool(_STRONG_RATE_LIMIT_TEXT.search(text))
        return EventObservation(
            raw_line=raw_line,
            event_type=None,
            session_id=None,
            quota_exhausted=quota_exhausted,
            reset_at=_extract_duration_reset(text, current_time),
            reason=text[:1000] if quota_exhausted else None,
            is_json=True,
        )

    event_type = _extract_event_type(payload)
    text = _flatten_strings(payload)
    reached_type = _extract_reached_type(payload)
    has_explicit_flag = _explicit_quota_flag(payload)
    failure_event = event_type in _FAILURE_EVENT_TYPES if event_type else False
    complete_event = event_type in _QUOTA_COMPLETE_EVENT_TYPES if event_type else False
    strong_limit_text = bool(_STRONG_RATE_LIMIT_TEXT.search(text))
    text_indicates_limit = bool(_RATE_LIMIT_TEXT.search(text) or strong_limit_text)
    quota_exhausted = has_explicit_flag or (
        text_indicates_limit and (failure_event or complete_event)
    ) or (complete_event and strong_limit_text)

    event_time = _payload_timestamp(payload) or current_time
    reset_at = _extract_reset_at(payload, event_time)
    if reset_at is None:
        reset_at = _extract_duration_reset(text, event_time)
    if reset_at is None:
        reset_at = _extract_clock_reset(text, event_time)

    if reached_type is not None and failure_event:
        quota_exhausted = True

    reason: str | None = None
    if quota_exhausted:
        reason = text[:1000] if text else (event_type or "额度限制失败")

    rate_limits = _extract_rate_limit_windows(payload, current_time)

    return EventObservation(
        raw_line=raw_line,
        event_type=event_type,
        session_id=_extract_session_id(payload, event_type),
        quota_exhausted=quota_exhausted,
        reset_at=reset_at,
        reason=reason,
        is_json=True,
        rate_limits=rate_limits,
    )
