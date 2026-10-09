"""Claude Code：项目会话文件定位与事件提取（Command Code、Cursor 复用其记录格式）。"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..alert_activity import describe_tool_activity, describe_user_text
from .common import (
    _Extraction,
    _MTIME_SLACK_SECONDS,
    _ToolCall,
    _claude_slug,
    _excerpt,
    _finish,
    _image_event,
    _iso_record_ts,
    _remember_tool_call,
    _tool_detail,
    _tool_output_info,
)


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


def _claude_map(
    record: Mapping[str, Any],
    timestamp: float,
    *,
    tool_calls: dict[str, _ToolCall] | None = None,
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
                        "activities": [describe_user_text(content)],
                    },
                    size,
                    0,
                )
            )
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                if block.get("type") == "image":
                    items.append(_image_event(block, timestamp))
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
                                "activities": [describe_user_text(text)],
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
                            **_tool_output_info(tool_calls, block.get("tool_use_id")),
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
                label = str(block.get("name") or "")
                activities = describe_tool_activity(label, block.get("input"))
                _remember_tool_call(
                    tool_calls, block.get("id"), _ToolCall(label, tuple(activities))
                )
                raw = json.dumps(block.get("input") or {}, ensure_ascii=False)
                size = len(raw.encode("utf-8", errors="replace"))
                items.append(
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
                )
    return items


def _extract_claude(path: Path, start: float, end: float) -> _Extraction:
    return _finish(
        path,
        parse_ts=_iso_record_ts,
        map_record=partial(_claude_map, tool_calls={}),
        start=start,
        end=end,
    )
