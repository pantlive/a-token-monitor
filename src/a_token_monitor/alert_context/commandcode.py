"""Command Code：项目会话定位与事件提取。"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..commandcode import (
    _commandcode_project_dir,
    _session_id_from_filename,
    read_commandcode_session_info,
)
from .claude import (
    _claude_map,
)
from .common import (
    _Candidate,
    _Extraction,
    _MTIME_SLACK_SECONDS,
    _ToolCall,
    _cwd_matches,
    _finish,
    _iso_record_ts,
)


def _commandcode_candidates(
    roots: Sequence[Path],
    cwd: str | None,
    start: float,
) -> list[_Candidate]:
    """按项目 slug 定位主会话 JSONL，再用会话头里的 cwd 校验一次。"""

    out: list[_Candidate] = []
    cutoff = start - _MTIME_SLACK_SECONDS
    for root in roots:
        if not root.is_dir():
            continue
        for project_dir in _commandcode_project_dirs(root, cwd):
            try:
                entries = tuple(project_dir.iterdir())
            except OSError:
                continue
            for path in entries:
                if _session_id_from_filename(path.name) is None:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if not path.is_file() or stat.st_mtime < cutoff:
                    continue
                if cwd and not _commandcode_header_matches(path, cwd):
                    continue
                out.append(_Candidate(path, stat.st_mtime, stat.st_size))
    return out


def _commandcode_project_dirs(root: Path, cwd: str | None) -> list[Path]:
    if not cwd:
        try:
            return [entry for entry in root.iterdir() if entry.is_dir()]
        except OSError:
            return []
    project = _commandcode_project_dir(root, cwd)
    return [project] if project is not None else []


def _commandcode_header_matches(path: Path, cwd: str) -> bool:
    info = read_commandcode_session_info(path)
    session_cwd = info.cwd if info is not None else None
    return bool(session_cwd) and _cwd_matches(cwd, session_cwd)


def _commandcode_map(
    record: Mapping[str, Any],
    timestamp: float,
    *,
    tool_calls: dict[str, _ToolCall] | None = None,
) -> list[tuple[dict[str, Any], int, int]]:
    """Command Code 用 type=message 加 message.role，内容块与 Claude 相同。"""

    if record.get("type") != "message":
        return []
    message = record.get("message")
    if not isinstance(message, Mapping):
        return []
    role = message.get("role")
    if role not in {"user", "assistant"}:
        return []
    return _claude_map(
        {"type": role, "message": message},
        timestamp,
        tool_calls=tool_calls,
    )


def _extract_commandcode(path: Path, start: float, end: float) -> _Extraction:
    return _finish(
        path,
        parse_ts=_iso_record_ts,
        map_record=partial(_commandcode_map, tool_calls={}),
        start=start,
        end=end,
    )
