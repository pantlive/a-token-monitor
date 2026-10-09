"""Codex CLI：rollout 文件定位与事件提取。"""

from __future__ import annotations

import json
import re
from hashlib import sha256
from collections import deque
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..local_time import local_naive_to_timestamp
from ..alert_activity import describe_tool_activity, describe_user_text
from ..discovery import JsonlSessionReader
from .common import (
    _Extraction,
    _MTIME_SLACK_SECONDS,
    _ToolCall,
    _WINDOW_TRAIL_SECONDS,
    _cwd_matches,
    _excerpt,
    _finish,
    _image_event,
    _iso_record_ts,
    _remember_tool_call,
    _tool_detail,
    _tool_output_info,
)


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
    return local_naive_to_timestamp(naive)


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


# 中文注释：跳过字符串及注释，只识别包装器中静态写出的 tools.xxx(...) 调用。
# 不执行日志里的 JavaScript，无法识别的动态调用仍保留原始包装器名称。
_JS_TOOL_CALLS = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/"
    r'|"(?:\\.|[^"\\])*"'
    r"|'(?:\\.|[^'\\])*'"
    r"|`(?:\\.|[^`\\])*`"
    r"|(?<![\w$.])tools\s*"
    r"(?:\.\s*(?P<name>[A-Za-z_$][\w$]*)"
    r"|\[\s*(?P<quote>[\"'])(?P<key>[A-Za-z_$][\w$]*)(?P=quote)\s*\])"
    r"\s*(?:\?\.\s*)?\("
)


def _codex_tool_label(name: str, arguments: str) -> str:
    """为 Codex 的 exec 包装器补充静态工具名称，保留原始调用关系。"""

    if name not in {"exec", "functions.exec", "functions__exec"}:
        return name
    # 中文注释：同一包装器可调用多个工具，按首次出现顺序去重展示。
    names = dict.fromkeys(
        match.group("name") or match.group("key")
        for match in _JS_TOOL_CALLS.finditer(arguments)
        if match.group("name") or match.group("key")
    )
    return f"{name} → {' · '.join(names)}" if names else name


def _codex_user_events(
    payload: Mapping[str, Any],
    timestamp: float,
    seen: deque[tuple[float, str, str]] | None,
) -> list[tuple[dict[str, Any], int, int]]:
    """兼容旧用户事件与新的消息块，并消除同一输入的双格式记录。"""

    legacy = payload.get("type") == "user_message"
    content = payload.get("content")
    blocks = content if isinstance(content, list) else []
    text = (
        str(payload.get("message") or "")
        if legacy
        else "\n".join(
            str(part.get("text") or "")
            for part in blocks
            if isinstance(part, Mapping) and part.get("type") in {"input_text", "text"}
        )
    )
    images = (
        [
            part
            for key in ("images", "local_images")
            for part in (payload.get(key) if isinstance(payload.get(key), list) else [])
        ]
        if legacy
        else [
            part
            for part in blocks
            if isinstance(part, Mapping)
            and part.get("type") in {"input_image", "image", "image_url"}
        ]
    )
    result: list[tuple[dict[str, Any], int, int]] = []
    source = "legacy" if legacy else "message"
    # 中文注释：仅消除一秒内不同日志格式的同内容记录，保留真实的重复输入。
    if text:
        fingerprint = sha256(text.encode("utf-8", errors="replace")).hexdigest()
        duplicate = next(
            (
                index
                for index, (previous, key, origin) in enumerate(seen or ())
                if abs(timestamp - previous) <= 1
                and fingerprint == key
                and source != origin
            ),
            None,
        )
        if duplicate is None:
            size = len(text.encode("utf-8", errors="replace"))
            result.append(
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
            if seen is not None:
                seen.append((timestamp, fingerprint, source))
        elif seen is not None:
            # 中文注释：双格式记录一对一消重，不能误删紧接着的真实重复输入。
            del seen[duplicate]
    result.extend(_image_event(part, timestamp) for part in images)
    return result


def _codex_map(
    record: Mapping[str, Any],
    timestamp: float,
    *,
    tool_calls: dict[str, _ToolCall] | None = None,
    user_inputs: deque[tuple[float, str, str]] | None = None,
) -> list[tuple[dict[str, Any], int, int]]:
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return []
    kind = payload.get("type")
    if (record.get("type") == "event_msg" and kind == "user_message") or (
        record.get("type") == "response_item"
        and kind == "message"
        and payload.get("role") == "user"
    ):
        return _codex_user_events(payload, timestamp, user_inputs)
    if kind in ("function_call", "custom_tool_call"):
        name = str(payload.get("name") or "")
        arguments = str(payload.get("arguments") or payload.get("input") or "")
        label = _codex_tool_label(name, arguments)
        activities = describe_tool_activity(name, arguments)
        _remember_tool_call(
            tool_calls, payload.get("call_id"), _ToolCall(label, tuple(activities))
        )
        size = len(arguments.encode("utf-8", errors="replace"))
        return [
            (
                {
                    "t": timestamp,
                    "kind": "tool",
                    "label": label,
                    "detail": _tool_detail(arguments),
                    "size": size,
                    "activities": activities,
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
                    **_tool_output_info(tool_calls, payload.get("call_id")),
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
                    "activities": [
                        {
                            "summary": "检索或读取网络内容",
                            "basis": "record",
                            "target": "",
                        }
                    ],
                },
                0,
                0,
            )
        ]
    return []


def _extract_codex(path: Path, start: float, end: float) -> _Extraction:
    return _finish(
        path,
        parse_ts=_iso_record_ts,
        map_record=partial(_codex_map, tool_calls={}, user_inputs=deque(maxlen=16)),
        start=start,
        end=end,
    )
