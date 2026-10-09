"""告警上下文的共享部分：窗口常量、候选与提取结果类型、记录扫描和文本摘要工具。"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..alert_activity import describe_tool_activity, describe_user_text


# 中文注释：内容事件（用户消息、工具输出）往往发生在字节告警触发前一两分钟
# （上传的是累积上下文），窗口向前多留 2 分钟；向后只留 30 秒落盘余量。
_WINDOW_LEAD_SECONDS = 120.0


_WINDOW_TRAIL_SECONDS = 30.0


# 中文注释：候选会话只需在窗口开始前 5 分钟内有过写入（mtime 预筛）。
_MTIME_SLACK_SECONDS = 300.0


_MAX_EVENTS = 60


# 中文注释：窗口内没有事件时（如 MCP 子进程直接外传），退而展示告警前最近的活动。
_FALLBACK_TAIL = 8


_EXCERPT_CHARS = 160


# 中文注释：关联工具输出时限制保留的调用数，避免长会话无限占用内存。
_MAX_TOOL_LABELS = 1024


# 中文注释：会话只追加。先从尾部退到时间窗之前再正向扫，避免整文件读进请求线程。
_TAIL_CHUNK = 256 * 1024


_LOOKBACK_RECORDS = 256


# 中文注释：单块 JSON 会话过大时不整份载入内存。
_BLOB_READ_LIMIT = 32 * 1024 * 1024


@dataclass(frozen=True)
class AlertContextRoots:
    """各产品的会话文件根目录。"""

    codex_sessions: tuple[Path, ...] = ()
    claude_projects: tuple[Path, ...] = ()
    kimi_sessions: tuple[Path, ...] = ()
    commandcode_projects: tuple[Path, ...] = ()
    grok_sessions: tuple[Path, ...] = ()
    dsh_sessions: tuple[Path, ...] = ()
    opencode_dbs: tuple[Path, ...] = ()
    cursor_projects: tuple[Path, ...] = ()
    gemini_homes: tuple[Path, ...] = ()
    qwen_homes: tuple[Path, ...] = ()
    aider_homes: tuple[Path, ...] = ()


@dataclass(frozen=True)
class _Candidate:
    """一个可能对上告警的会话文件；session_id 用于一个库里有多段会话的产品。"""

    path: Path
    mtime: float
    size: int
    session_id: str | None = None


@dataclass(frozen=True)
class _Extraction:
    """一次会话明细提取的结果；events 为 None 表示文件不可读。"""

    events: tuple[dict[str, Any], ...] | None
    truncated: bool
    input_bytes: int
    output_bytes: int
    fallback: bool = False
    event_count: int | None = None


@dataclass(frozen=True)
class _ToolCall:
    """保留调用的行为描述，使输出能通过 ID 关联用途而非猜测相邻事件。"""

    label: str
    activities: tuple[dict[str, str], ...]


def _copy_event(item: Mapping[str, Any]) -> dict[str, Any]:
    """复制一条事件，避免内容开关改写缓存里的摘要。"""

    copied = dict(item)
    activities = item.get("activities")
    if isinstance(activities, list):
        copied["activities"] = [
            dict(activity) if isinstance(activity, Mapping) else activity
            for activity in activities
        ]
    return copied


def _cwd_matches(alert_cwd: str, session_cwd: str) -> bool:
    """进程 cwd 等于会话目录，或落在会话目录里面，才算同一现场。

    会话目录是告警目录的上级时算匹配（进程在项目子目录里）。反过来，
    一个很短的告警目录不能把下面每个项目都算进来。空的会话目录也不匹配。
    """

    if not session_cwd or not alert_cwd:
        return False
    if alert_cwd == session_cwd:
        return True
    prefix = session_cwd.rstrip("/")
    if not prefix:
        return False
    return alert_cwd.startswith(prefix + "/")


def _claude_slug(cwd: str) -> str:
    """Claude Code 把 cwd 里的非字母数字字符替换成 '-' 作为目录名。"""

    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _session_id_from_name(path: Path) -> str | None:
    match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        path.name,
    )
    return match.group(1) if match else None


def _parse_ts(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _is_readable(path: Path) -> bool:
    try:
        with path.open("rb"):
            return True
    except OSError:
        return False


def _iter_records(
    path: Path,
    dates: tuple[bytes, ...] | None,
    offset: int = 0,
) -> Iterable[Mapping[str, Any]]:
    """从 ``offset`` 起逐行产出 JSON 记录（调用方先查可读性）。"""

    try:
        with path.open("rb") as handle:
            if offset > 0:
                handle.seek(offset)
            for raw in handle:
                if dates and not any(day in raw for day in dates):
                    continue
                try:
                    record = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(record, Mapping):
                    yield record
    except OSError:
        return


def _line_timestamp(raw: bytes, parse_ts: _TsFn) -> float | None:
    """解析一行的时间戳；坏行和没有时间的行返回 None。"""

    try:
        record = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    if not isinstance(record, Mapping):
        return None
    try:
        return parse_ts(record)
    except (TypeError, ValueError):
        return None


def _suffix_offset(path: Path, start: float | None, parse_ts: _TsFn) -> int:
    """返回正向扫描的起始字节。

    从文件尾往前找，直到越过时间窗并再留出一段记录，供工具调用和兜底使用。
    文件不超过一块、或整份都落在窗口里时返回 0，等价于整文件扫描。
    """

    if start is None:
        return 0
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size <= _TAIL_CHUNK:
        return 0
    try:
        handle = path.open("rb")
    except OSError:
        return 0
    with handle:
        position = size
        pending = b""
        old_count = 0
        crossed = False
        while position > 0:
            take = min(_TAIL_CHUNK, position)
            position -= take
            handle.seek(position)
            block = handle.read(take) + pending
            parts = block.split(b"\n")
            if position > 0:
                pending = parts[0]
                complete = parts[1:]
                base = position + len(parts[0]) + 1
            else:
                pending = b""
                complete = parts
                base = 0
            offsets: list[int] = []
            cursor = base
            for part in complete:
                offsets.append(cursor)
                cursor += len(part) + 1
            for offset, raw in zip(reversed(offsets), reversed(complete), strict=True):
                if not raw.strip():
                    continue
                timestamp = _line_timestamp(raw, parse_ts)
                if timestamp is not None and timestamp >= start:
                    # 中文注释：时钟回拨时重新计数，避免停在窗口中间。
                    crossed = False
                    old_count = 0
                    continue
                if timestamp is not None and timestamp < start:
                    crossed = True
                if not crossed:
                    continue
                old_count += 1
                if old_count >= _LOOKBACK_RECORDS:
                    return offset
    return 0


def _excerpt(value: object, limit: int = _EXCERPT_CHARS) -> str:
    """脱敏常见凭据后生成摘要；先脱敏再截断，避免暴露凭据前缀。"""

    text = str(value or "")
    text = re.sub(r"(?i)\bBearer\s+[\w.~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\bsk-[\w-]{8,}", "[REDACTED]", text)
    secret = r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|token|authorization)"
    text = re.sub(
        rf"(?i)(\b{secret}\b[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}}&]+)",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(rf"(?i)(--{secret}\s+)(\S+)", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _tool_detail(arguments: str) -> str:
    """从工具参数里挑出最能说明行为的字段（命令、路径、查询词）。"""

    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return _excerpt(arguments)
    if isinstance(parsed, Mapping):
        for key in (
            "cmd",
            "command",
            "file_path",
            "path",
            "filename",
            "target_file",
            "filePath",
            "query",
            "pattern",
        ):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return _excerpt(value)
        return _excerpt(json.dumps(parsed, ensure_ascii=False))
    return _excerpt(arguments)


def _remember_tool_call(
    calls: dict[str, _ToolCall] | None, call_id: object, call: _ToolCall
) -> None:
    """按调用 ID 记录用途和名称，用于关联后续输出。"""

    if calls is None or not isinstance(call_id, str) or not call_id:
        return
    calls[call_id] = call
    if len(calls) > _MAX_TOOL_LABELS:
        # 中文注释：只淘汰最旧的关联，不按相邻事件猜测输出属于哪个工具。
        calls.pop(next(iter(calls)))


def _tool_output_info(
    calls: dict[str, _ToolCall] | None, call_id: object
) -> dict[str, Any]:
    """输出只关联已记录的用途，不把执行结果当成上传成功的证明。"""

    call = calls.get(call_id) if calls and isinstance(call_id, str) else None
    return {
        "label": call.label if call else "",
        "activities": [dict(activity, phase="result") for activity in call.activities]
        if call
        else [
            {
                "summary": "返回工具结果",
                "basis": "record",
                "target": "",
                "phase": "result",
            }
        ],
    }


def _image_event(part: object, timestamp: float) -> tuple[dict[str, Any], int, int]:
    """提取图片输入线索；不把图片 URL、base64 或实际图像返回给页面。"""

    size = None
    if isinstance(part, Mapping):
        source = part.get("source")
        if isinstance(source, Mapping) and isinstance(source.get("data"), str):
            size = len(source["data"].encode("utf-8", errors="replace"))
        url = part.get("image_url")
        if isinstance(url, str) and url.startswith("data:"):
            size = len(url.encode("utf-8", errors="replace"))
    return (
        {
            "t": timestamp,
            "kind": "image",
            "label": "",
            "detail": "",
            "size": size,
            "activities": [
                {"summary": "向模型提供图片", "basis": "record", "target": ""}
            ],
        },
        size or 0,
        0,
    )


# 中文注释：map_record 把一条原始记录映射成 0..n 个
# （事件, 上行字节, 输出字节）；parse_ts 给出该格式的记录时间戳。
_MapFn = Callable[[Mapping[str, Any], float], list[tuple[dict[str, Any], int, int]]]


_TsFn = Callable[[Mapping[str, Any]], float | None]


def _scan_parsed(
    records: Iterable[Mapping[str, Any]],
    *,
    parse_ts: _TsFn,
    map_record: _MapFn,
    start: float | None,
    end: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, int, int, int]:
    """单遍扫描已解析记录：窗口内事件（带截断）+ 窗口前最近的若干条。"""

    in_window: list[dict[str, Any]] = []
    before: deque[dict[str, Any]] = deque(maxlen=_FALLBACK_TAIL)
    truncated = False
    input_bytes = 0
    output_bytes = 0
    event_count = 0
    for record in records:
        timestamp = parse_ts(record)
        if timestamp is None or timestamp > end:
            continue
        for item, delta_in, delta_out in map_record(record, timestamp):
            if start is None or timestamp < start:
                before.append(item)
                continue
            event_count += 1
            input_bytes += delta_in
            output_bytes += delta_out
            if len(in_window) >= _MAX_EVENTS:
                truncated = True
                continue
            in_window.append(item)
    in_window.sort(key=lambda item: item["t"])
    return in_window, list(before), truncated, input_bytes, output_bytes, event_count


def _scan_session(
    path: Path,
    *,
    dates: tuple[bytes, ...] | None,
    parse_ts: _TsFn,
    map_record: _MapFn,
    start: float | None,
    end: float,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, int, int, int]:
    """从尾部定位后的偏移正向扫描会话文件。"""

    return _scan_parsed(
        _iter_records(path, dates, offset),
        parse_ts=parse_ts,
        map_record=map_record,
        start=start,
        end=end,
    )


def _extraction_from_scan(
    scanned: tuple[list[dict[str, Any]], list[dict[str, Any]], bool, int, int, int],
) -> _Extraction:
    """窗口里有事件就用窗口；否则用扫描时已经留下的窗口前活动。"""

    in_window, before, truncated, input_bytes, output_bytes, event_count = scanned
    if in_window:
        return _Extraction(
            tuple(in_window),
            truncated,
            input_bytes,
            output_bytes,
            event_count=event_count,
        )
    if not before:
        return _Extraction((), False, 0, 0)
    fallback_in = sum(
        item["size"] or 0
        for item in before
        if item["kind"] in ("user", "image", "tool")
    )
    fallback_out = sum(
        item["size"] or 0 for item in before if item["kind"] == "tool_output"
    )
    return _Extraction(
        tuple(before), False, fallback_in, fallback_out, fallback=True
    )


def _finish(
    path: Path,
    *,
    parse_ts: _TsFn,
    map_record: _MapFn,
    start: float,
    end: float,
) -> _Extraction:
    """从覆盖时间窗的文件尾部做一次正向扫描。"""

    if not _is_readable(path):
        return _Extraction(None, False, 0, 0)
    return _extraction_from_scan(
        _scan_session(
            path,
            dates=None,
            parse_ts=parse_ts,
            map_record=map_record,
            start=start,
            end=end,
            offset=_suffix_offset(path, start, parse_ts),
        )
    )


def _iso_record_ts(record: Mapping[str, Any]) -> float | None:
    return _parse_ts(record.get("timestamp"))


# 中文注释：真实会话是 unix 秒（约 1e9）。超过该阈值才当成毫秒，避免把秒误除。
_GROK_MS_THRESHOLD = 10_000_000_000


def _epoch_seconds(value: object) -> float | None:
    """秒或毫秒的数字时间戳；布尔值不是时间。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number > _GROK_MS_THRESHOLD:
        return number / 1000.0
    return number


