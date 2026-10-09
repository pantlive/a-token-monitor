"""Grok CLI：会话目录定位与 updates 记录提取。"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..alert_activity import describe_tool_activity, describe_user_text
from ..grok import decode_grok_project
from .common import (
    _Candidate,
    _Extraction,
    _GROK_MS_THRESHOLD,
    _MTIME_SLACK_SECONDS,
    _ToolCall,
    _cwd_matches,
    _excerpt,
    _extraction_from_scan,
    _image_event,
    _is_readable,
    _parse_ts,
    _remember_tool_call,
    _scan_session,
    _suffix_offset,
    _tool_detail,
    _tool_output_info,
)


def _grok_candidates(
    roots: Sequence[Path],
    cwd: str | None,
    start: float,
) -> list[_Candidate]:
    """只扫 sessions/<项目>/<会话>/updates.jsonl，不递归整棵会话树。"""

    out: list[_Candidate] = []
    cutoff = start - _MTIME_SLACK_SECONDS
    for root in roots:
        if not root.is_dir():
            continue
        try:
            projects = [entry for entry in root.iterdir() if entry.is_dir()]
        except OSError:
            continue
        for project_dir in projects:
            try:
                sessions = [entry for entry in project_dir.iterdir() if entry.is_dir()]
            except OSError:
                continue
            for session_dir in sessions:
                log = session_dir / "updates.jsonl"
                try:
                    stat = log.stat()
                except OSError:
                    continue
                if not log.is_file() or stat.st_mtime < cutoff:
                    continue
                session_cwd = _grok_session_cwd(session_dir, project_dir.name)
                if cwd and (
                    not session_cwd or not _cwd_matches(cwd, session_cwd)
                ):
                    continue
                out.append(_Candidate(log, stat.st_mtime, stat.st_size))
    return out


def _grok_session_cwd(session_dir: Path, project_name: str) -> str | None:
    """summary.json 的 info.cwd 优先；缺失时用项目目录名解码。"""

    summary = session_dir / "summary.json"
    try:
        payload = json.loads(summary.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, Mapping):
        info = payload.get("info")
        if isinstance(info, Mapping):
            cwd = info.get("cwd")
            if isinstance(cwd, str) and cwd:
                return cwd
    return decode_grok_project(project_name)


def _grok_record_ts(record: Mapping[str, Any]) -> float | None:
    """updates.jsonl 的 timestamp 是 unix 秒；过大的数字按毫秒处理。"""

    value = record.get("timestamp")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > _GROK_MS_THRESHOLD:
            return number / 1000.0
        return number
    return _parse_ts(value)


def _grok_update(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    params = record.get("params")
    if isinstance(params, Mapping):
        update = params.get("update")
        if isinstance(update, Mapping):
            return update
    if isinstance(record.get("sessionUpdate"), str):
        return record
    return None


def _grok_tool_name(update: Mapping[str, Any]) -> str:
    meta = update.get("_meta")
    if isinstance(meta, Mapping):
        tool = meta.get("x.ai/tool")
        if isinstance(tool, Mapping):
            name = tool.get("name")
            if isinstance(name, str) and name.strip():
                return name
    title = update.get("title")
    return title if isinstance(title, str) else ""


def _grok_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(content, list):
        return " ".join(
            part for part in (_grok_text(item) for item in content) if part
        )
    return ""


def _grok_user_event(
    text: str, timestamp: float
) -> tuple[dict[str, Any], int, int] | None:
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


def _grok_user_events(
    update: Mapping[str, Any], timestamp: float
) -> list[tuple[dict[str, Any], int, int]]:
    content = update.get("content")
    blocks = content if isinstance(content, list) else [content]
    items: list[tuple[dict[str, Any], int, int]] = []
    for block in blocks:
        if isinstance(block, str):
            event = _grok_user_event(block, timestamp)
            if event is not None:
                items.append(event)
            continue
        if not isinstance(block, Mapping):
            continue
        if block.get("type") in {"image", "image_url", "input_image"}:
            items.append(_image_event(block, timestamp))
            continue
        text = block.get("text")
        if isinstance(text, str):
            event = _grok_user_event(text, timestamp)
            if event is not None:
                items.append(event)
    return items


def _grok_output_text(update: Mapping[str, Any]) -> str:
    raw = update.get("rawOutput")
    if isinstance(raw, str):
        text = raw
    elif raw is None:
        text = ""
    else:
        text = json.dumps(raw, ensure_ascii=False, default=str)
        if text in {"{}", "[]", "null"}:
            text = ""
    if text:
        return text
    return _grok_text(update.get("content"))


def _grok_map(
    record: Mapping[str, Any],
    timestamp: float,
    *,
    tool_calls: dict[str, _ToolCall] | None = None,
) -> list[tuple[dict[str, Any], int, int]]:
    update = _grok_update(record)
    if update is None:
        return []
    kind = update.get("sessionUpdate")
    if kind == "user_message_chunk":
        return _grok_user_events(update, timestamp)
    if kind == "tool_call":
        name = _grok_tool_name(update)
        raw_input = update.get("rawInput")
        if not isinstance(raw_input, Mapping):
            raw_input = {}
        activities = describe_tool_activity(name, raw_input)
        label = name or str(update.get("title") or "")
        _remember_tool_call(
            tool_calls,
            update.get("toolCallId"),
            _ToolCall(label, tuple(activities)),
        )
        raw = json.dumps(raw_input, ensure_ascii=False)
        size = len(raw.encode("utf-8", errors="replace"))
        return [
            (
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
        ]
    if kind != "tool_call_update":
        return []
    # 中文注释：同一次调用会先写进行中、再写完成。只保留终态，避免输出重复。
    if update.get("status") not in {"completed", "failed"}:
        return []
    text = _grok_output_text(update)
    if not text:
        return []
    size = len(text.encode("utf-8", errors="replace"))
    return [
        (
            {
                "t": timestamp,
                "kind": "tool_output",
                **_tool_output_info(tool_calls, update.get("toolCallId")),
                "detail": "",
                "size": size,
            },
            0,
            size,
        )
    ]


def _extract_grok(path: Path, start: float, end: float) -> _Extraction:
    """unix 时间戳从文件尾部定位后正向扫描；窗口为空时保留窗口前事件。"""

    if not _is_readable(path):
        return _Extraction(None, False, 0, 0)
    return _extraction_from_scan(
        _scan_session(
            path,
            dates=None,
            parse_ts=_grok_record_ts,
            map_record=partial(_grok_map, tool_calls={}),
            start=start,
            end=end,
            offset=_suffix_offset(path, start, _grok_record_ts),
        )
    )
