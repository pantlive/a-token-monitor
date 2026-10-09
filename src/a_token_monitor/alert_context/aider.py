"""Aider：.aider.chat.history.md 定位与操作提取。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Sequence

from ..alert_activity import describe_tool_activity
from .common import (
    _Candidate,
    _Extraction,
    _FALLBACK_TAIL,
    _MAX_EVENTS,
    _MTIME_SLACK_SECONDS,
    _cwd_matches,
    _existing_dir,
    _tail_text_lines,
)


_AIDER_EDIT = re.compile(r"^> Applied edit to (?P<path>\S.*?)\s*$")


_AIDER_EMPTY = re.compile(r"^> Creating empty file (?P<path>\S.*?)\s*$")


_AIDER_RUN = re.compile(r"^> Running (?P<command>\S.*?)\s*$")


def _aider_candidates(
    homes: Sequence[Path],
    cwd: str | None,
    start: float,
) -> list[_Candidate]:
    """每个目录最多一份聊天历史，用文件 mtime 判断是否靠近告警。"""

    out: list[_Candidate] = []
    cutoff = start - _MTIME_SLACK_SECONDS
    for home in homes:
        path = Path(home) / ".aider.chat.history.md"
        try:
            stat = path.stat()
        except OSError:
            continue
        if not path.is_file() or stat.st_mtime < cutoff:
            continue
        if cwd and not _cwd_matches(cwd, str(path.parent)):
            continue
        out.append(_Candidate(path, stat.st_mtime, stat.st_size))
    return out


def _aider_action(text: str) -> tuple[str, dict[str, str]] | None:
    """只认编辑和命令。用户正文、跳过的编辑和 token 行都不算活动。"""

    edited = _AIDER_EDIT.match(text) or _AIDER_EMPTY.match(text)
    if edited is not None:
        return "edit", {"path": edited.group("path")}
    running = _AIDER_RUN.match(text)
    if running is None:
        return None
    return "shell", {"command": running.group("command")}


def _extract_aider(path: Path, start: float, end: float) -> _Extraction:
    """历史没有逐条时间。文件落在窗口内时，把尾部工具行当作当时的活动。"""

    tailed = _tail_text_lines(path, _MAX_EVENTS)
    if tailed is None:
        return _Extraction(None, False, 0, 0)
    lines, truncated = tailed
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _Extraction(None, False, 0, 0)
    items: list[tuple[dict[str, Any], int, int]] = []
    for raw in lines:
        action = _aider_action(raw.strip())
        if action is None:
            continue
        name, argument = action
        activities = describe_tool_activity(name, argument)
        detail = next(iter(argument.values()), "")
        size = len(detail.encode("utf-8"))
        items.append(
            (
                {
                    "t": mtime,
                    "kind": "tool",
                    "label": "",
                    "detail": detail,
                    "size": size,
                    "activities": activities,
                },
                size,
                0,
            )
        )
    if not items:
        return _Extraction((), False, 0, 0)
    if start <= mtime <= end:
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


def _default_aider_homes() -> tuple[Path, ...]:
    configured = os.environ.get("AIDER_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".aider"
    return _existing_dir(home)
