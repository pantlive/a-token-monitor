"""Kimi Code：会话目录定位与 wire 记录提取。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..alert_activity import describe_tool_activity, describe_user_text
from .common import (
    _Candidate,
    _Extraction,
    _FALLBACK_TAIL,
    _MAX_EVENTS,
    _MTIME_SLACK_SECONDS,
    _cwd_matches,
    _excerpt,
    _image_event,
    _is_readable,
    _scan_session,
    _suffix_offset,
    _tool_detail,
    _tool_output_info,
)


def _kimi_candidates(
    roots: Sequence[Path],
    cwd: str | None,
    start: float,
) -> list[_Candidate]:
    """Kimi 会话按 ``sessions/wd_*/session_*/`` 组织，cwd 在 state.json 里。"""

    out: list[_Candidate] = []
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
            out.append(
                _Candidate(
                    state_file.parent,
                    latest,
                    sum(stat.st_size for stat in wires),
                )
            )
    return out


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
        content = message.get("content")
        images = (
            [
                part
                for part in content
                if isinstance(part, Mapping)
                and part.get("type") in {"image", "image_url", "input_image"}
            ]
            if isinstance(content, list)
            else []
        )
        image_events = [_image_event(part, timestamp) for part in images]
        if not text.strip():
            return image_events
        size = len(text.encode("utf-8", errors="replace"))
        return [
            (
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
        ] + image_events
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
                        "activities": describe_tool_activity(
                            str(event.get("name") or ""), event.get("args")
                        ),
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
                        **_tool_output_info(None, None),
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
                offset=_suffix_offset(wire, start, _kimi_record_ts),
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
        item["size"] or 0 for item in tail if item["kind"] in ("user", "image", "tool")
    )
    fallback_out = sum(
        item["size"] or 0 for item in tail if item["kind"] == "tool_output"
    )
    return _Extraction(tuple(tail), False, fallback_in, fallback_out, fallback=True)
