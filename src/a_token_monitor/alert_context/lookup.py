"""告警上下文入口：按产品定位候选会话、提取窗口内事件并缓存结果。"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..alerts import StoredAlert
from .aider import (
    _aider_candidates,
    _default_aider_homes,
    _extract_aider,
)
from .chat import (
    _default_gemini_homes,
    _default_qwen_homes,
    _extract_gemini,
    _extract_qwen,
    _gemini_candidates,
    _qwen_candidates,
)
from .claude import (
    _claude_recent_files,
    _extract_claude,
)
from .codex import (
    _codex_recent_files,
    _extract_codex,
    _match_codex_cwd,
)
from .commandcode import (
    _commandcode_candidates,
    _extract_commandcode,
)
from .common import (
    AlertContextRoots,
    _Candidate,
    _Extraction,
    _WINDOW_LEAD_SECONDS,
    _WINDOW_TRAIL_SECONDS,
    _copy_event,
    _excerpt,
    _existing_homes,
    _session_id_from_name,
)
from .cursor import (
    _cursor_candidates,
    _cursor_projects_from_homes,
    _default_cursor_projects,
    _extract_cursor,
)
from .dsh import (
    _dsh_candidates,
    _extract_dsh,
)
from .grok import (
    _extract_grok,
    _grok_candidates,
)
from .kimi import (
    _extract_kimi,
    _kimi_candidates,
)
from .opencode import (
    _default_opencode_dbs,
    _extract_opencode,
    _opencode_candidates,
    _opencode_dbs_from_homes,
)


# 中文注释：同一文件签名和时间窗的提取结果短时复用，避免每条告警重读大会话。
_EXTRACT_CACHE_LIMIT = 64


_EXTRACT_CACHE: OrderedDict[tuple[object, ...], "_Extraction"] = OrderedDict()


_EXTRACT_CACHE_LOCK = threading.Lock()


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


def _session_id_for(product: str, path: Path) -> str:
    """从文件位置还原会话 ID。"""

    return _SOURCES[product].session_id(path)


def _dispatch_extract(
    product: str, candidate: _Candidate, start: float, end: float
) -> _Extraction:
    """按产品提取窗口内的事件。"""

    return _SOURCES[product].extract(candidate, start, end)


def _find_candidates(
    alert: StoredAlert,
    roots: AlertContextRoots,
    start: float,
    end: float,
) -> list[_Candidate]:
    """按 mtime 预筛、cwd 匹配，返回候选会话。"""

    source = _SOURCES.get(alert.product)
    if source is None:
        return []
    return source.candidates(roots, alert.cwd, start, end)


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



@dataclass(frozen=True)
class _AlertSource:
    """一个产品接入告警上下文的三件事：定位候选会话、提取事件、还原会话 ID。"""

    candidates: Callable[
        [AlertContextRoots, str | None, float, float], list[_Candidate]
    ]
    extract: Callable[[_Candidate, float, float], _Extraction]
    session_id: Callable[[Path], str] = lambda path: path.stem


def _stat_candidates(files: Sequence[tuple[Path, Any]]) -> list[_Candidate]:
    """把 (路径, stat) 列表转成候选会话。"""

    return [_Candidate(path, stat.st_mtime, stat.st_size) for path, stat in files]


def _by_path(
    extractor: Callable[[Path, float, float], _Extraction],
) -> Callable[[_Candidate, float, float], _Extraction]:
    """按候选文件路径提取的产品（除 OpenCode 外都是）。"""

    return lambda candidate, start, end: extractor(candidate.path, start, end)


# 中文注释：按产品分派的唯一入口；SUPPORTED_PRODUCTS 由它生成，新增 agent 只在这里登记。
_SOURCES: dict[str, _AlertSource] = {
    "codex": _AlertSource(
        candidates=lambda roots, cwd, start, end: _stat_candidates(
            _match_codex_cwd(
                _codex_recent_files(roots.codex_sessions, start, end), cwd
            )
        ),
        extract=_by_path(_extract_codex),
        session_id=lambda path: _session_id_from_name(path) or path.stem,
    ),
    "claude": _AlertSource(
        candidates=lambda roots, cwd, start, end: _stat_candidates(
            _claude_recent_files(roots.claude_projects, cwd, start)
        ),
        extract=_by_path(_extract_claude),
    ),
    "kimi": _AlertSource(
        candidates=lambda roots, cwd, start, end: _kimi_candidates(
            roots.kimi_sessions, cwd, start
        ),
        extract=_by_path(_extract_kimi),
        session_id=lambda path: path.name.removeprefix("session_"),
    ),
    "command-code": _AlertSource(
        candidates=lambda roots, cwd, start, end: _commandcode_candidates(
            roots.commandcode_projects, cwd, start
        ),
        extract=_by_path(_extract_commandcode),
    ),
    "grok": _AlertSource(
        candidates=lambda roots, cwd, start, end: _grok_candidates(
            roots.grok_sessions, cwd, start
        ),
        extract=_by_path(_extract_grok),
        # 中文注释：updates.jsonl 的上一级目录名才是会话 ID。
        session_id=lambda path: path.parent.name,
    ),
    "dsh": _AlertSource(
        candidates=lambda roots, cwd, start, end: _dsh_candidates(
            roots.dsh_sessions, cwd, start
        ),
        extract=_by_path(_extract_dsh),
        session_id=lambda path: path.parent.name.removeprefix("session-"),
    ),
    "opencode": _AlertSource(
        candidates=lambda roots, cwd, start, end: _opencode_candidates(
            roots.opencode_dbs, cwd, start
        ),
        # 中文注释：OpenCode 的会话在 SQLite 里，会话 ID 不在文件名中，由候选带过来。
        extract=lambda candidate, start, end: _extract_opencode(
            candidate.path, start, end, candidate.session_id or ""
        ),
    ),
    "cursor": _AlertSource(
        candidates=lambda roots, cwd, start, end: _cursor_candidates(
            roots.cursor_projects, cwd, start, end
        ),
        extract=_by_path(_extract_cursor),
    ),
    "gemini": _AlertSource(
        candidates=lambda roots, cwd, start, end: _gemini_candidates(
            roots.gemini_homes, cwd, start
        ),
        extract=_by_path(_extract_gemini),
    ),
    "qwen": _AlertSource(
        candidates=lambda roots, cwd, start, end: _qwen_candidates(
            roots.qwen_homes, cwd, start
        ),
        extract=_by_path(_extract_qwen),
    ),
    "aider": _AlertSource(
        candidates=lambda roots, cwd, start, end: _aider_candidates(
            roots.aider_homes, cwd, start
        ),
        extract=_by_path(_extract_aider),
        session_id=lambda path: path.parent.name or path.stem,
    ),
}

SUPPORTED_PRODUCTS = tuple(_SOURCES)
