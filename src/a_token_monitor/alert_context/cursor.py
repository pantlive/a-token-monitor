"""Cursor Agent：agent-transcripts 定位与事件提取。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .claude import (
    _claude_map,
)
from .common import (
    _Candidate,
    _Extraction,
    _FALLBACK_TAIL,
    _MAX_EVENTS,
    _MTIME_SLACK_SECONDS,
    _ToolCall,
    _existing_dir,
    _path_slugs,
    _tail_text_lines,
)


def _cursor_as_claude(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Cursor 行没有独立 type 字段，整理成 Claude 记录再复用解析。"""

    role = record.get("role") or record.get("type")
    if role == "model":
        role = "assistant"
    if role not in {"user", "assistant"}:
        return None
    message = record.get("message")
    if isinstance(message, Mapping):
        return {"type": role, "message": message}
    content = record.get("content")
    if isinstance(content, (str, list)):
        return {"type": role, "message": {"role": role, "content": content}}
    return None


def _cursor_candidates(
    roots: Sequence[Path],
    cwd: str | None,
    start: float,
    end: float,
) -> list[_Candidate]:
    """转录没有逐行时间，用文件 mtime 决定它是否还在告警附近。"""

    del end
    slugs = _path_slugs(cwd) if cwd else None
    out: list[_Candidate] = []
    cutoff = start - _MTIME_SLACK_SECONDS
    for root in roots:
        if not root.is_dir():
            continue
        try:
            projects = [entry for entry in root.iterdir() if entry.is_dir()]
        except OSError:
            continue
        for project in projects:
            if slugs is not None and project.name not in slugs:
                continue
            transcripts = project / "agent-transcripts"
            if not transcripts.is_dir():
                continue
            for path in transcripts.rglob("*.jsonl"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if not path.is_file() or stat.st_mtime < cutoff:
                    continue
                out.append(_Candidate(path, stat.st_mtime, stat.st_size))
    return out


def _extract_cursor(path: Path, start: float, end: float) -> _Extraction:
    """行内没有时间戳。文件在窗口内写过，就把尾部当成当时的活动。"""

    tailed = _tail_text_lines(path, _MAX_EVENTS)
    if tailed is None:
        return _Extraction(None, False, 0, 0)
    lines, truncated = tailed
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _Extraction(None, False, 0, 0)
    tool_calls: dict[str, _ToolCall] = {}
    items: list[tuple[dict[str, Any], int, int]] = []
    for raw in lines:
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, Mapping):
            continue
        shaped = _cursor_as_claude(record)
        if shaped is None:
            continue
        for item, delta_in, delta_out in _claude_map(
            shaped, mtime, tool_calls=tool_calls
        ):
            stamped = dict(item)
            stamped["t"] = mtime
            items.append((stamped, delta_in, delta_out))
    if not items:
        return _Extraction((), False, 0, 0)
    in_window = start <= mtime <= end
    if in_window:
        return _Extraction(
            tuple(item for item, _, _ in items),
            truncated,
            sum(delta for _, delta, _ in items),
            sum(delta for _, _, delta in items),
            event_count=len(items),
        )
    tail = items[-_FALLBACK_TAIL:]
    return _Extraction(
        tuple(item for item, _, _ in tail),
        False,
        sum(delta for _, delta, _ in tail),
        sum(delta for _, _, delta in tail),
        fallback=True,
    )


def _default_cursor_projects() -> tuple[Path, ...]:
    configured = os.environ.get("CURSOR_CONFIG_DIR")
    if configured:
        home = Path(configured).expanduser()
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        home = Path(xdg).expanduser() / "cursor" if xdg else Path.home() / ".cursor"
    return _existing_dir(home / "projects")


def _cursor_projects_from_homes(homes: Sequence[Path]) -> tuple[Path, ...]:
    """Cursor 告警根是配置目录下的 projects，不是配置目录本身。"""

    found: list[Path] = []
    for home in homes:
        projects = Path(home) / "projects"
        if projects.is_dir():
            found.append(projects)
    return tuple(found)
