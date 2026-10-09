"""OpenCode：SQLite 会话库定位与消息提取。"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..local_agents import (
    opencode_db_path,
)
from .common import (
    _Candidate,
    _Extraction,
    _LOOKBACK_RECORDS,
    _MTIME_SLACK_SECONDS,
    _ToolCall,
    _as_text,
    _bucket_items,
    _content_text,
    _cwd_matches,
    _epoch_seconds,
    _existing_file,
    _output_item,
    _tool_item,
    _user_item,
)


def _opencode_connect(path: Path) -> sqlite3.Connection:
    """只读打开会话库，不碰账号和凭据表。"""

    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _opencode_candidates(
    roots: Sequence[Path],
    cwd: str | None,
    start: float,
) -> list[_Candidate]:
    out: list[_Candidate] = []
    cutoff = start - _MTIME_SLACK_SECONDS
    for path in roots:
        if not path.is_file():
            continue
        try:
            connection = _opencode_connect(path)
        except sqlite3.Error:
            continue
        try:
            try:
                rows = connection.execute(
                    "SELECT id, directory, time_updated FROM session"
                ).fetchall()
            except sqlite3.Error:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            for row in rows:
                session_id = row["id"]
                directory = row["directory"]
                updated = _epoch_seconds(row["time_updated"])
                if not isinstance(session_id, str) or updated is None:
                    continue
                if updated < cutoff:
                    continue
                if cwd and (
                    not isinstance(directory, str) or not _cwd_matches(cwd, directory)
                ):
                    continue
                out.append(
                    _Candidate(path, updated, size, session_id=session_id)
                )
        finally:
            connection.close()
    return out


def _opencode_role(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, Mapping):
        return ""
    role = payload.get("role")
    return role if isinstance(role, str) else ""


def _opencode_part_items(
    part: Mapping[str, Any],
    role: str,
    timestamp: float,
    tool_calls: dict[str, _ToolCall],
) -> list[tuple[dict[str, Any], int, int]]:
    kind = part.get("type")
    if kind == "text" and role == "user":
        item = _user_item(_content_text(part.get("text")), timestamp)
        return [item] if item is not None else []
    if kind != "tool":
        return []
    state = part.get("state")
    params = state if isinstance(state, Mapping) else {}
    call_id = part.get("callID") or part.get("callId")
    items = [
        _tool_item(
            str(part.get("tool") or ""),
            params.get("input"),
            timestamp,
            call_id,
            tool_calls,
        )
    ]
    status = params.get("status")
    if status in {"completed", "error"}:
        output = _output_item(
            _as_text(params.get("output")), timestamp, call_id, tool_calls
        )
        if output is not None:
            items.append(output)
    return items


def _extract_opencode(
    path: Path, start: float, end: float, session_id: str
) -> _Extraction:
    if not session_id:
        return _Extraction((), False, 0, 0)
    start_ms = int(start * 1000)
    end_ms = int(end * 1000)
    try:
        connection = _opencode_connect(path)
    except sqlite3.Error:
        return _Extraction(None, False, 0, 0)
    try:
        try:
            before_rows = connection.execute(
                """
                SELECT time_created, data, message_id
                FROM part
                WHERE session_id = ? AND time_created < ?
                ORDER BY time_created DESC
                LIMIT ?
                """,
                (session_id, start_ms, _LOOKBACK_RECORDS),
            ).fetchall()
            window_rows = connection.execute(
                """
                SELECT time_created, data, message_id
                FROM part
                WHERE session_id = ? AND time_created >= ? AND time_created <= ?
                ORDER BY time_created ASC
                """,
                (session_id, start_ms, end_ms),
            ).fetchall()
        except sqlite3.Error:
            return _Extraction(None, False, 0, 0)
        rows = list(reversed(before_rows)) + list(window_rows)
        message_ids = tuple(
            dict.fromkeys(
                row["message_id"]
                for row in rows
                if isinstance(row["message_id"], str) and row["message_id"]
            )
        )
        roles: dict[str, str] = {}
        if message_ids:
            marks = ",".join("?" for _ in message_ids)
            try:
                messages = connection.execute(
                    f"SELECT id, data FROM message WHERE id IN ({marks})",
                    message_ids,
                ).fetchall()
            except sqlite3.Error:
                return _Extraction(None, False, 0, 0)
            roles = {
                row["id"]: _opencode_role(row["data"])
                for row in messages
                if isinstance(row["id"], str)
            }
    finally:
        connection.close()
    tool_calls: dict[str, _ToolCall] = {}
    items: list[tuple[dict[str, Any], int, int]] = []
    for row in rows:
        timestamp = _epoch_seconds(row["time_created"])
        if timestamp is None:
            continue
        try:
            part = json.loads(row["data"] or "")
        except json.JSONDecodeError:
            continue
        if not isinstance(part, Mapping):
            continue
        role = roles.get(row["message_id"], "")
        items.extend(_opencode_part_items(part, role, timestamp, tool_calls))
    return _bucket_items(items, start, end)


def _default_opencode_dbs() -> tuple[Path, ...]:
    configured = os.environ.get("OPENCODE_DB")
    if configured:
        return _existing_file(Path(configured).expanduser())
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return _existing_file(base / "opencode" / "opencode.db")


def _opencode_dbs_from_homes(homes: Sequence[Path]) -> tuple[Path, ...]:
    """显式目录只映射到其中的数据库文件，空元组不退回本机默认库。"""

    found: list[Path] = []
    for home in homes:
        database = opencode_db_path(Path(home))
        if database.is_file():
            found.append(database)
    return tuple(found)