def _as_text(value: object) -> str:
    """把字符串或 JSON 值收成一段文本；空对象不当成输出。"""

    if isinstance(value, str):
        return value
    if value is None:
        return ""
    text = json.dumps(value, ensure_ascii=False, default=str)
    if text in {"{}", "[]", "null", '""'}:
        return ""
    return text


def _content_text(content: object) -> str:
    """取出用户或工具正文里的文字，跳过思考和图片块。"""

    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, Mapping):
            continue
        if part.get("type") in {"image", "image_url", "input_image"}:
            continue
        if part.get("thought") is True or part.get("type") == "thought":
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(part for part in parts if part)


def _user_item(text: str, timestamp: float) -> tuple[dict[str, Any], int, int] | None:
    if not text.strip():
        return None
    size = len(text.encode("utf-8", errors="replace"))
    return (
        {
            "t": timestamp,
            "kind": "user",
            "label": "",
            "detail": _excerpt(text),
            "size": size,
            "activities": [describe_user_text(text)],
        },
        size,
        0,
    )


def _tool_item(
    name: str,
    arguments: object,
    timestamp: float,
    call_id: object,
    tool_calls: dict[str, _ToolCall] | None,
) -> tuple[dict[str, Any], int, int]:
    activities = describe_tool_activity(name, arguments if arguments is not None else {})
    label = name or ""
    _remember_tool_call(tool_calls, call_id, _ToolCall(label, tuple(activities)))
    raw = arguments if isinstance(arguments, str) else _as_text(arguments)
    size = len(raw.encode("utf-8", errors="replace"))
    return (
        {
            "t": timestamp,
            "kind": "tool",
            "label": label,
            "detail": _tool_detail(raw),
            "size": size,
            "activities": activities,
        },
        size,
        0,
    )


