"""Gemini CLI / Qwen Code：chats 目录定位与记录提取（两者格式相同）。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .common import (
    _BLOB_READ_LIMIT,
    _Candidate,
    _Extraction,
    _MTIME_SLACK_SECONDS,
    _ToolCall,
    _as_text,
    _bucket_items,
    _content_text,
    _cwd_matches,
    _existing_dir,
    _header_cwd,
    _ids_for_cwd,
    _image_event,
    _is_readable,
    _iter_records,
    _output_item,
    _parse_ts,
    _path_slugs,
    _suffix_offset,
    _tool_item,
    _user_item,
)


def _chat_candidates(
    homes: Sequence[Path],
    cwd: str | None,
    start: float,
    *,
    layouts: tuple[str, ...],
) -> list[_Candidate]:
    out: list[_Candidate] = []
    cutoff = start - _MTIME_SLACK_SECONDS
    for home in homes:
        if not home.is_dir():
            continue
        allowed = _ids_for_cwd(home, cwd) if cwd else None
        slugs = _path_slugs(cwd) if cwd else None
        chat_dirs: list[tuple[Path, bool]] = []
        if "tmp" in layouts:
            tmp = home / "tmp"
            if tmp.is_dir():
                try:
                    projects = [entry for entry in tmp.iterdir() if entry.is_dir()]
                except OSError:
                    projects = []
                for project in projects:
                    if allowed is not None and project.name not in allowed:
                        continue
                    chats = project / "chats"
                    if chats.is_dir():
                        chat_dirs.append((chats, allowed is not None))
        if "projects" in layouts:
            projects_root = home / "projects"
            if projects_root.is_dir():
                try:
                    projects = [
                        entry for entry in projects_root.iterdir() if entry.is_dir()
                    ]
                except OSError:
                    projects = []
                for project in projects:
                    if slugs is not None and project.name not in slugs:
                        continue
                    chats = project / "chats"
                    if chats.is_dir():
                        chat_dirs.append((chats, slugs is not None))
        for chats, trusted in chat_dirs:
            try:
                entries = list(chats.iterdir())
            except OSError:
                continue
            for path in entries:
                if path.suffix not in {".json", ".jsonl"} or not path.is_file():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime < cutoff:
                    continue
                if cwd and not trusted:
                    session_cwd = _header_cwd(path)
                    if not session_cwd or not _cwd_matches(cwd, session_cwd):
                        continue
                out.append(_Candidate(path, stat.st_mtime, stat.st_size))
    return out


def _gemini_candidates(
    homes: Sequence[Path], cwd: str | None, start: float
) -> list[_Candidate]:
    return _chat_candidates(homes, cwd, start, layouts=("tmp",))


def _qwen_candidates(
    homes: Sequence[Path], cwd: str | None, start: float
) -> list[_Candidate]:
    return _chat_candidates(homes, cwd, start, layouts=("tmp", "projects"))


def _gemini_record_ts(record: Mapping[str, Any]) -> float | None:
    return _parse_ts(record.get("timestamp") or record.get("startTime"))


def _gemini_tool_items(
    record: Mapping[str, Any],
    timestamp: float,
    tool_calls: dict[str, _ToolCall],
) -> list[tuple[dict[str, Any], int, int]]:
    """只取工具调用和结果，不保留模型正文与思考。"""

    calls: list[Mapping[str, Any]] = []
    raw_calls = record.get("toolCalls")
    if isinstance(raw_calls, list):
        calls.extend(item for item in raw_calls if isinstance(item, Mapping))
    content = record.get("content")
    responses: list[tuple[object, object]] = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, Mapping):
                continue
            function_call = part.get("functionCall")
            if isinstance(function_call, Mapping):
                calls.append(
                    {
                        "name": function_call.get("name"),
                        "args": function_call.get("args"),
                        "id": part.get("id") or function_call.get("id"),
                    }
                )
            elif part.get("type") in {"functionCall", "tool_use"}:
                calls.append(part)
            function_response = part.get("functionResponse")
            if isinstance(function_response, Mapping):
                responses.append(
                    (
                        part.get("id") or function_response.get("id"),
                        function_response.get("response"),
                    )
                )
            elif part.get("type") in {"functionResponse", "tool_result"}:
                responses.append((part.get("id") or part.get("tool_use_id"), part.get("content")))
    items: list[tuple[dict[str, Any], int, int]] = []
    for call in calls:
        name = str(call.get("name") or "")
        call_id = call.get("id")
        items.append(
            _tool_item(name, call.get("args") or call.get("input"), timestamp, call_id, tool_calls)
        )
        result = call.get("result")
        output = _output_item(_as_text(result), timestamp, call_id, tool_calls)
        if output is not None:
            items.append(output)
    for call_id, body in responses:
        output = _output_item(_as_text(body) or _content_text(body), timestamp, call_id, tool_calls)
        if output is not None:
            items.append(output)
    return items


def _rewind_target(record: Mapping[str, Any]) -> str | None:
    for key in ("targetMessageId", "targetId", "messageId", "target"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _gemini_items(
    records: Iterable[Mapping[str, Any]],
) -> list[tuple[dict[str, Any], int, int]]:
    """按写入顺序折叠：同 id 覆盖，$rewindTo 丢掉目标及其后的消息。"""

    live: list[tuple[str | None, list[tuple[dict[str, Any], int, int]]]] = []
    tool_calls: dict[str, _ToolCall] = {}
    for record in records:
        kind = str(record.get("type") or "")
        if kind == "$rewindTo":
            target = _rewind_target(record)
            index = next(
                (slot for slot, item in enumerate(live) if item[0] == target),
                None,
            )
            if index is not None:
                del live[index:]
            continue
        if kind in {"$set", "session_metadata"}:
            continue
        timestamp = _gemini_record_ts(record)
        if timestamp is None:
            continue
        message_id = record.get("id") if isinstance(record.get("id"), str) else None
        if kind == "message_update":
            extra = _gemini_tool_items(record, timestamp, tool_calls)
            if not extra:
                continue
            if message_id:
                for slot, (existing_id, existing) in enumerate(live):
                    if existing_id != message_id:
                        continue
                    kept = [item for item in existing if item[0]["kind"] == "user"]
                    live[slot] = (message_id, kept + extra)
                    break
                else:
                    live.append((message_id, extra))
            else:
                live.append((None, extra))
            continue
        items: list[tuple[dict[str, Any], int, int]] = []
        if kind == "user":
            user = _user_item(_content_text(record.get("content")), timestamp)
            if user is not None:
                items.append(user)
            content = record.get("content")
            if isinstance(content, list):
                items.extend(
                    _image_event(part, timestamp)
                    for part in content
                    if isinstance(part, Mapping)
                    and part.get("type") in {"image", "image_url", "input_image"}
                )
        elif kind in {"gemini", "model"}:
            items.extend(_gemini_tool_items(record, timestamp, tool_calls))
        else:
            continue
        if message_id:
            for slot, (existing_id, _) in enumerate(live):
                if existing_id == message_id:
                    live[slot] = (message_id, items)
                    break
            else:
                live.append((message_id, items))
        elif items:
            live.append((None, items))
    return [item for _, events in live for item in events]


def _gemini_blob_records(path: Path) -> list[Mapping[str, Any]] | None:
    try:
        if path.stat().st_size > _BLOB_READ_LIMIT:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    records: list[Mapping[str, Any]] = []
    for message in messages:
        if isinstance(message, Mapping):
            records.append(message)
    return records


def _extract_gemini_records(
    records: Iterable[Mapping[str, Any]], start: float, end: float
) -> _Extraction:
    return _bucket_items(_gemini_items(records), start, end)


def _extract_gemini(path: Path, start: float, end: float) -> _Extraction:
    if path.suffix == ".json":
        records = _gemini_blob_records(path)
        if records is None:
            return _Extraction(None, False, 0, 0)
        return _extract_gemini_records(records, start, end)
    if not _is_readable(path):
        return _Extraction(None, False, 0, 0)
    return _extract_gemini_records(
        _iter_records(path, None, _suffix_offset(path, start, _gemini_record_ts)),
        start,
        end,
    )


def _extract_qwen(path: Path, start: float, end: float) -> _Extraction:
    return _extract_gemini(path, start, end)


def _default_gemini_homes() -> tuple[Path, ...]:
    configured = os.environ.get("GEMINI_CLI_HOME")
    if configured:
        raw = Path(configured).expanduser()
        home = raw if raw.name == ".gemini" else raw / ".gemini"
    else:
        home = Path.home() / ".gemini"
    return _existing_dir(home)


def _default_qwen_homes() -> tuple[Path, ...]:
    configured = os.environ.get("QWEN_HOME") or os.environ.get("QWEN_CODE_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".qwen"
    return _existing_dir(home)
