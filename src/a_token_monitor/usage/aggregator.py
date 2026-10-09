"""``UsageAggregator``：发现各家会话文件、有界增量读取并输出用量快照。"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..grok import (
    GrokSessionInfo,
)
from ..health import sanitize_error
from ..kimi import (
    KimiSessionInfo,
)
from ..registry import MultiSessionRegistry
from ..local_time import local_day_key, local_day_start, to_local
from ..providers import (
    ProviderHomesInput,
    resolve_provider_homes,
    update_provider_homes,
)
from .aggregates import (
    _ConversationMetrics,
    _FileRollup,
    _UsageAggregate,
    _aggregate_to_dict,
)
from .insights import (
    _build_insights,
    _conversation_metrics,
)
from .parsing import (
    _file_signature,
)
from .periods import (
    _period_start,
)
from .pricing import (
    pricing_metadata,
)
from .records import (
    UsageDelta,
    _CachedFile,
    _UsageSource,
    _round_number,
    _text_value,
)
from .store import (
    _UsageIndexStore,
)
from .querying import _UsageQueryMixin
from .readers import _FileReaderMixin
from .sources import _SourceDiscoveryMixin


class UsageAggregator(_UsageQueryMixin, _SourceDiscoveryMixin, _FileReaderMixin):
    """扫描多个 CODEX_HOME 并按账号、时间窗口汇总 JSONL 用量。"""
    # 中文注释：单轮和单文件都最多读取 1 MiB，避免冷的 ext4.vhdx 突发读满。
    _DEFAULT_READ_BUDGET = 1 * 1024 * 1024
    _PER_FILE_READ_BUDGET = 1 * 1024 * 1024
    _PERIODS = (
        ("today", "今天", "calendar_day"),
        ("seven_days", "近 7 天", "seven_days"),
        ("month", "近 30 天", "thirty_days"),
        ("year", "近 365 天", "three_hundred_sixty_five_days"),
    )

    _DAILY_TREND_DAYS = 30

    # 中文注释：后台索引限速，避免冷 vhdx 被一次读满，同时不再靠手工连点。
    _DEFAULT_INDEX_BYTES_PER_SEC = 1 * 1024 * 1024

    def __init__(
        self,
        discovery_interval: float = 300.0,
        refresh_interval: float = 300.0,
        read_budget_bytes: int = _DEFAULT_READ_BUDGET,
        cache_path: Path | None = None,
        background_indexing: bool = False,
        index_bytes_per_sec: int = _DEFAULT_INDEX_BYTES_PER_SEC,
        homes: ProviderHomesInput | None = None,
    ) -> None:
        """创建有刷新间隔、持久化检查点和单轮磁盘预算的用量缓存。"""

        if discovery_interval <= 0:
            raise ValueError("discovery_interval 必须大于 0")
        if refresh_interval <= 0:
            raise ValueError("refresh_interval 必须大于 0")
        if read_budget_bytes <= 0:
            raise ValueError("read_budget_bytes 必须大于 0")
        if index_bytes_per_sec <= 0:
            raise ValueError("index_bytes_per_sec 必须大于 0")
        self.discovery_interval = discovery_interval
        self.refresh_interval = refresh_interval
        self.read_budget_bytes = read_budget_bytes
        self.background_indexing = background_indexing
        self.index_bytes_per_sec = index_bytes_per_sec
        # 中文注释：聚合器里未给出的 provider 一律不扫描，避免单测把本机数据读进来；
        # 监控进程要自动探测时先 resolve，再把结果映射传进来。
        self._homes = resolve_provider_homes(homes, auto_detect=False)
        self._grok_sessions: dict[Path, dict[str, GrokSessionInfo]] = {}
        self._grok_sessions_at: dict[Path, float] = {}
        self._kimi_sessions: dict[Path, dict[str, KimiSessionInfo]] = {}
        self._kimi_sessions_at: dict[Path, float] = {}
        self._claude_sidechain_cache: dict[Path, tuple[float, bool]] = {}
        self._chat_partial: dict[Path, tuple[Any, ...]] = {}
        self._cache: dict[Path, _CachedFile] = {}
        self._rollups: dict[Path, _FileRollup] = {}
        self._discovered: dict[Path, tuple[Path, ...]] = {}
        self._discovered_at: dict[Path, float] = {}
        self._snapshot_cache: dict[str, Any] | None = None
        self._snapshot_cached_at = 0.0
        self._snapshot_scope: tuple[tuple[str, str, str, str], ...] = ()
        self._facets_cache: dict[str, Any] | None = None
        self._facets_cached_at = 0.0
        self._search_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
        self._index_cursor = 0
        self._deduplicated_files = 0
        self._deduplicated_bytes = 0
        self._last_indexed_at: float | None = None
        self._last_index_error: str | None = None
        self._lock = threading.RLock()
        self._snapshot_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._job: tuple[
            Mapping[str, MultiSessionRegistry],
            Mapping[str, Mapping[str, str | None]],
            float | None,
        ] | None = None
        self._persistent = None
        if cache_path is not None:
            try:
                self._persistent = _UsageIndexStore(cache_path)
            except (OSError, sqlite3.DatabaseError):
                # 中文注释：无法创建索引时仍可退化为内存缓存，不能影响监控主流程。
                self._persistent = None

    def snapshot(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        account_metadata: Mapping[str, Mapping[str, str | None]] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """读取所有 session JSONL 并返回四个时间范围的安全汇总。"""

        metadata_by_profile = account_metadata or {}
        scope = self._scope_key(registries, metadata_by_profile)
        if self.background_indexing and now is None:
            # 中文注释：HTTP 只读取已发布快照，不等待索引的磁盘读取和聚合锁。
            with self._snapshot_lock:
                self._job = (registries, metadata_by_profile, now)
                cached = self._snapshot_cache
                cached_scope = self._snapshot_scope
            self._ensure_worker()
            if cached is not None and scope == cached_scope:
                return cached
        with self._lock:
            self._job = (registries, metadata_by_profile, now)
            if self.background_indexing:
                self._ensure_worker()
            current_monotonic = time.monotonic()
            cached_snapshot = self._snapshot_cache
            indexing_complete = True
            if isinstance(cached_snapshot, Mapping):
                indexing = cached_snapshot.get("indexing")
                if isinstance(indexing, Mapping):
                    indexing_complete = bool(indexing.get("complete", True))
            if (
                now is None
                and cached_snapshot is not None
                and scope == self._snapshot_scope
            ):
                if self.background_indexing and not indexing_complete:
                    # 中文注释：后台线程正在继续读盘，HTTP 请求不再同步读取历史。
                    return cached_snapshot
                if (
                    indexing_complete
                    and current_monotonic - self._snapshot_cached_at
                    < self.refresh_interval
                ):
                    return cached_snapshot
            return self._run_index_once(registries, metadata_by_profile, now, scope)

    def close(self) -> None:
        """停止后台索引线程。"""

        self._stop.set()
        worker = self._worker
        self._worker = None
        if worker is not None and worker.is_alive():
            worker.join(timeout=2)

    def update_homes(self, homes: ProviderHomesInput) -> None:
        """热更新各 provider 的扫描目录；未给出或 None 保持不变，显式元组（含空）替换。

        只替换实例属性，下一轮 ``_index_once``/``_build_sources`` 自动按新目录
        建源；被移除目录的内存缓存和索引行由 ``_remove_stale_cache`` 和
        ``store.prune`` 在下一轮清出，不删除磁盘上的索引文件。
        """

        with self._lock:
            self._homes = update_provider_homes(self._homes, homes)
            grok_homes = self._homes["grok"]
            self._grok_sessions = {
                home: index
                for home, index in self._grok_sessions.items()
                if home in grok_homes
            }
            self._grok_sessions_at = {
                home: cached_at
                for home, cached_at in self._grok_sessions_at.items()
                if home in grok_homes
            }
            kimi_homes = self._homes["kimi"]
            self._kimi_sessions = {
                home: index
                for home, index in self._kimi_sessions.items()
                if home in kimi_homes
            }
            self._kimi_sessions_at = {
                home: cached_at
                for home, cached_at in self._kimi_sessions_at.items()
                if home in kimi_homes
            }
            self._claude_sidechain_cache = {
                home: cached
                for home, cached in self._claude_sidechain_cache.items()
                if home in self._homes["claude"]
            }
            # 中文注释：目录变化后立即丢弃展示缓存，不能继续显示旧账号或旧筛选项。
            with self._snapshot_lock:
                self._snapshot_scope = None
                self._facets_cache = None
                self._search_cache.clear()

    def _ensure_worker(self) -> None:
        """启动一次性的后台索引线程。"""

        with self._worker_lock:
            self._start_worker()

    def _start_worker(self) -> None:
        """在工作线程创建锁内启动索引器。"""

        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._run_background,
            name="usage-indexer",
            daemon=True,
        )
        self._worker.start()

    def _run_background(self) -> None:
        """按限速把 JSONL 索引完，不阻塞 Dashboard 请求。"""

        while not self._stop.is_set():
            read_bytes = 0
            complete = True
            with self._lock:
                job = self._job
                if job is not None:
                    try:
                        snapshot = self._run_index_once(*job)
                    except Exception:  # noqa: BLE001 - 索引失败不能杀掉后台线程
                        # 中文注释：错误已由 _run_index_once 记录；下一轮按
                        # 完整节奏重试，避免异常造成忙等。
                        complete = True
                    else:
                        indexing = snapshot.get("indexing")
                        if isinstance(indexing, Mapping):
                            complete = bool(indexing.get("complete", True))
                            read_bytes = int(
                                indexing.get("read_bytes_this_refresh") or 0
                            )
            delay = self._background_delay(complete, read_bytes)
            if self._stop.wait(timeout=delay):
                return

    def _background_delay(self, complete: bool, read_bytes: int) -> float:
        """计算后台下一轮等待时间，限制平均读速和空转频率。"""

        if complete:
            # 中文注释：索引完成后只按正常刷新周期检查追加，禁止每 2 秒
            # 重建全部时间窗口和聚合结果。
            return self.refresh_interval
        minimum_delay = max(
            0.05,
            min(1.0, self.read_budget_bytes / self.index_bytes_per_sec),
        )
        return max(minimum_delay, read_bytes / self.index_bytes_per_sec)

    def _run_index_once(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        metadata_by_profile: Mapping[str, Mapping[str, str | None]],
        now: float | None,
        scope: tuple[tuple[str, str, str, str], ...] | None = None,
    ) -> dict[str, Any]:
        """执行一轮索引并维护健康摘要；异常记录后原样抛出。"""

        try:
            snapshot = self._index_once(registries, metadata_by_profile, now, scope)
        except Exception as error:
            self._last_index_error = sanitize_error(error)
            raise
        self._last_indexed_at = time.time()
        self._last_index_error = None
        return snapshot

    def index_health(self) -> dict[str, object]:
        """返回用量索引的健康摘要；未启用后台索引时 worker_alive 为 None。"""

        worker = self._worker
        return {
            "last_indexed_at": self._last_indexed_at,
            "last_error": self._last_index_error,
            "worker_alive": worker.is_alive() if worker is not None else None,
        }

    def _index_once(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        metadata_by_profile: Mapping[str, Mapping[str, str | None]],
        now: float | None,
        scope: tuple[tuple[str, str, str, str], ...] | None = None,
    ) -> dict[str, Any]:
        """在已持有锁的情况下读取一轮有界 JSONL 并更新快照。"""

        if scope is None:
            scope = self._scope_key(registries, metadata_by_profile)
        observed_at = time.time() if now is None else float(now)
        sources = self._build_sources(registries, metadata_by_profile)
        self._remove_stale_cache(sources)
        file_deltas: dict[Path, tuple[UsageDelta, ...]] = {}
        effective_sources = dict(sources)
        ordered_paths = tuple(sorted(sources, key=str))
        if ordered_paths:
            start = self._index_cursor % len(ordered_paths)
            ordered_paths = ordered_paths[start:] + ordered_paths[:start]
        remaining_budget = self.read_budget_bytes
        next_cursor = self._index_cursor
        read_bytes_this_refresh = 0
        for path_index, path in enumerate(ordered_paths):
            cached_file = self._cache.get(path)
            if remaining_budget > 0:
                per_file_budget = min(
                    remaining_budget,
                    self._PER_FILE_READ_BUDGET,
                )
                cached_file = self._read_file(
                    path,
                    maximum_bytes=per_file_budget,
                )
                if cached_file is not None:
                    read_bytes = cached_file.last_read_bytes
                    remaining_budget = max(0, remaining_budget - read_bytes)
                    read_bytes_this_refresh += read_bytes
                    if read_bytes > 0:
                        next_cursor = self._index_cursor + path_index + 1
            if cached_file is None:
                continue
            file_deltas[path] = cached_file.deltas
            source = sources[path]
            if source.project is None and cached_file.project is not None:
                effective_sources[path] = _UsageSource(
                    profile_name=source.profile_name,
                    account_id=source.account_id,
                    codex_home=source.codex_home,
                    project=cached_file.project,
                    product=source.product,
                )
        if ordered_paths:
            self._index_cursor = next_cursor % len(ordered_paths)
        # 中文注释：账号身份按文件写进索引，检索才能按账号聚合和筛选；
        # 这里覆盖本次扫描到的全部来源，不要求这轮真的读过文件。
        if self._persistent is not None:
            self._persistent.save_file_accounts(effective_sources)
        indexing = self._index_progress(
            tuple(sources),
            read_bytes_this_refresh=read_bytes_this_refresh,
        )
        indexing["bytes_per_sec"] = self.index_bytes_per_sec
        indexing["deduplicated_files"] = self._deduplicated_files
        indexing["deduplicated_bytes"] = self._deduplicated_bytes
        # 中文注释：未遍历完全部 JSONL 时不计算也不输出可被误认为最终值的数字。
        periods = (
            [
                self._build_period(
                    key=key,
                    label=label,
                    period_kind=period_kind,
                    now=observed_at,
                    sources=effective_sources,
                    file_deltas=file_deltas,
                    metadata_by_profile=metadata_by_profile,
                )
                for key, label, period_kind in self._PERIODS
            ]
            if indexing["complete"]
            else []
        )
        daily = (
            self._build_daily(observed_at, file_deltas)
            if indexing["complete"]
            else []
        )
        snapshot = {
            "observed_at": observed_at,
            "pricing": pricing_metadata(),
            "periods": periods,
            "daily": daily,
            "cache_seconds": self.refresh_interval,
            "indexing": indexing,
        }
        with self._snapshot_lock:
            self._snapshot_cache = snapshot
            self._snapshot_cached_at = time.monotonic()
            self._snapshot_scope = scope
        return snapshot

    def cached_snapshot(self, now: float | None = None) -> dict[str, Any]:
        """返回内存中的用量结果，不触碰 CODEX_HOME 历史文件。"""

        with self._snapshot_lock:
            if self._snapshot_cache is not None:
                return self._snapshot_cache
        return self.empty_snapshot(now=now)

    @staticmethod
    def empty_snapshot(now: float | None = None) -> dict[str, Any]:
        """返回没有扫描源时仍可被 Dashboard 使用的空结构。"""

        observed_at = time.time() if now is None else float(now)
        return {
            "observed_at": observed_at,
            "pricing": pricing_metadata(),
            "periods": [],
            "daily": [],
            "cache_seconds": 300.0,
        }

    @staticmethod
    def empty_insights(now: float | None = None) -> dict[str, Any]:
        """返回没有用量索引时仍可被 Dashboard 使用的空分析结构。"""

        return {
            "ready": False,
            "observed_at": time.time() if now is None else float(now),
            "indexing": None,
        }

    def insights(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        account_metadata: Mapping[str, Mapping[str, str | None]] | None = None,
        now: float | None = None,
        since_days: int | None = None,
        since: float | None = None,
        window_kind: str | None = None,
    ) -> dict[str, Any]:
        """对已索引的对话做习惯分析。

        统计窗口可以给「最近 N 天」（``since_days``），也可以直接给起始时间戳
        （``since``，用于「今天」这种按本地日历日对齐的窗口）；``window_kind``
        会原样出现在结果里，供界面显示窗口名称。
        """

        metadata_by_profile = account_metadata or {}
        with self._lock:
            snapshot = self.snapshot(registries, metadata_by_profile, now)
            indexing = snapshot.get("indexing")
            observed_at = float(snapshot.get("observed_at", time.time()))
            if not isinstance(indexing, Mapping) or not indexing.get("complete"):
                return {
                    "ready": False,
                    "observed_at": observed_at,
                    "indexing": dict(indexing) if isinstance(indexing, Mapping) else None,
                }
            if since is None:
                since = (
                    observed_at - since_days * 86_400
                    if since_days is not None and since_days > 0
                    else None
                )
            kind = window_kind or ("days" if since is not None else "all")
            sources = self._build_sources(registries, metadata_by_profile)
            conversations: list[_ConversationMetrics] = []
            for path, cached_file in self._cache.items():
                metrics = _conversation_metrics(
                    path,
                    cached_file,
                    sources.get(path),
                    since=since,
                )
                if metrics is not None:
                    conversations.append(metrics)
            return _build_insights(
                conversations,
                observed_at,
                window_days=since_days if since is not None else None,
                window_kind=kind,
                window_since=since,
            )

    def refresh_index(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        account_metadata: Mapping[str, Mapping[str, str | None]] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """执行一轮有界索引，让没有 Dashboard 的进程也能用最新用量数据。"""

        metadata_by_profile = account_metadata or {}
        with self._lock:
            return self._run_index_once(registries, metadata_by_profile, now)

    def _remove_stale_cache(self, sources: Mapping[Path, _UsageSource]) -> None:
        """删除已经不在扫描范围内的缓存，避免长期运行无限增长。"""

        for path in tuple(self._rollups):
            if path not in sources:
                del self._rollups[path]
        for path in tuple(self._cache):
            if path not in sources:
                del self._cache[path]
        for path in tuple(self._chat_partial):
            if path not in sources:
                del self._chat_partial[path]
        if self._persistent is not None:
            self._persistent.prune(sources)

    def _index_progress(
        self,
        paths: tuple[Path, ...],
        read_bytes_this_refresh: int,
    ) -> dict[str, int | float | bool]:
        """汇总有界索引进度，供 Dashboard 明确标注统计完整性。"""

        total_bytes = 0
        indexed_bytes = 0
        pending_files = 0
        for path in paths:
            cached = self._cache.get(path)
            try:
                current_signature = _file_signature(path)
            except OSError:
                continue
            if current_signature is None:
                continue
            file_size = current_signature[2]
            total_bytes += file_size
            if cached is None:
                pending_files += 1
                continue
            if cached.signature == current_signature and cached.complete:
                indexed_bytes += file_size
                continue
            pending_files += 1
            # 中文注释：文件发生追加时保留已确认偏移，重写/截断则从零计进度。
            if (
                cached.signature[0] == current_signature[0]
                and file_size >= cached.next_offset
            ):
                indexed_bytes += min(cached.next_offset, file_size)
        percent = 100.0 if total_bytes == 0 else indexed_bytes * 100 / total_bytes
        return {
            "complete": pending_files == 0,
            "files": len(paths),
            "pending_files": pending_files,
            "indexed_bytes": indexed_bytes,
            "total_bytes": total_bytes,
            "percent": round(percent, 2),
            "read_bytes_this_refresh": read_bytes_this_refresh,
        }

    def _build_period(
        self,
        key: str,
        label: str,
        period_kind: str,
        now: float,
        sources: Mapping[Path, _UsageSource],
        file_deltas: Mapping[Path, tuple[UsageDelta, ...]],
        metadata_by_profile: Mapping[str, Mapping[str, str | None]],
    ) -> dict[str, Any]:
        """把文件增量过滤到一个时间范围并生成账号汇总。"""

        start = _period_start(period_kind, now)
        aggregates: dict[str, _UsageAggregate] = {}
        account_sources: dict[str, _UsageSource] = {}
        for profile_name, metadata in metadata_by_profile.items():
            profile = _text_value(metadata.get("profile_name")) or profile_name
            account_id = _text_value(metadata.get("account_id"))
            source = _UsageSource(
                profile_name=profile,
                account_id=account_id,
                codex_home=_text_value(metadata.get("codex_home")),
            )
            account_key = account_id or f"profile:{profile}"
            aggregate = aggregates.setdefault(account_key, _UsageAggregate())
            aggregate.profiles.add(profile)
            account_sources.setdefault(account_key, source)

        for path, deltas in file_deltas.items():
            source = sources[path]
            account_sources.setdefault(source.account_key, source)
            account = aggregates.setdefault(
                source.account_key,
                _UsageAggregate(),
            )
            for delta, estimate in self._rollup_entries(path, deltas, start, now):
                account.add(source, delta, estimate)

        accounts = []
        for account_key, aggregate in sorted(
            aggregates.items(),
            key=lambda item: item[0],
        ):
            source = account_sources.get(account_key)
            if source is None:
                source = _source_for_account_key(account_key, sources)
            accounts.append(
                _aggregate_to_dict(
                    aggregate,
                    account_name=(source.account_name if source else account_key),
                    account_id=(source.account_id if source else None),
                    profiles=aggregate.profiles,
                )
            )
        return {
            "key": key,
            "label": label,
            "start_at": start,
            "end_at": now,
            "accounts": accounts,
        }

    def _build_daily(
        self,
        now: float,
        file_deltas: Mapping[Path, tuple[UsageDelta, ...]],
    ) -> list[dict[str, Any]]:
        """按本地自然日汇总近 30 天全部账号的 token 与 API 等价金额。"""

        today = to_local(now).date()
        days: list[dict[str, Any]] = []
        index: dict[str, dict[str, Any]] = {}
        for offset in range(self._DAILY_TREND_DAYS - 1, -1, -1):
            key = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            entry = {
                "date": key,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
                "has_unpriced": False,
            }
            days.append(entry)
            index[key] = entry
        first_start = local_day_start(now, days_ago=self._DAILY_TREND_DAYS - 1)
        for path, deltas in file_deltas.items():
            for delta, estimate in self._rollup_entries(path, deltas, first_start, now):
                key = local_day_key(delta.timestamp)
                entry = index.get(key)
                if entry is None:
                    continue
                entry["total_tokens"] += delta.usage.total_tokens
                cost = estimate.get("estimated_cost_usd")
                if cost is None:
                    entry["has_unpriced"] = True
                else:
                    entry["estimated_cost_usd"] += float(cost)
        for entry in days:
            entry["estimated_cost_usd"] = _round_number(entry["estimated_cost_usd"])
        return days

    def _rollup_entries(
        self,
        path: Path,
        deltas: tuple[UsageDelta, ...],
        start: float,
        end: float,
    ) -> Iterator[tuple[UsageDelta, Mapping[str, Any]]]:
        """按文件更新新尾部，再复用各窗口共用的计价分桶。"""

        rollup = self._rollups.setdefault(path, _FileRollup())
        rollup.update(deltas)
        yield from rollup.entries(start, end)


def _source_for_account_key(
    account_key: str,
    sources: Mapping[Path, _UsageSource],
) -> _UsageSource | None:
    """从文件来源中找到账号的展示信息。"""

    for source in sources.values():
        if source.account_key == account_key:
            return source
    if account_key.startswith("profile:"):
        return _UsageSource(
            profile_name=account_key.removeprefix("profile:"),
            account_id=None,
            codex_home=None,
        )
    return None