def _output_item(
    text: str,
    timestamp: float,
    call_id: object,
    tool_calls: dict[str, _ToolCall] | None,
) -> tuple[dict[str, Any], int, int] | None:
    if not text:
        return None
    size = len(text.encode("utf-8", errors="replace"))
    return (
        {
            "t": timestamp,
            "kind": "tool_output",
            **_tool_output_info(tool_calls, call_id),
            "detail": "",
            "size": size,
        },
        0,
        size,
    )


class _DecodeError(OSError):
    """会话压缩帧无法解开。"""


_UNREADABLE = object()


def _mapping_lines(lines: Iterable[str]) -> Iterable[Mapping[str, Any]]:
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(record, Mapping):
            yield record


def _bucket_items(
    items: list[tuple[dict[str, Any], int, int]],
    start: float,
    end: float,
) -> _Extraction:
    """按事件时间分成窗口内和窗口前，规则与文件扫描一致。"""

    in_window: list[dict[str, Any]] = []
    before: deque[dict[str, Any]] = deque(maxlen=_FALLBACK_TAIL)
    truncated = False
    input_bytes = 0
    output_bytes = 0
    event_count = 0
    for item, delta_in, delta_out in items:
        timestamp = float(item["t"])
        if timestamp > end:
            continue
        if timestamp < start:
            before.append(item)
            continue
        event_count += 1
        input_bytes += delta_in
        output_bytes += delta_out
        if len(in_window) >= _MAX_EVENTS:
            truncated = True
            continue
        in_window.append(item)
    in_window.sort(key=lambda event: event["t"])
    return _extraction_from_scan(
        (in_window, list(before), truncated, input_bytes, output_bytes, event_count)
    )


