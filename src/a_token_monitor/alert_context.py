"""告警上下文：把流量告警映射回本地会话文件，回答「当时在上传什么」。

流量监控只按内核 TCP 计数器统计字节数，看不到也存不下具体内容；但 agent
发给 API 的内容（用户消息、工具调用、工具输出）完整落在本地会话 JSONL 里。
本模块按告警的 ``cwd`` 和时间窗定位会话文件，提取窗口内的事件明细。
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
from hashlib import sha256
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .local_time import local_naive_to_timestamp
from .alerts import StoredAlert
from .alert_activity import describe_tool_activity, describe_user_text
from .local_agents import (
    opencode_db_path,
)
from .commandcode import (
    _commandcode_project_dir,
    _session_id_from_filename,
    read_commandcode_session_info,
)
from .discovery import JsonlSessionReader
from .grok import decode_grok_project

# 中文注释：内容事件（用户消息、工具输出）往往发生在字节告警触发前一两分钟
# （上传的是累积上下文），窗口向前多留 2 分钟；向后只留 30 秒落盘余量。
_WINDOW_LEAD_SECONDS = 120.0
_WINDOW_TRAIL_SECONDS = 30.0
# 中文注释：候选会话只需在窗口开始前 5 分钟内有过写入（mtime 预筛）。
_MTIME_SLACK_SECONDS = 300.0
_MAX_EVENTS = 60
# 中文注释：窗口内没有事件时（如 MCP 子进程直接外传），退而展示告警前最近的活动。
_FALLBACK_TAIL = 8
_EXCERPT_CHARS = 160
# 中文注释：关联工具输出时限制保留的调用数，避免长会话无限占用内存。
_MAX_TOOL_LABELS = 1024
# 中文注释：会话只追加。先从尾部退到时间窗之前再正向扫，避免整文件读进请求线程。
_TAIL_CHUNK = 256 * 1024
_LOOKBACK_RECORDS = 256
# 中文注释：单块 JSON 会话过大时不整份载入内存。
_BLOB_READ_LIMIT = 32 * 1024 * 1024
# 中文注释：同一文件签名和时间窗的提取结果短时复用，避免每条告警重读大会话。
_EXTRACT_CACHE_LIMIT = 64
_EXTRACT_CACHE: OrderedDict[tuple[object, ...], "_Extraction"] = OrderedDict()
_EXTRACT_CACHE_LOCK = threading.Lock()

SUPPORTED_PRODUCTS = (
    "codex",
    "claude",
    "kimi",
    "command-code",
    "grok",
    "dsh",
    "opencode",
    "cursor",
    "gemini",
    "qwen",
    "aider",
)


@dataclass(frozen=True)
class AlertContextRoots:
    """各产品的会话文件根目录。"""

    codex_sessions: tuple[Path, ...] = ()
    claude_projects: tuple[Path, ...] = ()
    kimi_sessions: tuple[Path, ...] = ()
    commandcode_projects: tuple[Path, ...] = ()
    grok_sessions: tuple[Path, ...] = ()
    dsh_sessions: tuple[Path, ...] = ()
    opencode_dbs: tuple[Path, ...] = ()
    cursor_projects: tuple[Path, ...] = ()
    gemini_homes: tuple[Path, ...] = ()
    qwen_homes: tuple[Path, ...] = ()
    aider_homes: tuple[Path, ...] = ()


@dataclass(frozen=True)
class _Candidate:
    """一个可能对上告警的会话文件；session_id 用于一个库里有多段会话的产品。"""

    path: Path
    mtime: float
    size: int
    session_id: str | None = None


@dataclass(frozen=True)
class _Extraction:
    """一次会话明细提取的结果；events 为 None 表示文件不可读。"""

    events: tuple[dict[str, Any], ...] | None
    truncated: bool
    input_bytes: int
    output_bytes: int
    fallback: bool = False
    event_count: int | None = None


@dataclass(frozen=True)
class _ToolCall:
    """保留调用的行为描述，使输出能通过 ID 关联用途而非猜测相邻事件。"""

    label: str
    activities: tuple[dict[str, str], ...]


def load_alert_context(
    alert: StoredAlert,
    roots: AlertContextRoots,
    *,
    include_content: bool = False,
) -> dict[str, Any]:
    """定位告警对应的会话并提取时间窗内的事件；返回可直接 JSON 化的字典。"""

    start = float(alert.first_seen_at) - _WINDOW_LEAD_SECONDS
    end = float(alert.last_seen_at) + _WINDOW_TRAIL_SECONDS
    payload: dict[str, Any] = {
        "content_enabled": include_content,
        "found": False,
        "reason": None,
        "product": alert.product,
        "window": {"start": start, "end": end},
        "candidates": 0,
        "session": None,
        "truncated": False,
        "fallback": False,
        "totals": {"events": 0, "input_bytes": 0, "output_bytes": 0},
        "events": [],
        "activity_summary": [],
    }
    if alert.product not in SUPPORTED_PRODUCTS:
        payload["reason"] = "unsupported_product"
        return payload
    candidates = _find_candidates(alert, roots, start, end)
    payload["candidates"] = len(candidates)
    if not candidates:
        payload["reason"] = "no_session"
        return payload
    # 中文注释：同目录可能并行多个会话，按最后写入时间离告警由近到远尝试：
    # 窗口内有真实事件的优先，其次是兜底（窗口前活动），最后才是空明细。
    best: tuple[_Candidate, _Extraction] | None = None
    fallback_choice: tuple[_Candidate, _Extraction] | None = None
    empty_choice: tuple[_Candidate, _Extraction] | None = None
    for candidate in sorted(
        candidates, key=lambda item: abs(item.mtime - alert.last_seen_at)
    ):
        result = _cached_extract(alert.product, candidate, start, end)
        if result.events is None:
            continue
        entry = (candidate, result)
        if result.events and not result.fallback:
            best = entry
            break
        if result.events and fallback_choice is None:
            fallback_choice = entry
        if not result.events and empty_choice is None:
            empty_choice = entry
    chosen = best or fallback_choice or empty_choice
    if chosen is None:
        payload["reason"] = "unreadable"
        return payload
    candidate, extraction = chosen
    path, mtime, size = candidate.path, candidate.mtime, candidate.size
    # 中文注释：行为与对象类别始终可见，文件名和内容仍由摘要开关控制。
    events = [_copy_event(item) for item in (extraction.events or ())]
    for item in events:
        item["detail"] = _excerpt(item.get("detail")) if include_content else ""
        item["label"] = _excerpt(item.get("label"), 240)
        for activity in item.get("activities", []):
            activity["target"] = (
                _excerpt(activity.get("target")) if include_content else ""
            )
    summaries = list(
        dict.fromkeys(
            activity["summary"]
            for item in events
            for activity in item.get("activities", [])
            if activity.get("phase") != "result"
        )
    )
    session_id = candidate.session_id or _session_id_for(alert.product, path)
    payload.update(
        found=True,
        reason=None,
        session={
            "path": str(path),
            "session_id": session_id,
            "size": size,
            "modified_at": mtime,
        },
        truncated=extraction.truncated,
        fallback=extraction.fallback,
        totals={
            "events": extraction.event_count
            if extraction.event_count is not None
            else len(events),
            "input_bytes": extraction.input_bytes,
            "output_bytes": extraction.output_bytes,
        },
        events=events,
        activity_summary=summaries,
    )
    return payload


def _copy_event(item: Mapping[str, Any]) -> dict[str, Any]:
    """复制一条事件，避免内容开关改写缓存里的摘要。"""

    copied = dict(item)
    activities = item.get("activities")
    if isinstance(activities, list):
        copied["activities"] = [
            dict(activity) if isinstance(activity, Mapping) else activity
            for activity in activities
        ]
    return copied


def _cached_extract(
    product: str,
    candidate: _Candidate,
    start: float,
    end: float,
) -> _Extraction:
    """按文件大小、修改时间和时间窗缓存提取结果，含不可读。"""

    key = (
        product,
        str(candidate.path),
        candidate.session_id or "",
        candidate.size,
        candidate.mtime,
        start,
        end,
    )
    with _EXTRACT_CACHE_LOCK:
        cached = _EXTRACT_CACHE.get(key)
        if cached is not None:
            _EXTRACT_CACHE.move_to_end(key)
            return cached
    result = _dispatch_extract(product, candidate, start, end)
    with _EXTRACT_CACHE_LOCK:
        _EXTRACT_CACHE[key] = result
        _EXTRACT_CACHE.move_to_end(key)
        while len(_EXTRACT_CACHE) > _EXTRACT_CACHE_LIMIT:
            _EXTRACT_CACHE.popitem(last=False)
    return result


# ---------------------------------------------------------------- 候选定位


def _session_id_for(product: str, path: Path) -> str:
    """从文件位置还原会话 ID。"""

    if product == "codex":
        return _session_id_from_name(path) or path.stem
    if product == "kimi":
        return path.name.removeprefix("session_")
    if product == "grok":
        # 中文注释：updates.jsonl 的上一级目录名才是会话 ID。
        return path.parent.name
    if product == "dsh":
        return path.parent.name.removeprefix("session-")
    if product == "aider":
        return path.parent.name or path.stem
    return path.stem


def _dispatch_extract(
    product: str, candidate: _Candidate, start: float, end: float
) -> _Extraction:
    """按产品提取；OpenCode 的会话 ID 不在文件名里，要单独传进去。"""

    if product == "opencode":
        return _extract_opencode(
            candidate.path, start, end, candidate.session_id or ""
        )
    extractor = _EXTRACTORS[product]
    return extractor(candidate.path, start, end)


def _find_candidates(
    alert: StoredAlert,
    roots: AlertContextRoots,
    start: float,
    end: float,
) -> list[_Candidate]:
    """按 mtime 预筛、cwd 匹配，返回候选会话。"""

    if alert.product == "codex":
        files = _codex_recent_files(roots.codex_sessions, start, end)
        matched = _match_codex_cwd(files, alert.cwd)
        return [
            _Candidate(path, stat.st_mtime, stat.st_size) for path, stat in matched
        ]
    if alert.product == "claude":
        files = _claude_recent_files(roots.claude_projects, alert.cwd, start)
        return [_Candidate(path, stat.st_mtime, stat.st_size) for path, stat in files]
    if alert.product == "kimi":
        return _kimi_candidates(roots.kimi_sessions, alert.cwd, start)
    if alert.product == "command-code":
        return _commandcode_candidates(roots.commandcode_projects, alert.cwd, start)
    if alert.product == "grok":
        return _grok_candidates(roots.grok_sessions, alert.cwd, start)
    if alert.product == "dsh":
        return _dsh_candidates(roots.dsh_sessions, alert.cwd, start)
    if alert.product == "opencode":
        return _opencode_candidates(roots.opencode_dbs, alert.cwd, start)
    if alert.product == "cursor":
        return _cursor_candidates(roots.cursor_projects, alert.cwd, start, end)
    if alert.product == "gemini":
        return _gemini_candidates(roots.gemini_homes, alert.cwd, start)
    if alert.product == "qwen":
        return _qwen_candidates(roots.qwen_homes, alert.cwd, start)
    if alert.product == "aider":
        return _aider_candidates(roots.aider_homes, alert.cwd, start)
    return []


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


def _cwd_matches(alert_cwd: str, session_cwd: str) -> bool:
    """进程 cwd 等于会话目录，或落在会话目录里面，才算同一现场。

    会话目录是告警目录的上级时算匹配（进程在项目子目录里）。反过来，
    一个很短的告警目录不能把下面每个项目都算进来。空的会话目录也不匹配。
    """

    if not session_cwd or not alert_cwd:
        return False
    if alert_cwd == session_cwd:
        return True
    prefix = session_cwd.rstrip("/")
    if not prefix:
        return False
    return alert_cwd.startswith(prefix + "/")


def _claude_slug(cwd: str) -> str:
    """Claude Code 把 cwd 里的非字母数字字符替换成 '-' 作为目录名。"""

    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _session_id_from_name(path: Path) -> str | None:
    match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        path.name,
    )
    return match.group(1) if match else None


# ---------------------------------------------------------------- 通用扫描


def _parse_ts(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _is_readable(path: Path) -> bool:
    try:
        with path.open("rb"):
            return True
    except OSError:
        return False


def _iter_records(
    path: Path,
    dates: tuple[bytes, ...] | None,
    offset: int = 0,
) -> Iterable[Mapping[str, Any]]:
    """从 ``offset`` 起逐行产出 JSON 记录（调用方先查可读性）。"""

    try:
        with path.open("rb") as handle:
            if offset > 0:
                handle.seek(offset)
            for raw in handle:
                if dates and not any(day in raw for day in dates):
                    continue
                try:
                    record = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(record, Mapping):
                    yield record
    except OSError:
        return


def _line_timestamp(raw: bytes, parse_ts: _TsFn) -> float | None:
    """解析一行的时间戳；坏行和没有时间的行返回 None。"""

    try:
        record = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    if not isinstance(record, Mapping):
        return None
    try:
        return parse_ts(record)
    except (TypeError, ValueError):
        return None


def _suffix_offset(path: Path, start: float | None, parse_ts: _TsFn) -> int:
    """返回正向扫描的起始字节。

    从文件尾往前找，直到越过时间窗并再留出一段记录，供工具调用和兜底使用。
    文件不超过一块、或整份都落在窗口里时返回 0，等价于整文件扫描。
    """

    if start is None:
        return 0
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size <= _TAIL_CHUNK:
        return 0
    try:
        handle = path.open("rb")
    except OSError:
        return 0
    with handle:
        position = size
        pending = b""
        old_count = 0
        crossed = False
        while position > 0:
            take = min(_TAIL_CHUNK, position)
            position -= take
            handle.seek(position)
            block = handle.read(take) + pending
            parts = block.split(b"\n")
            if position > 0:
                pending = parts[0]
                complete = parts[1:]
                base = position + len(parts[0]) + 1
            else:
                pending = b""
                complete = parts
                base = 0
            offsets: list[int] = []
            cursor = base
            for part in complete:
                offsets.append(cursor)
                cursor += len(part) + 1
            for offset, raw in zip(reversed(offsets), reversed(complete), strict=True):
                if not raw.strip():
                    continue
                timestamp = _line_timestamp(raw, parse_ts)
                if timestamp is not None and timestamp >= start:
                    # 中文注释：时钟回拨时重新计数，避免停在窗口中间。
                    crossed = False
                    old_count = 0
                    continue
                if timestamp is not None and timestamp < start:
                    crossed = True
                if not crossed:
                    continue
                old_count += 1
                if old_count >= _LOOKBACK_RECORDS:
                    return offset
    return 0


def _excerpt(value: object, limit: int = _EXCERPT_CHARS) -> str:
    """脱敏常见凭据后生成摘要；先脱敏再截断，避免暴露凭据前缀。"""

    text = str(value or "")
    text = re.sub(r"(?i)\bBearer\s+[\w.~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\bsk-[\w-]{8,}", "[REDACTED]", text)
    secret = r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|token|authorization)"
    text = re.sub(
        rf"(?i)(\b{secret}\b[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}}&]+)",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(rf"(?i)(--{secret}\s+)(\S+)", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _tool_detail(arguments: str) -> str:
    """从工具参数里挑出最能说明行为的字段（命令、路径、查询词）。"""

    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return _excerpt(arguments)
    if isinstance(parsed, Mapping):
        for key in (
            "cmd",
            "command",
            "file_path",
            "path",
            "filename",
            "target_file",
            "filePath",
            "query",
            "pattern",
        ):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return _excerpt(value)
        return _excerpt(json.dumps(parsed, ensure_ascii=False))
    return _excerpt(arguments)


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


def _remember_tool_call(
    calls: dict[str, _ToolCall] | None, call_id: object, call: _ToolCall
) -> None:
    """按调用 ID 记录用途和名称，用于关联后续输出。"""

    if calls is None or not isinstance(call_id, str) or not call_id:
        return
    calls[call_id] = call
    if len(calls) > _MAX_TOOL_LABELS:
        # 中文注释：只淘汰最旧的关联，不按相邻事件猜测输出属于哪个工具。
        calls.pop(next(iter(calls)))


def _tool_output_info(
    calls: dict[str, _ToolCall] | None, call_id: object
) -> dict[str, Any]:
    """输出只关联已记录的用途，不把执行结果当成上传成功的证明。"""

    call = calls.get(call_id) if calls and isinstance(call_id, str) else None
    return {
        "label": call.label if call else "",
        "activities": [dict(activity, phase="result") for activity in call.activities]
        if call
        else [
            {
                "summary": "返回工具结果",
                "basis": "record",
                "target": "",
                "phase": "result",
            }
        ],
    }


def _image_event(part: object, timestamp: float) -> tuple[dict[str, Any], int, int]:
    """提取图片输入线索；不把图片 URL、base64 或实际图像返回给页面。"""

    size = None
    if isinstance(part, Mapping):
        source = part.get("source")
        if isinstance(source, Mapping) and isinstance(source.get("data"), str):
            size = len(source["data"].encode("utf-8", errors="replace"))
        url = part.get("image_url")
        if isinstance(url, str) and url.startswith("data:"):
            size = len(url.encode("utf-8", errors="replace"))
    return (
        {
            "t": timestamp,
            "kind": "image",
            "label": "",
            "detail": "",
            "size": size,
            "activities": [
                {"summary": "向模型提供图片", "basis": "record", "target": ""}
            ],
        },
        size or 0,
        0,
    )


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


# 中文注释：map_record 把一条原始记录映射成 0..n 个
# （事件, 上行字节, 输出字节）；parse_ts 给出该格式的记录时间戳。
_MapFn = Callable[[Mapping[str, Any], float], list[tuple[dict[str, Any], int, int]]]
_TsFn = Callable[[Mapping[str, Any]], float | None]


def _scan_parsed(
    records: Iterable[Mapping[str, Any]],
    *,
    parse_ts: _TsFn,
    map_record: _MapFn,
    start: float | None,
    end: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, int, int, int]:
    """单遍扫描已解析记录：窗口内事件（带截断）+ 窗口前最近的若干条。"""

    in_window: list[dict[str, Any]] = []
    before: deque[dict[str, Any]] = deque(maxlen=_FALLBACK_TAIL)
    truncated = False
    input_bytes = 0
    output_bytes = 0
    event_count = 0
    for record in records:
        timestamp = parse_ts(record)
        if timestamp is None or timestamp > end:
            continue
        for item, delta_in, delta_out in map_record(record, timestamp):
            if start is None or timestamp < start:
                before.append(item)
                continue
            event_count += 1
            input_bytes += delta_in
            output_bytes += delta_out
            if len(in_window) >= _MAX_EVENTS:
                truncated = True
                continue
            in_window.append(item)
    in_window.sort(key=lambda item: item["t"])
    return in_window, list(before), truncated, input_bytes, output_bytes, event_count


def _scan_session(
    path: Path,
    *,
    dates: tuple[bytes, ...] | None,
    parse_ts: _TsFn,
    map_record: _MapFn,
    start: float | None,
    end: float,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, int, int, int]:
    """从尾部定位后的偏移正向扫描会话文件。"""

    return _scan_parsed(
        _iter_records(path, dates, offset),
        parse_ts=parse_ts,
        map_record=map_record,
        start=start,
        end=end,
    )


def _extraction_from_scan(
    scanned: tuple[list[dict[str, Any]], list[dict[str, Any]], bool, int, int, int],
) -> _Extraction:
    """窗口里有事件就用窗口；否则用扫描时已经留下的窗口前活动。"""

    in_window, before, truncated, input_bytes, output_bytes, event_count = scanned
    if in_window:
        return _Extraction(
            tuple(in_window),
            truncated,
            input_bytes,
            output_bytes,
            event_count=event_count,
        )
    if not before:
        return _Extraction((), False, 0, 0)
    fallback_in = sum(
        item["size"] or 0
        for item in before
        if item["kind"] in ("user", "image", "tool")
    )
    fallback_out = sum(
        item["size"] or 0 for item in before if item["kind"] == "tool_output"
    )
    return _Extraction(
        tuple(before), False, fallback_in, fallback_out, fallback=True
    )


def _finish(
    path: Path,
    *,
    parse_ts: _TsFn,
    map_record: _MapFn,
    start: float,
    end: float,
) -> _Extraction:
    """从覆盖时间窗的文件尾部做一次正向扫描。"""

    if not _is_readable(path):
        return _Extraction(None, False, 0, 0)
    return _extraction_from_scan(
        _scan_session(
            path,
            dates=None,
            parse_ts=parse_ts,
            map_record=map_record,
            start=start,
            end=end,
            offset=_suffix_offset(path, start, parse_ts),
        )
    )


# ---------------------------------------------------------------- Codex


def _iso_record_ts(record: Mapping[str, Any]) -> float | None:
    return _parse_ts(record.get("timestamp"))


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


# ---------------------------------------------------------------- Claude


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


# ---------------------------------------------------------------- Kimi


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


# ---------------------------------------------------------------- Command Code


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


# ---------------------------------------------------------------- Grok


# 中文注释：真实会话是 unix 秒（约 1e9）。超过该阈值才当成毫秒，避免把秒误除。
_GROK_MS_THRESHOLD = 10_000_000_000


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


# ---------------------------------------------------------------- 时间与文本


def _epoch_seconds(value: object) -> float | None:
    """秒或毫秒的数字时间戳；布尔值不是时间。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number > _GROK_MS_THRESHOLD:
        return number / 1000.0
    return number


