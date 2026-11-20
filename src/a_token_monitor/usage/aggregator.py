"""``UsageAggregator``：发现各家会话文件、有界增量读取并输出用量快照。"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from typing import Any, ContextManager, Iterator, Mapping

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
from .timed_cache import TimedCache
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
    # 中文注释：单轮最多读 8 MiB、单文件最多 4 MiB；突发读取量有界，平均读速
    # 再由 _DEFAULT_INDEX_BYTES_PER_SEC 和 _INDEX_DUTY_CYCLE 共同限制。
    _DEFAULT_READ_BUDGET = 8 * 1024 * 1024
    _PER_FILE_READ_BUDGET = 4 * 1024 * 1024
    _PERIODS = (
        ("today", "今天", "calendar_day"),
        ("seven_days", "近 7 天", "seven_days"),
        ("month", "近 30 天", "thirty_days"),
        ("year", "近 365 天", "three_hundred_sixty_five_days"),
    )

    _DAILY_TREND_DAYS = 30

    # 中文注释：热力图按自然周对齐，53 列覆盖最近一年（含当前未走完的一周）。
    _HEATMAP_WEEKS = 53

    # 中文注释：后台索引平均读速上限。补完几十到一两百 MiB 的增量约 10 秒内
    # 完成，又远低于磁盘带宽，不会把冷 vhdx 读满。
    _DEFAULT_INDEX_BYTES_PER_SEC = 16 * 1024 * 1024

    # 中文注释：后台索引的工作占空比。每轮工作 t 秒后至少休息
    # t × (1 − duty) / duty 秒，慢盘读一轮更久时自动降速，磁盘至少一半时间空闲。
    _INDEX_DUTY_CYCLE = 0.5

    # 中文注释：一轮没有读到任何新字节却仍未完成（例如文件刚被改写、超限的
    # projcache），继续高频空转没有意义，按这个间隔重试。
    _INDEX_IDLE_RETRY_SECONDS = 1.0

    # 中文注释：实测吞吐的指数滑动平均系数，前端用它估算剩余时间。
    _THROUGHPUT_EMA_ALPHA = 0.3

    def __init__(
        self,
        discovery_interval: float = 300.0,
        refresh_interval: float = 300.0,
        read_budget_bytes: int = _DEFAULT_READ_BUDGET,
        cache_path: Path | None = None,
        background_indexing: bool = False,
        index_bytes_per_sec: int = _DEFAULT_INDEX_BYTES_PER_SEC,
        homes: ProviderHomesInput | None = None,
        prune_stale: bool = True,
    ) -> None:
        """创建有刷新间隔、持久化检查点和单轮磁盘预算的用量缓存。

        ``prune_stale`` 控制是否从持久索引删除不在本实例扫描范围内的文件。
        索引文件由 daemon 与一次性命令共享，只有长期运行、范围完整的 daemon
        才应清理；一次性命令（例如 ``sessions``）的范围可能和 daemon 不同，
        清理会误删 daemon 建好的其他 provider 的索引。
        """

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
        self.prune_stale = prune_stale
        # 中文注释：聚合器里未给出的 provider 一律不扫描，避免单测把本机数据读进来；
        # 监控进程要自动探测时先 resolve，再把结果映射传进来。
        self._homes = resolve_provider_homes(homes, auto_detect=False)
        self._grok_sessions: TimedCache[Path, dict[str, GrokSessionInfo]] = TimedCache()
        self._kimi_sessions: TimedCache[Path, dict[str, KimiSessionInfo]] = TimedCache()
        self._claude_sidechain_cache: dict[Path, tuple[float, bool]] = {}
        self._chat_partial: dict[Path, tuple[Any, ...]] = {}
        self._cache: dict[Path, _CachedFile] = {}
        self._rollups: dict[Path, _FileRollup] = {}
        # 中文注释：目录列举结果按 discovery_interval 缓存，键是扫描根或缓存身份。
        self._discovered: TimedCache[Path, tuple[Path, ...]] = TimedCache()
        self._snapshot_cache: dict[str, Any] | None = None
        self._snapshot_cached_at = 0.0
        # 中文注释：None 表示目录刚变化、展示缓存已作废。
        self._snapshot_scope: tuple[tuple[str, str, str, str], ...] | None = ()
        self._facets_cache: dict[str, Any] | None = None
        self._facets_cached_at = 0.0
        self._search_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
        self._index_cursor = 0
        self._deduplicated_files = 0
        self._deduplicated_bytes = 0
        self._last_indexed_at: float | None = None
        self._last_index_error: str | None = None
        # 中文注释：后台索引的实测吞吐（字节/秒，EMA），None 表示还没有样本。
        self._throughput_ema: float | None = None
        # 中文注释：首轮用一次批量查询预热内存缓存，之后只按需逐文件兜底。
        self._cache_warmed = False
        # 中文注释：本轮预热耗时。预热是一次性顺序读 SQLite 和 CPU 解码，
        # 不是 JSONL 读盘，不计入占空比的休息时间。
        self._last_warm_seconds = 0.0
        # 中文注释：上次成功写入索引的来源集合和账号映射；没变时跳过
        # prune 和 save_file_accounts，避免每轮全表读写。
        self._pruned_paths: frozenset[Path] | None = None
        self._saved_accounts: dict[Path, tuple[Any, ...]] | None = None
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
            self._grok_sessions.retain(lambda home: home in grok_homes)
            kimi_homes = self._homes["kimi"]
            self._kimi_sessions.retain(lambda home: home in kimi_homes)
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
            started = time.monotonic()
            # 中文注释：elapsed 是本轮读 JSONL、解析和写索引的时间，用于占空比；
            # busy 另外包含预建分桶这类纯 CPU 工作，用于按读速上限摊开整轮周期。
            # 首轮的批量预热只顺序读一次 SQLite，两者都不计入。
            elapsed = 0.0
            busy = 0.0
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
                    work_end = time.monotonic()
                    elapsed = max(
                        0.0, work_end - started - self._last_warm_seconds
                    )
                    self._last_warm_seconds = 0.0
                    if not complete and read_bytes > 0:
                        # 中文注释：按读速上限本来就要等一段时间，这段空档里先把
                        # 已缓存文件的分桶和计价算好，完成那一轮不必一次性处理
                        # 全部历史增量。只用不会拉长本轮周期的那部分空档。
                        cycle = read_bytes / self.index_bytes_per_sec
                        slack = cycle - elapsed / self._INDEX_DUTY_CYCLE
                        if slack > 0:
                            self._prebuild_rollups(work_end + slack)
                    busy = elapsed + (time.monotonic() - work_end)
            delay = self._background_delay(complete, read_bytes, elapsed, busy)
            if not complete:
                self._record_throughput(read_bytes, busy + delay)
            if self._stop.wait(timeout=delay):
                return

    def _background_delay(
        self,
        complete: bool,
        read_bytes: int,
        elapsed: float = 0.0,
        busy: float | None = None,
    ) -> float:
        """计算后台下一轮等待时间，同时限制平均读速和工作占空比。

        ``elapsed`` 是本轮读盘工作的耗时；``busy`` 是本轮全部工作耗时（含纯
        CPU 的预建分桶，默认等于 ``elapsed``）。未完成时取三者最大值：

        - ``read_bytes / index_bytes_per_sec − busy``：整轮周期（工作 + 等待）
          不短于按读速上限读完本轮字节所需的时间，平均读速不超过上限；
        - ``elapsed × (1 − duty) / duty``：读盘工作最多占 ``_INDEX_DUTY_CYCLE``，
          慢盘读一轮更久时自动降速；
        - 0.05 秒下限。

        本轮没有读到新字节时按 ``_INDEX_IDLE_RETRY_SECONDS`` 重试，避免空转。
        """

        if complete:
            # 中文注释：索引完成后只按正常刷新周期检查追加，禁止每 2 秒
            # 重建全部时间窗口和聚合结果。
            return self.refresh_interval
        duty = self._INDEX_DUTY_CYCLE
        work = max(0.0, float(elapsed))
        total = work if busy is None else max(work, float(busy))
        delay = max(
            0.05,
            read_bytes / self.index_bytes_per_sec - total,
            work * (1.0 - duty) / duty,
        )
        if read_bytes <= 0:
            delay = max(delay, self._INDEX_IDLE_RETRY_SECONDS)
        return delay

    def _prebuild_rollups(self, deadline: float) -> None:
        """在 ``deadline``（monotonic）之前为已缓存文件预建分桶。

        ``_FileRollup.update`` 对同一增量序列幂等、对追加只处理新尾部，
        预建与完成时现算的结果完全一致。
        """

        for path, cached_file in tuple(self._cache.items()):
            if time.monotonic() >= deadline:
                return
            deltas = cached_file.deltas
            rollup = self._rollups.get(path)
            if rollup is not None and rollup.sequence is deltas:
                continue
            if rollup is None:
                rollup = self._rollups.setdefault(path, _FileRollup())
            rollup.update(deltas)

    def _record_throughput(self, read_bytes: int, cycle_seconds: float) -> None:
        """用一轮「工作 + 休息」的实测吞吐更新 EMA，供前端估算剩余时间。"""

        if read_bytes <= 0 or cycle_seconds <= 0:
            return
        sample = read_bytes / cycle_seconds
        previous = self._throughput_ema
        alpha = self._THROUGHPUT_EMA_ALPHA
        self._throughput_ema = (
            sample if previous is None else previous + alpha * (sample - previous)
        )

    def _reported_bytes_per_sec(self) -> int:
        """返回给前端的读速：有实测样本时用 EMA，且不超过配置上限。"""

        measured = self._throughput_ema
        if measured is None or measured <= 0:
            return self.index_bytes_per_sec
        return max(1, int(min(float(self.index_bytes_per_sec), measured)))

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
        self._warm_cache(sources)
        file_deltas: dict[Path, tuple[UsageDelta, ...]] = {}
        # 中文注释：读取时已经 stat 过的签名留给进度统计复用，不再重复 stat。
        signatures: dict[Path, tuple[int, int, int]] = {}
        effective_sources = dict(sources)
        ordered_paths = tuple(sorted(sources, key=str))
        if ordered_paths:
            start = self._index_cursor % len(ordered_paths)
            ordered_paths = ordered_paths[start:] + ordered_paths[:start]
        remaining_budget = self.read_budget_bytes
        next_cursor = self._index_cursor
        read_bytes_this_refresh = 0
        with self._index_transaction():
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
                        signatures[path] = cached_file.signature
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
        self._save_file_accounts(effective_sources)
        indexing = self._index_progress(
            tuple(sources),
            read_bytes_this_refresh=read_bytes_this_refresh,
            signatures=signatures,
        )
        indexing["bytes_per_sec"] = self._reported_bytes_per_sec()
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
        heatmap = (
            self._build_heatmap(observed_at, file_deltas)
            if indexing["complete"]
            else []
        )
        snapshot = {
            "observed_at": observed_at,
            "pricing": pricing_metadata(),
            "periods": periods,
            "daily": daily,
            "heatmap": heatmap,
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
            "heatmap": [],
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
        if self._persistent is not None and self.prune_stale:
            # 中文注释：prune 要全表读出路径再比对，来源集合没变时没有可删的行，
            # 跳过；写入失败时不记录，下一轮重试。
            current = frozenset(sources)
            if current != self._pruned_paths and self._persistent.prune(sources):
                self._pruned_paths = current

    def _save_file_accounts(self, sources: Mapping[Path, _UsageSource]) -> None:
        """账号映射相对上次成功写入有变化时才写索引。"""

        if self._persistent is None:
            return
        current = {
            path: (
                source.account_key,
                source.account_name,
                source.account_id,
                source.profile_name,
                source.product,
            )
            for path, source in sources.items()
        }
        if current == self._saved_accounts:
            return
        if self._persistent.save_file_accounts(sources) is not False:
            self._saved_accounts = current

    def _warm_cache(self, sources: Mapping[Path, _UsageSource]) -> None:
        """首轮用一次批量查询把持久索引读进内存，代替逐文件开连接查询。"""

        if self._cache_warmed or self._persistent is None:
            return
        self._cache_warmed = True
        missing = [path for path in sources if path not in self._cache]
        if not missing:
            return
        started = time.monotonic()
        for path, cached in self._persistent.load_many(missing).items():
            self._cache.setdefault(path, cached)
        self._last_warm_seconds = time.monotonic() - started

    def _index_transaction(self) -> ContextManager[None]:
        """一轮读取共用一个 SQLite 连接并只提交一次；没有持久索引时为空操作。"""

        if self._persistent is None:
            return nullcontext()
        return self._persistent.transaction()

    def _index_progress(
        self,
        paths: tuple[Path, ...],
        read_bytes_this_refresh: int,
        signatures: Mapping[Path, tuple[int, int, int]] | None = None,
    ) -> dict[str, int | float | bool]:
        """汇总有界索引进度，供 Dashboard 明确标注统计完整性。

        ``signatures`` 是本轮读取时已经拿到的文件签名，命中时直接复用，
        只给本轮没读到的文件补一次 stat。
        """

        known = signatures or {}
        total_bytes = 0
        indexed_bytes = 0
        pending_files = 0
        for path in paths:
            cached = self._cache.get(path)
            current_signature = known.get(path)
            if current_signature is None:
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
            account_source = account_sources.get(account_key)
            if account_source is None:
                account_source = _source_for_account_key(account_key, sources)
            accounts.append(
                _aggregate_to_dict(
                    aggregate,
                    account_name=(
                        account_source.account_name if account_source else account_key
                    ),
                    account_id=(account_source.account_id if account_source else None),
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
            entry: dict[str, Any] = {
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
                day_entry = index.get(key)
                if day_entry is None:
                    continue
                day_entry["total_tokens"] += delta.usage.total_tokens
                cost = estimate.get("estimated_cost_usd")
                if cost is None:
                    day_entry["has_unpriced"] = True
                else:
                    day_entry["estimated_cost_usd"] += float(cost)
        for entry in days:
            entry["estimated_cost_usd"] = _round_number(entry["estimated_cost_usd"])
        return days

    def _build_heatmap(
        self,
        now: float,
        file_deltas: Mapping[Path, tuple[UsageDelta, ...]],
    ) -> list[dict[str, Any]]:
        """按本地自然日汇总最近 53 周的 token、请求数和 API 等价金额。

        起点对齐到 52 周前的周一，终点是当前周的周日：前端可以固定画
        53 列 × 7 行，不需要再处理首尾不完整的星期。当前周尚未到来的
        日期保留为零并标记 ``future``，避免被误认为真实的零用量。
        """

        today = to_local(now).date()
        start_offset = today.weekday() + (self._HEATMAP_WEEKS - 1) * 7
        start_day = today - timedelta(days=start_offset)
        days: list[dict[str, Any]] = []
        index: dict[str, dict[str, Any]] = {}
        for offset in range(self._HEATMAP_WEEKS * 7):
            day = start_day + timedelta(days=offset)
            key = day.strftime("%Y-%m-%d")
            entry: dict[str, Any] = {
                "date": key,
                "total_tokens": 0,
                "requests": 0,
                "estimated_cost_usd": 0.0,
                "has_unpriced": False,
                "future": day > today,
            }
            days.append(entry)
            index[key] = entry
        first_start = local_day_start(now, days_ago=start_offset)
        for path, deltas in file_deltas.items():
            for delta, estimate, requests in self._rollup_entries_with_requests(
                path, deltas, first_start, now
            ):
                key = local_day_key(delta.timestamp)
                day_entry = index.get(key)
                if day_entry is None:
                    continue
                day_entry["total_tokens"] += delta.usage.total_tokens
                day_entry["requests"] += requests
                cost = estimate.get("estimated_cost_usd")
                if cost is None:
                    day_entry["has_unpriced"] = True
                else:
                    day_entry["estimated_cost_usd"] += float(cost)
        for entry in days:
            entry["estimated_cost_usd"] = _round_number(entry["estimated_cost_usd"])
        return days

    def _rollup_entries_with_requests(
        self,
        path: Path,
        deltas: tuple[UsageDelta, ...],
        start: float,
        end: float,
    ) -> Iterator[tuple[UsageDelta, Mapping[str, Any], int]]:
        """更新文件分桶并返回带请求数的汇总。"""

        rollup = self._rollups.setdefault(path, _FileRollup())
        rollup.update(deltas)
        yield from rollup.entries_with_requests(start, end)

    def _rollup_entries(
        self,
        path: Path,
        deltas: tuple[UsageDelta, ...],
        start: float,
        end: float,
    ) -> Iterator[tuple[UsageDelta, Mapping[str, Any]]]:
        """按文件更新新尾部，再复用各窗口共用的计价分桶。"""

        for delta, estimate, _requests in self._rollup_entries_with_requests(
            path, deltas, start, end
        ):
            yield delta, estimate


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