def _path_slugs(cwd: str) -> set[str]:
    """目录名的几种扁平写法：非字母数字换成 '-'，或只替换斜杠。"""

    dashed = _claude_slug(cwd)
    slash = cwd.replace("/", "-").replace("\\", "-")
    return {
        item
        for item in (dashed, dashed.lstrip("-"), slash, slash.lstrip("-"))
        if item
    }


def _tail_text_lines(path: Path, limit: int) -> tuple[list[str], bool] | None:
    """从文件尾部取出最多 limit 行；多于这个数时标明被截断。"""

    try:
        handle = path.open("rb")
    except OSError:
        return None
    collected: list[str] = []
    with handle:
        try:
            position = handle.seek(0, 2)
        except OSError:
            return None
        pending = b""
        while position > 0 and len(collected) <= limit:
            take = min(_TAIL_CHUNK, position)
            position -= take
            handle.seek(position)
            block = handle.read(take) + pending
            parts = block.split(b"\n")
            if position > 0:
                pending = parts[0]
                complete = parts[1:]
            else:
                pending = b""
                complete = parts
            for raw in reversed(complete):
                if not raw.strip():
                    continue
                collected.append(raw.decode("utf-8", errors="replace"))
                if len(collected) > limit:
                    break
        if pending.strip() and len(collected) <= limit:
            collected.append(pending.decode("utf-8", errors="replace"))
    truncated = len(collected) > limit
    return list(reversed(collected[:limit])), truncated