def _as_text(value: object) -> str:
    """把字符串或 JSON 值收成一段文本；空对象不当成输出。"""

    if isinstance(value, str):
        return value
    if value is None:
        return ""
    text = json.dumps(value, ensure_ascii=False, default=str)
    if text in {"{}", "[]", "null", '""'}:
        return ""
    return text


def _content_text(content: object) -> str:
    """取出用户或工具正文里的文字，跳过思考和图片块。"""

    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, Mapping):
            continue
        if part.get("type") in {"image", "image_url", "input_image"}:
            continue
        if part.get("thought") is True or part.get("type") == "thought":
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(part for part in parts if part)


def _user_item(text: str, timestamp: float) -> tuple[dict[str, Any], int, int] | None:
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


def _tool_item(
    name: str,
    arguments: object,
    timestamp: float,
    call_id: object,
    tool_calls: dict[str, _ToolCall] | None,
) -> tuple[dict[str, Any], int, int]:
    activities = describe_tool_activity(name, arguments if arguments is not None else {})
    label = name or ""
    _remember_tool_call(tool_calls, call_id, _ToolCall(label, tuple(activities)))
    raw = arguments if isinstance(arguments, str) else _as_text(arguments)
    size = len(raw.encode("utf-8", errors="replace"))
    return (
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


def _output_item(
    text: str,
    timestamp: float,
    call_id: object,
    tool_calls: dict[str, _ToolCall] | None,
) -> tuple[dict[str, Any], int, int] | None:
    if not text:
        return None
    size = len(text.encode("utf-8", errors="replace"))
    return (
        {
            "t": timestamp,
            "kind": "tool_output",
            **_tool_output_info(tool_calls, call_id),
            "detail": "",
            "size": size,
        },
        0,
        size,
    )


# ---------------------------------------------------------------- DeepSeek Harness


class _DecodeError(OSError):
    """会话压缩帧无法解开。"""


_UNREADABLE = object()
_DSH_TRANSCRIPTS = (
    "session.v4.jsonl.zstd",
    "session.v3.jsonl.zstd",
    "session.v2.jsonl.zstd",
    "session.v4.jsonl",
    "session.v3.jsonl",
    "session.v2.jsonl",
    "session.jsonl",
)


def _dsh_transcript(session_dir: Path) -> Path | None:
    """同一会话目录里取版本最高的那份记录。"""

    for name in _DSH_TRANSCRIPTS:
        path = session_dir / name
        if path.is_file():
            return path
    return None


def _zstd_lines(path: Path) -> Iterable[str]:
    """解开 zstd JSONL。标准库、可选第三方库、本机 zstd 命令依次尝试。"""

    try:
        zstd_mod = importlib.import_module("compression.zstd")
    except ImportError:
        zstd_mod = None
    if zstd_mod is not None:
        try:
            with path.open("rb") as raw, zstd_mod.ZstdFile(raw) as decoded:
                for line in decoded:
                    yield line.decode("utf-8", errors="replace")
            return
        except (OSError, AttributeError) as error:
            raise _DecodeError(str(error)) from error
    for module_name in ("zstandard", "backports.zstd"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        try:
            with path.open("rb") as raw:
                if module_name == "zstandard":
                    reader = module.ZstdDecompressor().stream_reader(raw)
                else:
                    reader = module.open(raw, "rb")
                try:
                    for line in reader:
                        yield line.decode("utf-8", errors="replace")
                finally:
                    reader.close()
            return
        except (OSError, AttributeError) as error:
            raise _DecodeError(str(error)) from error
    if shutil.which("zstd") is None:
        raise _DecodeError("没有可用的 zstd 解码器")
    process = subprocess.Popen(
        ["zstd", "-dc", "--", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    stdout = process.stdout
    if stdout is None:
        process.kill()
        process.wait()
        raise _DecodeError("zstd 没有输出")
    try:
        for line in stdout:
            yield line.decode("utf-8", errors="replace")
    finally:
        stdout.close()
        process.kill()
        process.wait()


def _dsh_lines(path: Path) -> Iterable[str]:
    """逐行读出会话。压缩帧不能按偏移截断，读完或关闭生成器即停止。"""

    if path.name.endswith(".zstd"):
        yield from _zstd_lines(path)
        return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            yield from handle
    except OSError as error:
        raise _DecodeError(str(error)) from error


def _dsh_records(path: Path) -> Iterable[Mapping[str, Any]]:
    for line in _dsh_lines(path):
        text = line.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(record, Mapping):
            yield record


def _dsh_first_record(path: Path) -> Mapping[str, Any] | None | object:
    """只读第一条记录拿 cwd；解不开时返回哨兵，调用方仍保留这个候选。"""

    lines = _dsh_lines(path)
    try:
        for record in _mapping_lines(lines):
            return record
        return None
    except _DecodeError:
        return _UNREADABLE
    finally:
        close = getattr(lines, "close", None)
        if close is not None:
            close()


def _mapping_lines(lines: Iterable[str]) -> Iterable[Mapping[str, Any]]:
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(record, Mapping):
            yield record


def _dsh_cwd(record: Mapping[str, Any]) -> str | None:
    data = record.get("data")
    if isinstance(data, Mapping):
        cwd = data.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    cwd = record.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def _dsh_record_ts(record: Mapping[str, Any]) -> float | None:
    return _epoch_seconds(record.get("time"))


def _dsh_candidates(
    roots: Sequence[Path],
    cwd: str | None,
    start: float,
) -> list[_Candidate]:
    """会话目录名不可逆，cwd 在第一条 session 记录上。"""

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
            try:
                sessions = [entry for entry in project.iterdir() if entry.is_dir()]
            except OSError:
                continue
            for session_dir in sessions:
                path = _dsh_transcript(session_dir)
                if path is None:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime < cutoff:
                    continue
                header = _dsh_first_record(path)
                if header is _UNREADABLE:
                    out.append(_Candidate(path, stat.st_mtime, stat.st_size))
                    continue
                session_cwd = _dsh_cwd(header) if isinstance(header, Mapping) else None
                if cwd and not (session_cwd and _cwd_matches(cwd, session_cwd)):
                    continue
                out.append(_Candidate(path, stat.st_mtime, stat.st_size))
    return out


def _dsh_map(
    record: Mapping[str, Any],
    timestamp: float,
    *,
    tool_calls: dict[str, _ToolCall] | None = None,
) -> list[tuple[dict[str, Any], int, int]]:
    kind = record.get("type")
    data = record.get("data")
    if not isinstance(data, Mapping):
        return []
    if kind == "user/message":
        source = data.get("source")
        if not isinstance(source, Mapping) or source.get("kind") != "user":
            return []
        item = _user_item(_content_text(data.get("content")), timestamp)
        return [item] if item is not None else []
    if kind == "tool/call":
        return [
            _tool_item(
                str(data.get("name") or ""),
                data.get("arguments"),
                timestamp,
                data.get("callId"),
                tool_calls,
            )
        ]
    if kind != "tool/result":
        return []
    call_id = data.get("callId")
    message = data.get("message")
    content: object = data.get("content")
    if isinstance(message, Mapping):
        source = message.get("source")
        if isinstance(source, Mapping) and source.get("callId"):
            call_id = source.get("callId")
        content = message.get("content")
    item = _output_item(_content_text(content) or _as_text(content), timestamp, call_id, tool_calls)
    return [item] if item is not None else []


def _extract_dsh(path: Path, start: float, end: float) -> _Extraction:
    tool_calls: dict[str, _ToolCall] = {}
    try:
        return _extraction_from_scan(
            _scan_parsed(
                _dsh_records(path),
                parse_ts=_dsh_record_ts,
                map_record=partial(_dsh_map, tool_calls=tool_calls),
                start=start,
                end=end,
            )
        )
    except _DecodeError:
        return _Extraction(None, False, 0, 0)


# ---------------------------------------------------------------- OpenCode


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


def _bucket_items(
    items: list[tuple[dict[str, Any], int, int]],
    start: float,
    end: float,
) -> _Extraction:
    """按事件时间分成窗口内和窗口前，规则与文件扫描一致。"""

    in_window: list[dict[str, Any]] = []
    before: deque[dict[str, Any]] = deque(maxlen=_FALLBACK_TAIL)
    truncated = False
    input_bytes = 0
    output_bytes = 0
    event_count = 0
    for item, delta_in, delta_out in items:
        timestamp = float(item["t"])
        if timestamp > end:
            continue
        if timestamp < start:
            before.append(item)
            continue
        event_count += 1
        input_bytes += delta_in
        output_bytes += delta_out
        if len(in_window) >= _MAX_EVENTS:
            truncated = True
            continue
        in_window.append(item)
    in_window.sort(key=lambda event: event["t"])
    return _extraction_from_scan(
        (in_window, list(before), truncated, input_bytes, output_bytes, event_count)
    )


# ---------------------------------------------------------------- Cursor


def _path_slugs(cwd: str) -> set[str]:
    """目录名的几种扁平写法：非字母数字换成 '-'，或只替换斜杠。"""

    dashed = _claude_slug(cwd)
    slash = cwd.replace("/", "-").replace("\\", "-")
    return {
        item
        for item in (dashed, dashed.lstrip("-"), slash, slash.lstrip("-"))
        if item
    }


def _tail_text_lines(path: Path, limit: int) -> tuple[list[str], bool] | None:
    """从文件尾部取出最多 limit 行；多于这个数时标明被截断。"""

    try:
        handle = path.open("rb")
    except OSError:
        return None
    collected: list[str] = []
    with handle:
        try:
            position = handle.seek(0, 2)
        except OSError:
            return None
        pending = b""
        while position > 0 and len(collected) <= limit:
            take = min(_TAIL_CHUNK, position)
            position -= take
            handle.seek(position)
            block = handle.read(take) + pending
            parts = block.split(b"\n")
            if position > 0:
                pending = parts[0]
                complete = parts[1:]
            else:
                pending = b""
                complete = parts
            for raw in reversed(complete):
                if not raw.strip():
                    continue
                collected.append(raw.decode("utf-8", errors="replace"))
                if len(collected) > limit:
                    break
        if pending.strip() and len(collected) <= limit:
            collected.append(pending.decode("utf-8", errors="replace"))
    truncated = len(collected) > limit
    return list(reversed(collected[:limit])), truncated


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


# ---------------------------------------------------------------- Gemini / Qwen


def _project_paths(home: Path) -> dict[str, str]:
    """projects.json 把项目 id 映到工作目录。几种历史写法都认。"""

    path = home / "projects.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    found: dict[str, str] = {}

    def take(ident: object, value: object) -> None:
        if isinstance(value, str) and isinstance(ident, str) and value:
            found[ident] = value
            return
        if isinstance(value, Mapping) and isinstance(ident, str):
            cwd = value.get("path") or value.get("cwd")
            if isinstance(cwd, str) and cwd:
                found[ident] = cwd

    if isinstance(payload, Mapping):
        nested = payload.get("projects")
        source = nested if isinstance(nested, Mapping) else payload
        for key, value in source.items():
            take(key, value)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, Mapping):
                take(item.get("id") or item.get("hash"), item.get("path") or item.get("cwd"))
    return found


def _ids_for_cwd(home: Path, cwd: str) -> set[str] | None:
    """有项目表时只返回对得上的 id；没有表时返回 None，表示还得看文件头。"""

    mapping = _project_paths(home)
    if not mapping:
        return None
    matched = {
        ident for ident, path in mapping.items() if _cwd_matches(cwd, path)
    }
    # 中文注释：表里没有这个目录时，改看会话头，避免过期的项目表把会话藏掉。
    return matched or None


def _header_cwd(path: Path) -> str | None:
    """会话头里的 cwd。JSONL 只看开头几行，不把整份历史读进来。"""

    try:
        if path.suffix == ".json":
            if path.stat().st_size > _BLOB_READ_LIMIT:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, Mapping):
                for key in ("cwd", "projectPath", "directory"):
                    value = payload.get(key)
                    if isinstance(value, str) and value:
                        return value
            return None
        with path.open("rb") as handle:
            raw = handle.read(65536)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    for line in raw.split(b"\n")[:8]:
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if not isinstance(record, Mapping):
            continue
        for key in ("cwd", "projectPath", "directory"):
            value = record.get(key)
            if isinstance(value, str) and value:
                return value
    return None


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


# ---------------------------------------------------------------- Aider


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


# ---------------------------------------------------------------- 本机默认目录


def _existing_file(path: Path) -> tuple[Path, ...]:
    return (path,) if path.is_file() else ()


def _existing_dir(path: Path) -> tuple[Path, ...]:
    return (path,) if path.is_dir() else ()


def _default_opencode_dbs() -> tuple[Path, ...]:
    configured = os.environ.get("OPENCODE_DB")
    if configured:
        return _existing_file(Path(configured).expanduser())
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return _existing_file(base / "opencode" / "opencode.db")


def _default_cursor_projects() -> tuple[Path, ...]:
    configured = os.environ.get("CURSOR_CONFIG_DIR")
    if configured:
        home = Path(configured).expanduser()
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        home = Path(xdg).expanduser() / "cursor" if xdg else Path.home() / ".cursor"
    return _existing_dir(home / "projects")


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


def _default_aider_homes() -> tuple[Path, ...]:
    configured = os.environ.get("AIDER_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".aider"
    return _existing_dir(home)


def _opencode_dbs_from_homes(homes: Sequence[Path]) -> tuple[Path, ...]:
    """显式目录只映射到其中的数据库文件，空元组不退回本机默认库。"""

    found: list[Path] = []
    for home in homes:
        database = opencode_db_path(Path(home))
        if database.is_file():
            found.append(database)
    return tuple(found)


def _cursor_projects_from_homes(homes: Sequence[Path]) -> tuple[Path, ...]:
    """Cursor 告警根是配置目录下的 projects，不是配置目录本身。"""

    found: list[Path] = []
    for home in homes:
        projects = Path(home) / "projects"
        if projects.is_dir():
            found.append(projects)
    return tuple(found)


def _existing_homes(homes: Sequence[Path]) -> tuple[Path, ...]:
    return tuple(path for path in (Path(home) for home in homes) if path.is_dir())


def configured_alert_context_roots(
    *,
    codex_sessions: Sequence[Path] = (),
    homes: Mapping[str, Sequence[Path] | None] | None = None,
) -> AlertContextRoots:
    """按 provider 目录映射生成告警详情的会话根目录。

    已配置的产品只用传入的目录。映射里没有（或为 None）的产品：Claude Code、
    Kimi、Command Code、Grok、DSH 不扫描；OpenCode、Cursor、Gemini、Qwen、Aider
    看本机默认位置。
    """

    given = homes or {}
    claude_homes = given.get("claude") or ()
    kimi_homes = given.get("kimi") or ()
    commandcode_homes = given.get("commandcode") or ()
    grok_homes = given.get("grok") or ()
    dsh_homes = given.get("dsh") or ()
    opencode_homes = given.get("opencode")
    cursor_homes = given.get("cursor")
    gemini_homes = given.get("gemini")
    qwen_homes = given.get("qwen")
    aider_homes = given.get("aider")
    return AlertContextRoots(
        codex_sessions=tuple(codex_sessions),
        claude_projects=tuple(Path(home) / "projects" for home in claude_homes),
        kimi_sessions=tuple(Path(home) / "sessions" for home in kimi_homes),
        commandcode_projects=tuple(
            Path(home) / "projects" for home in commandcode_homes
        ),
        grok_sessions=tuple(Path(home) / "sessions" for home in grok_homes),
        dsh_sessions=tuple(Path(home) / "sessions" for home in dsh_homes),
        opencode_dbs=(
            _default_opencode_dbs()
            if opencode_homes is None
            else _opencode_dbs_from_homes(opencode_homes)
        ),
        cursor_projects=(
            _default_cursor_projects()
            if cursor_homes is None
            else _cursor_projects_from_homes(cursor_homes)
        ),
        gemini_homes=(
            _default_gemini_homes()
            if gemini_homes is None
            else _existing_homes(gemini_homes)
        ),
        qwen_homes=(
            _default_qwen_homes()
            if qwen_homes is None
            else _existing_homes(qwen_homes)
        ),
        aider_homes=(
            _default_aider_homes()
            if aider_homes is None
            else _existing_homes(aider_homes)
        ),
    )


def default_alert_context_roots() -> AlertContextRoots:
    """命令行使用各产品的默认数据目录；目录不存在就不扫。"""

    from .discovery import default_session_root
    from .providers import resolve_provider_homes

    codex = default_session_root()
    return configured_alert_context_roots(
        codex_sessions=(codex,) if codex.is_dir() else (),
        homes=resolve_provider_homes(None, auto_detect=True),
    )


# 中文注释：按产品分派的会话文件解析器；OpenCode 的会话在 SQLite 里，单独处理。
_EXTRACTORS: dict[str, Callable[[Path, float, float], _Extraction]] = {
    "codex": _extract_codex,
    "claude": _extract_claude,
    "kimi": _extract_kimi,
    "command-code": _extract_commandcode,
    "grok": _extract_grok,
    "dsh": _extract_dsh,
    "cursor": _extract_cursor,
    "gemini": _extract_gemini,
    "qwen": _extract_qwen,
    "aider": _extract_aider,
}
