"""告警上下文：把流量告警映射回本地会话文件，回答「当时在上传什么」。

流量监控只按内核 TCP 计数器统计字节数，看不到也存不下具体内容；但 agent
发给 API 的内容（用户消息、工具调用、工具输出）完整落在本地会话 JSONL 里。
本模块按告警的 ``cwd`` 和时间窗定位会话文件，提取窗口内的事件明细。
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .alerts import StoredAlert
from .discovery import JsonlSessionReader

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

SUPPORTED_PRODUCTS = ("codex", "claude", "kimi")


@dataclass(frozen=True)
class AlertContextRoots:
    """各产品的会话文件根目录。"""

    codex_sessions: tuple[Path, ...] = ()
    claude_projects: tuple[Path, ...] = ()
    kimi_sessions: tuple[Path, ...] = ()


@dataclass(frozen=True)
class _Extraction:
    """一次会话明细提取的结果；events 为 None 表示文件不可读。"""

    events: tuple[dict[str, Any], ...] | None
    truncated: bool
    input_bytes: int
    output_bytes: int
    fallback: bool = False
    event_count: int | None = None


def load_alert_context(
    alert: StoredAlert,
    roots: AlertContextRoots,
    *,
    include_content: bool = False,
) -> dict[str, Any]:
    """定位告警对应的会话并提取时间窗内的事件；返回可直接 JSON 化的字典。"""

    start = float(alert.first_seen_at) - _WINDOW_LEAD_SECONDS
    end = float(alert.last_seen_at) + _WINDOW_TRAIL_SECONDS
    payload: dict[str, Any] = {
        "content_enabled": include_content,
        "found": False,
        "reason": None,
        "product": alert.product,
        "window": {"start": start, "end": end},
        "candidates": 0,
        "session": None,
        "truncated": False,
        "fallback": False,
        "totals": {"events": 0, "input_bytes": 0, "output_bytes": 0},
        "events": [],
    }
    if alert.product not in SUPPORTED_PRODUCTS:
        payload["reason"] = "unsupported_product"
        return payload
    candidates = _find_candidates(alert, roots, start, end)
    payload["candidates"] = len(candidates)
    if not candidates:
        payload["reason"] = "no_session"
        return payload
    extractor = {
        "codex": _extract_codex,
        "claude": _extract_claude,
        "kimi": _extract_kimi,
    }[alert.product]
    # 中文注释：同目录可能并行多个会话，按最后写入时间离告警由近到远尝试：
    # 窗口内有真实事件的优先，其次是兜底（窗口前活动），最后才是空明细。
    best: tuple[Path, float, int, _Extraction] | None = None
    fallback_choice: tuple[Path, float, int, _Extraction] | None = None
    empty_choice: tuple[Path, float, int, _Extraction] | None = None
    for path, mtime, size in sorted(
        candidates, key=lambda item: abs(item[1] - alert.last_seen_at)
    ):
        result = extractor(path, start, end)
        if result.events is None:
            continue
        entry = (path, mtime, size, result)
        if result.events and not result.fallback:
            best = entry
            break
        if result.events and fallback_choice is None:
            fallback_choice = entry
        if not result.events and empty_choice is None:
            empty_choice = entry
    chosen = best or fallback_choice or empty_choice
    if chosen is None:
        payload["reason"] = "unreadable"
        return payload
    path, mtime, size, extraction = chosen
    # 中文注释：内容展示需显式开启，默认响应只保留类型、时间及字节数。
    events = [dict(item) for item in (extraction.events or ())]
    for item in events:
        item["detail"] = _excerpt(item.get("detail")) if include_content else ""
        item["label"] = _excerpt(item.get("label"), 80)
    if alert.product == "codex":
        session_id = _session_id_from_name(path)
    elif alert.product == "claude":
        session_id = path.stem
    else:
        session_id = path.name.removeprefix("session_")
    payload.update(
        found=True,
        reason=None,
        session={
            "path": str(path),
            "session_id": session_id,
            "size": size,
            "modified_at": mtime,
        },
        truncated=extraction.truncated,
        fallback=extraction.fallback,
        totals={
            "events": extraction.event_count
            if extraction.event_count is not None
            else len(events),
            "input_bytes": extraction.input_bytes,
            "output_bytes": extraction.output_bytes,
        },
        events=events,
    )
    return payload


# ---------------------------------------------------------------- 候选定位


def _find_candidates(
    alert: StoredAlert,
    roots: AlertContextRoots,
    start: float,
    end: float,
) -> list[tuple[Path, float, int]]:
    """按 mtime 预筛、cwd 匹配，返回候选会话（路径, 最后写入时间, 体积）。"""

    if alert.product == "codex":
        files = _codex_recent_files(roots.codex_sessions, start, end)
        matched = _match_codex_cwd(files, alert.cwd)
        return [(path, stat.st_mtime, stat.st_size) for path, stat in matched]
    if alert.product == "claude":
        files = _claude_recent_files(roots.claude_projects, alert.cwd, start)
        return [(path, stat.st_mtime, stat.st_size) for path, stat in files]
    return _kimi_candidates(roots.kimi_sessions, alert.cwd, start)


# 中文注释：rollout 文件名内嵌会话开始的本地时间。
_ROLLOUT_NAME = re.compile(
    r"rollout-(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})"
)


def _codex_file_started(path: Path) -> float | None:
    """从 rollout 文件名解析会话开始时间（本地时间），解析失败返回 None。"""

    match = _ROLLOUT_NAME.search(path.name)
    if not match:
        return None
    try:
        naive = datetime(*[int(part) for part in match.groups()])
    except ValueError:
        return None
    return naive.astimezone().timestamp()


def _codex_recent_files(
    roots: Sequence[Path],
    start: float,
    end: float,
) -> list[tuple[Path, Any]]:
    files: list[tuple[Path, Any]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < start - _MTIME_SLACK_SECONDS:
                continue
            # 中文注释：窗口结束后才开始的会话不可能含有窗口内的事件。
            started = _codex_file_started(path)
            if started is not None and started > end + _WINDOW_TRAIL_SECONDS:
                continue
            files.append((path, stat))
    return files


def _match_codex_cwd(
    files: Iterable[tuple[Path, Any]],
    cwd: str | None,
) -> list[tuple[Path, Any]]:
    reader = JsonlSessionReader()
    matched: list[tuple[Path, Any]] = []
    for path, stat in files:
        if not cwd:
            matched.append((path, stat))
            continue
        metadata = reader.read_metadata(path)
        session_cwd = str(metadata.cwd) if metadata and metadata.cwd else None
        if session_cwd and _cwd_matches(cwd, session_cwd):
            matched.append((path, stat))
    return matched


def _claude_recent_files(
    roots: Sequence[Path],
    cwd: str | None,
    start: float,
) -> list[tuple[Path, Any]]:
    slug = _claude_slug(cwd) if cwd else None
    files: list[tuple[Path, Any]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for project_dir in root.iterdir():
            if not project_dir.is_dir():
                continue
            if slug and project_dir.name != slug:
                continue
            for path in project_dir.glob("*.jsonl"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime >= start - _MTIME_SLACK_SECONDS:
                    files.append((path, stat))
    return files


def _kimi_candidates(
    roots: Sequence[Path],
    cwd: str | None,
    start: float,
) -> list[tuple[Path, float, int]]:
    """Kimi 会话按 ``sessions/wd_*/session_*/`` 组织，cwd 在 state.json 里。"""

    out: list[tuple[Path, float, int]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for state_file in root.glob("wd_*/session_*/state.json"):
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            session_cwd = state.get("cwd") if isinstance(state, Mapping) else None
            if cwd and (not isinstance(session_cwd, str) or not _cwd_matches(cwd, session_cwd)):
                continue
            wires: list[Any] = []
            for wire in state_file.parent.glob("agents/*/wire.jsonl"):
                try:
                    wires.append(wire.stat())
                except OSError:
                    continue
            if not wires:
                continue
            latest = max(stat.st_mtime for stat in wires)
            if latest < start - _MTIME_SLACK_SECONDS:
                continue
            out.append((state_file.parent, latest, sum(stat.st_size for stat in wires)))
    return out


def _cwd_matches(alert_cwd: str, session_cwd: str) -> bool:
    """进程 cwd 与会话工作目录一致或互为前缀时视为同一会话现场。"""

    if alert_cwd == session_cwd:
        return True
    return (
        alert_cwd.startswith(session_cwd.rstrip("/") + "/")
        or session_cwd.startswith(alert_cwd.rstrip("/") + "/")
    )


def _claude_slug(cwd: str) -> str:
    """Claude Code 把 cwd 里的非字母数字字符替换成 '-' 作为目录名。"""

    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _session_id_from_name(path: Path) -> str | None:
    match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        path.name,
    )
    return match.group(1) if match else None


# ---------------------------------------------------------------- 通用扫描


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


def _window_dates(start: float, end: float) -> tuple[bytes, ...]:
    """窗口覆盖的 UTC 日期串，用于行级字节预筛，避免整文件 JSON 解析。"""

    days: list[bytes] = []
    current = datetime.fromtimestamp(start, tz=timezone.utc).date()
    last = datetime.fromtimestamp(end, tz=timezone.utc).date()
    while current <= last:
        days.append(current.isoformat().encode("ascii"))
        current += timedelta(days=1)
    return tuple(days)


def _is_readable(path: Path) -> bool:
    try:
        with path.open("rb"):
            return True
    except OSError:
        return False


def _iter_records(path: Path, dates: tuple[bytes, ...] | None) -> Iterable[Mapping[str, Any]]:
    """逐行产出 JSON 记录；给出日期时先做字节级预筛（调用方先查可读性）。"""

    try:
        with path.open("rb") as handle:
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
        for key in ("cmd", "command", "file_path", "path", "filename", "query", "pattern"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return _excerpt(value)
        return _excerpt(json.dumps(parsed, ensure_ascii=False))
    return _excerpt(arguments)


# 中文注释：map_record 把一条原始记录映射成 0..n 个
# （事件, 上行字节, 输出字节）；parse_ts 给出该格式的记录时间戳。
_MapFn = Callable[[Mapping[str, Any], float], list[tuple[dict[str, Any], int, int]]]
_TsFn = Callable[[Mapping[str, Any]], float | None]


def _scan_session(
    path: Path,
    *,
    dates: tuple[bytes, ...] | None,
    parse_ts: _TsFn,
    map_record: _MapFn,
    start: float | None,
    end: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, int, int, int]:
    """单遍扫描会话文件：窗口内事件（带截断）+ 窗口前最近的若干条。

    ``start`` 为 None 时只收集窗口前的事件（兜底第二遍，不做日期预筛）。
    """

    in_window: list[dict[str, Any]] = []
    before: deque[dict[str, Any]] = deque(maxlen=_FALLBACK_TAIL)
    truncated = False
    input_bytes = 0
    output_bytes = 0
    event_count = 0
    for record in _iter_records(path, dates):
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


def _finish(
    path: Path,
    *,
    prefilter: bool,
    parse_ts: _TsFn,
    map_record: _MapFn,
    start: float,
    end: float,
) -> _Extraction:
    """先扫窗口日期；窗口空时全量扫第二遍，取告警前最近的活动兜底。"""

    if not _is_readable(path):
        return _Extraction(None, False, 0, 0)
    in_window, _, truncated, input_bytes, output_bytes, event_count = _scan_session(
        path,
        dates=_window_dates(start, end) if prefilter else None,
        parse_ts=parse_ts,
        map_record=map_record,
        start=start,
        end=end,
    )
    if in_window:
        return _Extraction(
            tuple(in_window),
            truncated,
            input_bytes,
            output_bytes,
            event_count=event_count,
        )
    if not prefilter:
        # 无日期预筛的格式第一遍就已收集到窗口前事件，这里直接没有。
        return _Extraction((), False, 0, 0)
    _, before, _, _, _, _ = _scan_session(
        path,
        dates=None,
        parse_ts=parse_ts,
        map_record=map_record,
        start=None,
        end=start,
    )
    if not before:
        return _Extraction((), False, 0, 0)
    fallback_in = sum(
        item["size"] or 0 for item in before if item["kind"] in ("user", "tool")
    )
    fallback_out = sum(
        item["size"] or 0 for item in before if item["kind"] == "tool_output"
    )
    return _Extraction(tuple(before), False, fallback_in, fallback_out, fallback=True)


# ---------------------------------------------------------------- Codex


def _iso_record_ts(record: Mapping[str, Any]) -> float | None:
    return _parse_ts(record.get("timestamp"))


def _codex_map(
    record: Mapping[str, Any], timestamp: float
) -> list[tuple[dict[str, Any], int, int]]:
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return []
    kind = payload.get("type")
    if record.get("type") == "event_msg" and kind == "user_message":
        text = str(payload.get("message") or "")
        size = len(text.encode("utf-8", errors="replace"))
        return [
            (
                {
                    "t": timestamp,
                    "kind": "user",
                    "label": "",
                    "detail": _excerpt(text),
                    "size": size,
                },
                size,
                0,
            )
        ]
    if kind in ("function_call", "custom_tool_call"):
        name = str(payload.get("name") or "")
        arguments = str(payload.get("arguments") or payload.get("input") or "")
        size = len(arguments.encode("utf-8", errors="replace"))
        return [
            (
                {
                    "t": timestamp,
                    "kind": "tool",
                    "label": name,
                    "detail": _tool_detail(arguments),
                    "size": size,
                },
                size,
                0,
            )
        ]
    if kind in ("function_call_output", "custom_tool_call_output"):
        output = payload.get("output")
        text = (
            output
            if isinstance(output, str)
            else json.dumps(output, ensure_ascii=False, default=str)
        )
        size = len(text.encode("utf-8", errors="replace"))
        return [
            (
                {
                    "t": timestamp,
                    "kind": "tool_output",
                    "label": "",
                    "detail": "",
                    "size": size,
                },
                0,
                size,
            )
        ]
    if kind == "web_search_call":
        action = payload.get("action")
        query = action.get("query") if isinstance(action, Mapping) else None
        return [
            (
                {
                    "t": timestamp,
                    "kind": "search",
                    "label": "",
                    "detail": _excerpt(query or ""),
                    "size": None,
                },
                0,
                0,
            )
        ]
    return []


def _extract_codex(path: Path, start: float, end: float) -> _Extraction:
    return _finish(
        path,
        prefilter=True,
        parse_ts=_iso_record_ts,
        map_record=_codex_map,
        start=start,
        end=end,
    )


# ---------------------------------------------------------------- Claude


def _claude_map(
    record: Mapping[str, Any], timestamp: float
) -> list[tuple[dict[str, Any], int, int]]:
    if record.get("isSidechain"):
        return []
    message = record.get("message")
    if not isinstance(message, Mapping):
        return []
    content = message.get("content")
    items: list[tuple[dict[str, Any], int, int]] = []
    if record.get("type") == "user":
        if isinstance(content, str):
            size = len(content.encode("utf-8", errors="replace"))
            items.append(
                (
                    {
                        "t": timestamp,
                        "kind": "user",
                        "label": "",
                        "detail": _excerpt(content),
                        "size": size,
                    },
                    size,
                    0,
                )
            )
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                if block.get("type") == "text":
                    text = str(block.get("text") or "")
                    size = len(text.encode("utf-8", errors="replace"))
                    items.append(
                        (
                            {
                                "t": timestamp,
                                "kind": "user",
                                "label": "",
                                "detail": _excerpt(text),
                                "size": size,
                            },
                            size,
                            0,
                        )
                    )
                    continue
                if block.get("type") != "tool_result":
                    continue
                body = block.get("content")
                if isinstance(body, list):
                    text = " ".join(
                        str(part.get("text") or "")
                        for part in body
                        if isinstance(part, Mapping)
                    )
                else:
                    text = str(body or "")
                size = len(text.encode("utf-8", errors="replace"))
                items.append(
                    (
                        {
                            "t": timestamp,
                            "kind": "tool_output",
                            "label": "",
                            "detail": "",
                            "size": size,
                        },
                        0,
                        size,
                    )
                )
    elif record.get("type") == "assistant" and isinstance(content, list):
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "tool_use":
                raw = json.dumps(block.get("input") or {}, ensure_ascii=False)
                size = len(raw.encode("utf-8", errors="replace"))
                items.append(
                    (
                        {
                            "t": timestamp,
                            "kind": "tool",
                            "label": str(block.get("name") or ""),
                            "detail": _tool_detail(raw),
                            "size": size,
                        },
                        size,
                        0,
                    )
                )
    return items


def _extract_claude(path: Path, start: float, end: float) -> _Extraction:
    return _finish(
        path,
        prefilter=True,
        parse_ts=_iso_record_ts,
        map_record=_claude_map,
        start=start,
        end=end,
    )


# ---------------------------------------------------------------- Kimi


def _kimi_record_ts(record: Mapping[str, Any]) -> float | None:
    """wire.jsonl 的时间是毫秒 epoch。"""

    value = record.get("time")
    if isinstance(value, (int, float)):
        return float(value) / 1000.0
    return None


def _kimi_message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "text"
        )
    return ""


def _kimi_map(
    record: Mapping[str, Any], timestamp: float
) -> list[tuple[dict[str, Any], int, int]]:
    record_type = record.get("type")
    if record_type == "context.append_message":
        message = record.get("message")
        if not isinstance(message, Mapping):
            return []
        origin = message.get("origin")
        if (
            message.get("role") != "user"
            or not isinstance(origin, Mapping)
            or origin.get("kind") != "user"
        ):
            return []
        text = _kimi_message_text(message)
        if not text.strip():
            return []
        size = len(text.encode("utf-8", errors="replace"))
        return [
            (
                {
                    "t": timestamp,
                    "kind": "user",
                    "label": "",
                    "detail": _excerpt(text),
                    "size": size,
                },
                size,
                0,
            )
        ]
    if record_type == "context.append_loop_event":
        event = record.get("event")
        if not isinstance(event, Mapping):
            return []
        event_type = event.get("type")
        if event_type == "tool.call":
            raw_args = json.dumps(event.get("args") or {}, ensure_ascii=False)
            size = len(raw_args.encode("utf-8", errors="replace"))
            return [
                (
                    {
                        "t": timestamp,
                        "kind": "tool",
                        "label": str(event.get("name") or ""),
                        "detail": _tool_detail(raw_args),
                        "size": size,
                    },
                    size,
                    0,
                )
            ]
        if event_type == "tool.result":
            result = event.get("result")
            output = result.get("output") if isinstance(result, Mapping) else None
            text = (
                output
                if isinstance(output, str)
                else json.dumps(output, ensure_ascii=False, default=str)
            )
            if not text:
                return []
            size = len(text.encode("utf-8", errors="replace"))
            return [
                (
                    {
                        "t": timestamp,
                        "kind": "tool_output",
                        "label": "",
                        "detail": "",
                        "size": size,
                    },
                    0,
                    size,
                )
            ]
    return []


def _extract_kimi(session_dir: Path, start: float, end: float) -> _Extraction:
    """Kimi 一个会话的 agents/*/wire.jsonl 都要扫，合并后统一排序。"""

    wires = sorted(session_dir.glob("agents/*/wire.jsonl"))
    if not wires:
        return _Extraction(None, False, 0, 0)
    combined: list[dict[str, Any]] = []
    before: list[dict[str, Any]] = []
    truncated = False
    input_bytes = 0
    output_bytes = 0
    readable = False
    event_count = 0
    for wire in wires:
        if not _is_readable(wire):
            continue
        readable = True
        in_window, wire_before, wire_truncated, delta_in, delta_out, wire_count = (
            _scan_session(
                wire,
                dates=None,
                parse_ts=_kimi_record_ts,
                map_record=_kimi_map,
                start=start,
                end=end,
            )
        )
        event_count += wire_count
        combined.extend(in_window)
        before.extend(wire_before)
        truncated = truncated or wire_truncated
        input_bytes += delta_in
        output_bytes += delta_out
    if not readable:
        return _Extraction(None, False, 0, 0)
    if combined:
        combined.sort(key=lambda item: item["t"])
        truncated = truncated or len(combined) > _MAX_EVENTS
        return _Extraction(
            tuple(combined[:_MAX_EVENTS]),
            truncated,
            input_bytes,
            output_bytes,
            event_count=event_count,
        )
    tail = sorted(before, key=lambda item: item["t"])[-_FALLBACK_TAIL:]
    if not tail:
        return _Extraction((), False, 0, 0)
    fallback_in = sum(
        item["size"] or 0 for item in tail if item["kind"] in ("user", "tool")
    )
    fallback_out = sum(
        item["size"] or 0 for item in tail if item["kind"] == "tool_output"
    )
    return _Extraction(tuple(tail), False, fallback_in, fallback_out, fallback=True)