def _project_paths(home: Path) -> dict[str, str]:
    """projects.json 把项目 id 映到工作目录。几种历史写法都认。"""

    path = home / "projects.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    found: dict[str, str] = {}

    def take(ident: object, value: object) -> None:
        if isinstance(value, str) and isinstance(ident, str) and value:
            found[ident] = value
            return
        if isinstance(value, Mapping) and isinstance(ident, str):
            cwd = value.get("path") or value.get("cwd")
            if isinstance(cwd, str) and cwd:
                found[ident] = cwd

    if isinstance(payload, Mapping):
        nested = payload.get("projects")
        source = nested if isinstance(nested, Mapping) else payload
        for key, value in source.items():
            take(key, value)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, Mapping):
                take(item.get("id") or item.get("hash"), item.get("path") or item.get("cwd"))
    return found


def _ids_for_cwd(home: Path, cwd: str) -> set[str] | None:
    """有项目表时只返回对得上的 id；没有表时返回 None，表示还得看文件头。"""

    mapping = _project_paths(home)
    if not mapping:
        return None
    matched = {
        ident for ident, path in mapping.items() if _cwd_matches(cwd, path)
    }
    # 中文注释：表里没有这个目录时，改看会话头，避免过期的项目表把会话藏掉。
    return matched or None


def _header_cwd(path: Path) -> str | None:
    """会话头里的 cwd。JSONL 只看开头几行，不把整份历史读进来。"""

    try:
        if path.suffix == ".json":
            if path.stat().st_size > _BLOB_READ_LIMIT:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, Mapping):
                for key in ("cwd", "projectPath", "directory"):
                    value = payload.get(key)
                    if isinstance(value, str) and value:
                        return value
            return None
        with path.open("rb") as handle:
            raw = handle.read(65536)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    for line in raw.split(b"\n")[:8]:
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if not isinstance(record, Mapping):
            continue
        for key in ("cwd", "projectPath", "directory"):
            value = record.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _existing_file(path: Path) -> tuple[Path, ...]:
    return (path,) if path.is_file() else ()


def _existing_dir(path: Path) -> tuple[Path, ...]:
    return (path,) if path.is_dir() else ()


def _existing_homes(homes: Sequence[Path]) -> tuple[Path, ...]:
    return tuple(path for path in (Path(home) for home in homes) if path.is_dir())
