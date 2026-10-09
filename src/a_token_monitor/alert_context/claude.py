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
    _Request,
    _ToolCall,
    _claude_slug,
    _excerpt,
    _finish,
    _image_event,
    _iso_record_ts,
    _remember_tool_call,
    _tool_detail,
    _tool_output_info,
    _token_count,
    _with_requests,
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


def _remember_claude_request(
    requests: dict[str, _Request],
    record: Mapping[str, Any],
    message: Mapping[str, Any],
    timestamp: float,
) -> None:
    """记下一次模型请求的上下文大小：未缓存输入 + 读缓存 + 写缓存。

    同一次请求会按内容块拆成多条 assistant 记录，用 requestId（或消息 ID）去重。
    """

    usage = message.get("usage")
    if not isinstance(usage, Mapping):
        return
    tokens = sum(
        _token_count(usage.get(key))
        for key in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    )
    key = record.get("requestId") or message.get("id")
    if not tokens or not isinstance(key, str) or not key:
        return
    previous = requests.get(key)
    if previous is None or tokens > previous[1]:
        requests[key] = (previous[0] if previous else timestamp, tokens)


def _claude_map(
    record: Mapping[str, Any],
    timestamp: float,
    *,
    tool_calls: dict[str, _ToolCall] | None = None,
    requests: dict[str, _Request] | None = None,
) -> list[tuple[dict[str, Any], int, int]]:
    if record.get("isSidechain"):
        return []
    message = record.get("message")
    if not isinstance(message, Mapping):
        return []
    if requests is not None and record.get("type") == "assistant":
        _remember_claude_request(requests, record, message, timestamp)
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
    requests: dict[str, _Request] = {}
    extraction = _finish(
        path,
        parse_ts=_iso_record_ts,
        map_record=partial(_claude_map, tool_calls={}, requests=requests),
        start=start,
        end=end,
    )
    return _with_requests(extraction, requests.values(), start, end)
