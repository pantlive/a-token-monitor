"""从本地 Codex session JSONL 提取最近额度快照的兜底实现。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from heapq import nlargest
from pathlib import Path
from typing import Any, Iterable, Mapping

from .discovery import JsonlSessionReader
from .quota import QuotaSnapshot, QuotaWindow


_MAX_FALLBACK_READ_BYTES = 256 * 1024


@dataclass(frozen=True)
class _CachedQuotaTail:
    """一个 JSONL 尾部额度结果及其文件签名。"""

    signature: tuple[int, int, int]
    windows: tuple[tuple[float, QuotaWindow], ...]
    plan: tuple[float, str] | None


class JsonlQuotaFallbackReader:
    """按文件签名缓存额度尾部，避免每次失败重复读取冷数据。"""

    def __init__(self, reader: JsonlSessionReader | None = None) -> None:
        """创建持久复用的额度回退读取器。"""

        self.reader = reader or JsonlSessionReader()
        self._cache: dict[Path, _CachedQuotaTail] = {}

    def read(
        self,
        paths: Iterable[Path],
        now: float | None = None,
    ) -> QuotaSnapshot | None:
        """只重读发生变化的 JSONL，并合并最新额度窗口。"""

        current_time = now if now is not None else time.time()
        selected_paths = tuple(dict.fromkeys(Path(path) for path in paths))
        selected_set = set(selected_paths)
        latest: dict[tuple[str, str], tuple[float, QuotaWindow]] = {}
        latest_plan: tuple[float, str] | None = None
        for path in selected_paths:
            signature = _file_signature(path)
            if signature is None:
                continue
            cached = self._cache.get(path)
            if cached is None or cached.signature != signature:
                cached = self._read_path(path, signature, current_time)
                self._cache[path] = cached
            for timestamp, window in cached.windows:
                key = (window.limit_id, window.name)
                previous = latest.get(key)
                if previous is None or timestamp >= previous[0]:
                    latest[key] = (timestamp, window)
            if cached.plan is not None and (
                latest_plan is None or cached.plan[0] >= latest_plan[0]
            ):
                latest_plan = cached.plan

        # 中文注释：缓存只保留本轮候选，避免长期运行时路径集合无限增长。
        self._cache = {
            path: cached for path, cached in self._cache.items() if path in selected_set
        }
        if not latest:
            return None
        observed_at = max(timestamp for timestamp, _ in latest.values())
        return QuotaSnapshot(
            observed_at=observed_at,
            windows=tuple(
                window
                for _, window in sorted(
                    latest.values(),
                    key=lambda item: item[1].name,
                )
            ),
            plan_type=latest_plan[1] if latest_plan is not None else None,
            source="session-jsonl-fallback",
            raw_limit_ids=("codex",),
            metadata={"freshness": "latest local token_count event"},
        )

    def _read_path(
        self,
        path: Path,
        signature: tuple[int, int, int],
        now: float,
    ) -> _CachedQuotaTail:
        """读取单个文件最多 256 KiB 的尾部并保留安全字段。"""

        initial_offset = self.reader.initial_offset(path)
        tail = self.reader.read(
            path,
            offset=initial_offset,
            now=now,
            maximum_bytes=_MAX_FALLBACK_READ_BYTES,
        )
        latest: dict[tuple[str, str], tuple[float, QuotaWindow]] = {}
        latest_plan: tuple[float, str] | None = None
        for event in tail.events:
            if not event.observation.rate_limits:
                continue
            event_time = event.timestamp or now
            event_plan = _extract_plan_type(event.observation.raw_line)
            if event_plan is not None and (
                latest_plan is None or event_time >= latest_plan[0]
            ):
                latest_plan = (event_time, event_plan)
            for window in event.observation.rate_limits:
                parsed = QuotaWindow(
                    limit_id="codex",
                    name=window.name,
                    used_percent=window.used_percent,
                    window_minutes=window.window_minutes,
                    resets_at=window.reset_at,
                )
                key = (parsed.limit_id, parsed.name)
                previous = latest.get(key)
                if previous is None or event_time >= previous[0]:
                    latest[key] = (event_time, parsed)
        return _CachedQuotaTail(
            signature=signature,
            windows=tuple(latest.values()),
            plan=latest_plan,
        )


def read_jsonl_quota(
    paths: Iterable[Path],
    reader: JsonlSessionReader | None = None,
    now: float | None = None,
) -> QuotaSnapshot | None:
    """读取最近 JSONL 额度事件，返回明确标注为本地兜底来源的快照。"""

    return JsonlQuotaFallbackReader(reader=reader).read(paths, now=now)


def recent_session_paths(
    session_root: Path,
    active_paths: Iterable[Path] = (),
    known_paths: Iterable[Path] = (),
    maximum_files: int = 20,
) -> tuple[Path, ...]:
    """优先返回活动和注册表路径，仅在没有已知路径时遍历历史。"""

    if maximum_files <= 0:
        raise ValueError("maximum_files 必须大于 0")
    candidates: dict[str, Path] = {}
    for path in active_paths:
        normalized = Path(path)
        if normalized.is_file():
            candidates[str(normalized)] = normalized
    known_count = 0
    for path in known_paths:
        if known_count >= maximum_files:
            break
        normalized = Path(path)
        if not normalized.is_file() or str(normalized) in candidates:
            continue
        candidates[str(normalized)] = normalized
        known_count += 1
    if candidates:
        return tuple(candidates.values())

    # 中文注释：仅用于首次运行且注册表为空的场景；后续周期查询不再递归扫描。
    try:
        recent = nlargest(
            maximum_files,
            (path for path in session_root.rglob("*.jsonl") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
        )
    except OSError:
        recent = []
    for path in recent:
        candidates[str(path)] = path
    return tuple(candidates.values())


def _file_signature(path: Path) -> tuple[int, int, int] | None:
    """返回可识别追加和替换的 inode、修改时间及大小。"""

    try:
        stat_result = path.stat()
    except OSError:
        return None
    return stat_result.st_ino, stat_result.st_mtime_ns, stat_result.st_size


def _extract_plan_type(line: str) -> str | None:
    """从一条 JSONL 额度事件中提取套餐名。"""

    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return _find_string(payload, {"plan_type", "plantype"})


def _find_string(value: Any, names: set[str]) -> str | None:
    """在有限 JSON 对象中递归查找指定字符串字段。"""

    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            normalized = str(raw_key).replace("_", "").lower()
            if normalized in names and isinstance(child, str) and child:
                return child
            found = _find_string(child, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_string(child, names)
            if found is not None:
                return found
    return None
