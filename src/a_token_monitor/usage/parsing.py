"""从各家 agent 的会话日志中解析 token 增量、模型和项目。"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..local_agents import (
    CountedUsage,
)
from .records import (
    TokenUsage,
    UsageDelta,
    _UsageParseResult,
    _UsageParseState,
    _text_value,
)


_USAGE_LINE_HINTS = (
    b"total_token_usage",
    b"last_token_usage",
    b"totalTokenUsage",
    b"lastTokenUsage",
)


_MODEL_LINE_HINTS = (
    b"turn_context",
    b"world_state",
    b"session_meta",
    b"thread_settings",
)


_MAX_MODEL_LINE_BYTES = 64 * 1024


def _token_usage_delta(
    current: TokenUsage,
    previous: TokenUsage | None,
) -> TokenUsage | None:
    """两个累计快照之间的非负增量；计数回退时重新发出当前合计。"""

    if previous is None:
        if current.total_tokens <= 0 and current.input_tokens <= 0:
            return None
        return current
    if (
        current.input_tokens < previous.input_tokens
        or current.output_tokens < previous.output_tokens
        or current.total_tokens < previous.total_tokens
    ):
        return current
    delta = TokenUsage(
        input_tokens=current.input_tokens - previous.input_tokens,
        cached_input_tokens=max(
            0, current.cached_input_tokens - previous.cached_input_tokens
        ),
        cache_write_input_tokens=max(
            0,
            current.cache_write_input_tokens - previous.cache_write_input_tokens,
        ),
        output_tokens=current.output_tokens - previous.output_tokens,
        reasoning_output_tokens=max(
            0,
            current.reasoning_output_tokens - previous.reasoning_output_tokens,
        ),
        total_tokens=max(0, current.total_tokens - previous.total_tokens),
    )
    if (
        delta.input_tokens
        + delta.cached_input_tokens
        + delta.output_tokens
        + delta.total_tokens
        <= 0
    ):
        return None
    return delta


def _parse_usage_file(
    path: Path,
    fallback_timestamp: float,
) -> tuple[tuple[UsageDelta, ...], str | None]:
    """安全解析一个 JSONL，只提取累计 token 和模型字段。"""

    parsed = _parse_usage_chunk(
        path,
        offset=0,
        state=_UsageParseState(
            previous_timestamp=fallback_timestamp,
        ),
    )
    deltas = (
        parsed.total_deltas if parsed.state.has_total_usage else parsed.fallback_deltas
    )
    return deltas, parsed.state.project


def _parse_usage_chunk(
    path: Path,
    offset: int,
    state: _UsageParseState,
    maximum_bytes: int | None = None,
    *,
    commandcode: bool = False,
) -> _UsageParseResult:
    """从已确认的完整行偏移继续解析 JSONL 追加内容。"""

    if offset < 0:
        raise ValueError("offset 不能小于 0")
    if maximum_bytes is not None and maximum_bytes <= 0:
        raise ValueError("maximum_bytes 必须大于 0")
    total_deltas: list[UsageDelta] = []
    fallback_deltas: list[UsageDelta] = []
    total_baseline = state.total_baseline
    last_baseline = state.last_baseline
    has_total_usage = state.has_total_usage
    current_model = state.current_model
    project = state.project
    previous_timestamp = state.previous_timestamp
    next_offset = offset
    bytes_read = 0
    reached_eof = False
    discarding_oversized_line = state.discarding_oversized_line
    recent_ids = list(state.recent_ids)
    known_ids = set(recent_ids)
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
            if discarding_oversized_line:
                first_newline = content.find(b"\n")
                if first_newline < 0:
                    next_offset = offset + bytes_read
                    reached_eof = reached_physical_eof
                    return _UsageParseResult(
                        next_offset=next_offset,
                        total_deltas=(),
                        fallback_deltas=(),
                        state=replace(
                            state,
                            discarding_oversized_line=True,
                        ),
                        bytes_read=bytes_read,
                        reached_eof=reached_eof,
                    )
                content_offset = first_newline + 1
                discarding_oversized_line = False

            remaining = content[content_offset:]
            last_newline = remaining.rfind(b"\n")
            if last_newline < 0:
                if reached_physical_eof:
                    # 中文注释：EOF 半行不计入偏移，文件追加完成后会重新读取。
                    next_offset = offset + content_offset
                    reached_eof = True
                else:
                    # 中文注释：单行超过预算时分段跳过，防止一次读入超大工具输出。
                    next_offset = offset + bytes_read
                    discarding_oversized_line = True
                complete_content = b""
            else:
                complete_content = remaining[: last_newline + 1]
                next_offset = offset + content_offset + len(complete_content)
                reached_eof = reached_physical_eof

            for raw_line in complete_content.splitlines(keepends=True):
                if not _should_parse_usage_line(raw_line) and not (
                    commandcode and (b'"usage"' in raw_line or (
                        len(raw_line) <= _MAX_MODEL_LINE_BYTES
                        and any(hint in raw_line for hint in (b'"session"', b'"model_change"'))
                    ))
                ):
                    continue
                line = raw_line.decode("utf-8", errors="replace")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, Mapping):
                    continue
                model = _extract_model(event)
                if model is not None:
                    current_model = model
                if project is None:
                    project = _extract_project(event)
                event_timestamp = _event_timestamp(event)
                if event_timestamp is not None:
                    previous_timestamp = event_timestamp
                if commandcode:
                    # 中文注释：官方 sessionStore 将单次请求 usage 写在 assistant 消息外层。
                    message = event.get("message")
                    raw_usage = event.get("usage")
                    if (
                        event.get("type") != "message"
                        or not isinstance(message, Mapping)
                        or message.get("role") != "assistant"
                        or not isinstance(raw_usage, Mapping)
                    ):
                        continue
                    entry_id = _text_value(event.get("id"))
                    if entry_id and entry_id in known_ids:
                        continue
                    request_usage = TokenUsage.from_mapping({
                        "input_tokens": raw_usage.get("inputTokens"),
                        "output_tokens": raw_usage.get("outputTokens"),
                        "cached_input_tokens": raw_usage.get("cacheReadTokens"),
                        "cache_write_input_tokens": raw_usage.get("cacheWriteTokens"),
                    })
                    if request_usage is not None and not request_usage.is_zero():
                        has_total_usage = True
                        total_deltas.append(UsageDelta(
                            previous_timestamp, current_model, request_usage,
                            billing_usage=request_usage, project=project,
                        ))
                        if entry_id:
                            known_ids.add(entry_id)
                            recent_ids.append(entry_id)
                    continue
                total_usage = _extract_usage(event, "total_token_usage")
                last_usage = _extract_usage(event, "last_token_usage")
                if total_usage is not None:
                    has_total_usage = True
                    previous_total = total_baseline
                    decreased = (
                        previous_total is not None
                        and total_usage.decreased_from(previous_total)
                    )
                    total_delta = _next_delta(total_usage, previous_total)
                    unchanged = (
                        previous_total is not None
                        and not decreased
                        and total_delta.is_zero()
                    )
                    total_baseline = total_usage
                    if last_usage is not None:
                        last_baseline = last_usage
                    if unchanged:
                        continue
                    # 中文注释：正常情况下累计差值是完整用量；累计计数回退时，
                    # 当前 total 往往仍携带上下文，只能加入本次 last 用量。
                    emitted = (
                        last_usage
                        if decreased and last_usage is not None
                        else total_delta
                    )
                    if emitted.is_zero():
                        continue
                    total_deltas.append(
                        UsageDelta(
                            timestamp=previous_timestamp,
                            model=current_model,
                            usage=emitted,
                            billing_usage=_billing_usage(emitted, last_usage),
                        )
                    )
                elif last_usage is not None:
                    # 中文注释：last_token_usage 是一次请求而不是累计计数；相邻
                    # 完全相同通常是同一快照重复写入，只做相等去重。
                    repeated = (
                        last_baseline is not None
                        and last_usage.as_values() == last_baseline.as_values()
                    )
                    last_baseline = last_usage
                    if not repeated and not last_usage.is_zero():
                        fallback_deltas.append(
                            UsageDelta(
                                timestamp=previous_timestamp,
                                model=current_model,
                                usage=last_usage,
                                billing_usage=last_usage,
                            )
                        )
    except (OSError, UnicodeError):
        return _UsageParseResult(
            next_offset=offset,
            total_deltas=(),
            fallback_deltas=(),
            state=state,
            bytes_read=0,
            reached_eof=False,
        )
    return _UsageParseResult(
        next_offset=next_offset,
        total_deltas=tuple(total_deltas),
        fallback_deltas=tuple(fallback_deltas),
        state=_UsageParseState(
            total_baseline=total_baseline,
            last_baseline=last_baseline,
            has_total_usage=has_total_usage,
            current_model=current_model,
            project=project,
            previous_timestamp=previous_timestamp,
            discarding_oversized_line=discarding_oversized_line,
            recent_ids=tuple(recent_ids[-512:]),
        ),
        bytes_read=bytes_read,
        reached_eof=reached_eof,
    )


def _next_delta(
    current: TokenUsage,
    previous: TokenUsage | None,
) -> TokenUsage:
    """把累计快照转换为增量；检测到计数回退时视为新一段累计。"""

    if previous is None or current.decreased_from(previous):
        return current
    return current.subtract(previous)


def _should_parse_usage_line(raw_line: bytes) -> bool:
    """只解析用量、模型和会话元数据行，跳过巨型工具输出。"""

    if any(hint in raw_line for hint in _USAGE_LINE_HINTS):
        return True
    if len(raw_line) > _MAX_MODEL_LINE_BYTES:
        return False
    return any(hint in raw_line for hint in _MODEL_LINE_HINTS)


def _billing_usage(
    total_delta: TokenUsage,
    last_usage: TokenUsage | None,
) -> TokenUsage:
    """选择用于 API 等价计价的单次请求用量。"""

    # 中文注释：只有 last 与累计增量一致时，才能确认它完整覆盖该增量。
    # 若中间快照缺失，直接使用较小的 last 会系统性漏算 token 和成本。
    if (
        last_usage is not None
        and last_usage.as_values() == total_delta.as_values()
    ):
        return last_usage
    return total_delta


def _canonical_source_rank(path: Path) -> tuple[int, int, str]:
    """按文件长度、修改时间和路径稳定选择最完整的 session 副本。"""

    try:
        stat_result = path.stat()
    except OSError:
        return (-1, -1, str(path))
    return (stat_result.st_size, stat_result.st_mtime_ns, str(path))


def _safe_file_size(path: Path) -> int:
    """安全读取文件长度，文件消失时按零处理。"""

    try:
        return path.stat().st_size
    except OSError:
        return 0


def _extract_usage(
    event: Mapping[str, Any],
    field_name: str,
) -> TokenUsage | None:
    """只在已知 Codex info 路径中读取 token 对象。"""

    for container in _event_containers(event):
        info = container.get("info")
        if isinstance(info, Mapping):
            value = info.get(field_name)
            if isinstance(value, Mapping):
                usage = TokenUsage.from_mapping(value)
                if usage is not None:
                    return usage
        value = container.get(field_name)
        if isinstance(value, Mapping):
            usage = TokenUsage.from_mapping(value)
            if usage is not None:
                return usage
    return None


def _extract_model(event: Mapping[str, Any]) -> str | None:
    """在 Codex 已知结构中读取当前模型，不遍历用户输入内容。"""

    paths = (
        ("model",),
        ("model_id",),
        ("model_name",),
        ("model_slug",),
        ("info", "model"),
        ("thread_settings", "model"),
        ("thread_settings", "collaboration_mode", "settings", "model"),
        ("collaboration_mode", "settings", "model"),
        ("turn_context", "model"),
        ("turn_context", "payload", "model"),
        ("world_state", "model"),
        ("world_state", "payload", "state", "model"),
        ("state", "model"),
    )
    for container in _event_containers(event):
        for path in paths:
            value: Any = container
            for key in path:
                if not isinstance(value, Mapping):
                    value = None
                    break
                value = value.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:160]
    return None


def _extract_project(event: Mapping[str, Any]) -> str | None:
    """从 session_meta 等已知结构读取工作目录，不遍历用户内容。"""

    paths = (
        ("cwd",),
        ("working_directory",),
        ("workingDirectory",),
        ("session_meta", "cwd"),
        ("session_meta", "working_directory"),
        ("session_meta", "workingDirectory"),
    )
    for container in _event_containers(event):
        for path in paths:
            value: Any = container
            for key in path:
                if not isinstance(value, Mapping):
                    value = None
                    break
                value = value.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:1_024]
    return None


def _event_containers(event: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """返回外层事件和有限深度的已知 payload 容器。"""

    containers: list[Mapping[str, Any]] = [event]
    first_payload = event.get("payload")
    if isinstance(first_payload, Mapping):
        containers.append(first_payload)
        second_payload = first_payload.get("payload")
        if isinstance(second_payload, Mapping):
            containers.append(second_payload)
    return tuple(containers)


def _event_timestamp(event: Mapping[str, Any]) -> float | None:
    """解析 JSONL 顶层或已知 payload 中的事件时间。"""

    values: list[Any] = [event.get("timestamp")]
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        values.append(payload.get("timestamp"))
    for value in values:
        timestamp = _timestamp(value)
        if timestamp is not None:
            return timestamp
    return None


def _timestamp(value: Any) -> float | None:
    """解析 Unix 秒、毫秒或 ISO 8601 时间。"""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return number / 1000 if number > 10_000_000_000 else number
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


def _normalized_path(value: str | None) -> Path | None:
    """把 JSONL 路径规范化为稳定的绝对 Path。"""

    if value is None or not value.strip():
        return None
    path = Path(value).expanduser()
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()


def _file_signature(path: Path) -> tuple[int, int, int] | None:
    """读取文件 inode、修改时间和大小，不读取文件内容。"""

    try:
        stat_result = path.stat()
    except OSError:
        return None
    return stat_result.st_ino, stat_result.st_mtime_ns, stat_result.st_size


def _delta_from_counted(event: CountedUsage) -> UsageDelta:
    """把一次真实记录的用量变成索引增量。"""

    usage = TokenUsage(
        input_tokens=event.input_tokens,
        cached_input_tokens=event.cached_input_tokens,
        cache_write_input_tokens=event.cache_write_input_tokens,
        output_tokens=event.output_tokens,
        reasoning_output_tokens=event.reasoning_output_tokens,
        total_tokens=event.total_tokens,
    )
    return UsageDelta(
        timestamp=event.timestamp,
        model=event.model,
        usage=usage,
        billing_usage=usage,
        project=event.project,
    )


def _merge_counted(
    order: tuple[str, ...],
    current: dict[str, UsageDelta],
    events: tuple[CountedUsage, ...],
    *,
    replace: bool,
) -> tuple[tuple[str, ...], dict[str, UsageDelta]]:
    """Gemini / Qwen 同 ID 以后写为准；Cursor 同 ID 只保留第一次。"""

    keys = list(order)
    merged = dict(current)
    for event in events:
        key = event.dedupe_key
        if key in merged:
            if replace:
                merged[key] = _delta_from_counted(event)
            continue
        merged[key] = _delta_from_counted(event)
        keys.append(key)
    return tuple(keys), merged


def _single_project(deltas: tuple[UsageDelta, ...]) -> str | None:
    projects = {delta.project for delta in deltas if delta.project}
    if len(projects) == 1:
        return next(iter(projects))
    return None


def _path_under(path: Path, homes: Sequence[Path]) -> bool:
    return _owning_home(path, homes) is not None


def _owning_home(path: Path, homes: Sequence[Path]) -> Path | None:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    matches: list[Path] = []
    for home in homes:
        try:
            home_resolved = home.resolve()
        except OSError:
            home_resolved = home
        try:
            resolved.relative_to(home_resolved)
        except ValueError:
            continue
        matches.append(home)
    if not matches:
        return None
    return max(matches, key=lambda item: len(item.parts))
