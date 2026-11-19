"""订阅额度的周期历史：5 小时与周窗口的采样、回填和结算。

周期表放在现有的 ``provider_quota.sqlite3`` 里，和每日基线共用一个库，
但不参与保留期清理。每个「产品 × 账号 × 窗口 × 周期起点」一行，记下
周期内观察到的最高已用百分比；周期结束后把这段时间的 token 和 API
等价美元固化下来，避免用量明细过期后无法再算。
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from .discovery import JsonlSessionReader
from .grok import (
    grok_unified_log,
    read_grok_account,
    read_grok_billing_snapshots,
    read_grok_quota,
)
from .claude import read_claude_account, read_claude_quota
from .provider_quota_baseline import (
    ProviderQuotaBaselineError,
    ProviderQuotaBaselineStore,
)
from .quota import QuotaSnapshot, QuotaWindow, quota_period
from .usage import TokenUsage, _UsageIndexStore, _estimate_usage


COMPARE_PRODUCTS = frozenset({"claude", "codex", "grok"})
PERIOD_FIVE_HOURS = "five_hours"
PERIOD_WEEK = "week"
COMPARE_PERIODS = frozenset({PERIOD_FIVE_HOURS, PERIOD_WEEK})

SOURCE_LIVE = "live"
SOURCE_BACKFILL_CODEX = "backfill_codex"
SOURCE_BACKFILL_GROK = "backfill_grok_sparse"
CYCLE_SOURCES = frozenset(
    {SOURCE_LIVE, SOURCE_BACKFILL_CODEX, SOURCE_BACKFILL_GROK}
)

SETTLED_PENDING = "pending"
SETTLED_DONE = "done"

# 中文注释：5 小时窗口按 5 分钟取整，周窗口按 1 小时取整，吸收重置时间的抖动。
_FIVE_HOUR_QUANTUM = 300.0
_WEEK_QUANTUM = 3600.0
_INCOMPLETE_GAP_SECONDS = 15 * 60.0
_NOMINAL_WINDOW_SECONDS = {
    PERIOD_FIVE_HOURS: 5 * 3600.0,
    PERIOD_WEEK: 7 * 86400.0,
}
_DEFAULT_WINDOW_MINUTES = {
    PERIOD_FIVE_HOURS: 300.0,
    PERIOD_WEEK: 10080.0,
}

CLAUDE_SAMPLE_INTERVAL = 600.0
GROK_SAMPLE_INTERVAL = 300.0
PRE_RESET_LEAD_SECONDS = 180.0
BACKFILL_INTERVAL = 600.0
_BACKFILL_CHUNK_BYTES = 1024 * 1024

_LOGGER = logging.getLogger(__name__)


class QuotaCycleError(RuntimeError):
    """额度周期库不可用。"""


def normalize_period(period: str | None) -> str | None:
    """把接口和内部的周期名收成 ``five_hours`` 或 ``week``。"""

    key = (period or "").strip().lower().replace("-", "_")
    if key in {"5h", "five_hours", "five_hour"}:
        return PERIOD_FIVE_HOURS
    if key in {"week", "weekly", "7d"}:
        return PERIOD_WEEK
    return None


def period_api_name(period: str) -> str:
    """返回查询参数使用的周期名。"""

    normalized = normalize_period(period)
    if normalized == PERIOD_FIVE_HOURS:
        return "5h"
    if normalized == PERIOD_WEEK:
        return "week"
    raise ValueError(f"未知额度周期: {period}")


def quantize_cycle_start(raw_start: float, period: str) -> float:
    """把周期起点取整到该窗口的去抖网格上。"""

    quantum = _quantum_for(period)
    return math.floor(float(raw_start) / quantum + 0.5) * quantum


def _quantum_for(period: str) -> float:
    if period == PERIOD_FIVE_HOURS:
        return _FIVE_HOUR_QUANTUM
    if period == PERIOD_WEEK:
        return _WEEK_QUANTUM
    raise ValueError(f"未知额度周期: {period}")


def cycle_is_incomplete(
    sample_count: int,
    last_observed_at: float,
    resets_at: float | None,
) -> bool:
    """末次观察离重置超过 15 分钟，或采样少于 2 次，都算不完整。"""

    if sample_count < 2 or resets_at is None:
        return True
    return float(resets_at) - float(last_observed_at) > _INCOMPLETE_GAP_SECONDS


def is_design_window(window: QuotaWindow) -> bool:
    """Claude Design 的周窗口不参与编码额度对比。"""

    text = f"{window.limit_id} {window.name}".lower()
    return "design" in text or "omelette" in text


@dataclass(frozen=True)
class QuotaCycle:
    """周期表里的一行。"""

    product: str
    account_key: str
    period: str
    cycle_start: float
    max_used_percent: float
    first_observed_at: float
    last_observed_at: float
    resets_at: float | None
    sample_count: int
    source: str
    incomplete: bool
    settled: str
    tokens: int | None
    cost_usd: float | None


class QuotaCycleStore:
    """按账号和窗口持久化额度周期，复用 provider 额度库。"""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir.expanduser()
        self.database_file = self.state_dir / "provider_quota.sqlite3"
        self._ensure_database()

    def _ensure_database(self) -> None:
        """创建受保护的状态目录、周期表和回填检查点。"""

        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.state_dir.chmod(0o700)
            with self._connection() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS provider_quota_cycle (
                        product TEXT NOT NULL,
                        account_key TEXT NOT NULL,
                        period TEXT NOT NULL,
                        cycle_start REAL NOT NULL,
                        max_used_percent REAL NOT NULL,
                        first_observed_at REAL NOT NULL,
                        last_observed_at REAL NOT NULL,
                        resets_at REAL,
                        sample_count INTEGER NOT NULL,
                        source TEXT NOT NULL,
                        incomplete INTEGER NOT NULL,
                        settled TEXT NOT NULL,
                        tokens INTEGER,
                        cost_usd REAL,
                        PRIMARY KEY (
                            product, account_key, period, cycle_start
                        )
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS provider_quota_backfill_file (
                        path TEXT PRIMARY KEY,
                        product TEXT NOT NULL,
                        inode INTEGER NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        file_size INTEGER NOT NULL,
                        next_offset INTEGER NOT NULL
                    )
                    """
                )
        except (OSError, sqlite3.Error) as error:
            raise QuotaCycleError("无法初始化额度周期库") from error

    def _protect_database_files(self) -> None:
        """限制数据库及 WAL 辅助文件权限，与每日基线库保持一致。"""

        for suffix in ("", "-wal", "-shm"):
            path = self.database_file.with_name(self.database_file.name + suffix)
            try:
                if path.exists():
                    path.chmod(0o600)
            except OSError:
                continue

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """每次操作独立连接，提交或回滚后及时关闭。"""

        connection = sqlite3.connect(self.database_file, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._protect_database_files()

    def observe(
        self,
        product: str,
        account_key: str,
        snapshot: QuotaSnapshot,
        *,
        source: str = SOURCE_LIVE,
    ) -> tuple[QuotaCycle, ...]:
        """记录一次快照。只收 Claude、Codex、Grok 的 5 小时和周窗口。

        返回本轮因此结束、需要结算的旧周期。Claude Design 窗口直接跳过。
        """

        if product not in COMPARE_PRODUCTS or source not in CYCLE_SOURCES:
            return ()
        if not account_key.strip():
            return ()
        closed: list[QuotaCycle] = []
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for period, window in _compare_windows(snapshot):
                    minutes = window.window_minutes or _DEFAULT_WINDOW_MINUTES[period]
                    resets_at = window.resets_at
                    used = window.used_percent
                    if resets_at is None or used is None or minutes <= 0:
                        continue
                    raw_start = float(resets_at) - float(minutes) * 60.0
                    cycle_start = self._debounced_start(
                        connection,
                        product,
                        account_key,
                        period,
                        raw_start,
                    )
                    previous = self._load_row(
                        connection, product, account_key, period, cycle_start
                    )
                    merged = _merge_observation(
                        previous,
                        product=product,
                        account_key=account_key,
                        period=period,
                        cycle_start=cycle_start,
                        used_percent=float(used),
                        observed_at=float(snapshot.observed_at),
                        resets_at=float(resets_at),
                        source=source,
                    )
                    if merged is None:
                        continue
                    self._upsert(connection, merged)
                    if previous is None:
                        closed.extend(
                            self._close_older(
                                connection,
                                product,
                                account_key,
                                period,
                                cycle_start,
                            )
                        )
        except sqlite3.Error as error:
            raise QuotaCycleError("无法写入额度周期") from error
        return tuple(closed)

    def recent(self, period: str, limit: int) -> tuple[QuotaCycle, ...]:
        """返回每个账号最近 ``limit`` 个该周期，新的在前。"""

        normalized = normalize_period(period)
        if normalized is None:
            raise ValueError(f"未知额度周期: {period}")
        if limit <= 0:
            return ()
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM provider_quota_cycle
                    WHERE period = ?
                    ORDER BY cycle_start DESC, product, account_key
                    """,
                    (normalized,),
                ).fetchall()
        except sqlite3.Error as error:
            raise QuotaCycleError("无法读取额度周期") from error
        kept: list[QuotaCycle] = []
        seen: dict[tuple[str, str], int] = {}
        for row in rows:
            cycle = _cycle_from_row(row)
            key = (cycle.product, cycle.account_key)
            count = seen.get(key, 0)
            if count >= limit:
                continue
            seen[key] = count + 1
            kept.append(cycle)
        return tuple(kept)

    def pending_cycles(self, now: float | None = None) -> tuple[QuotaCycle, ...]:
        """返回已经结束但还没固化 token / 美元的周期。"""

        moment = time.time() if now is None else float(now)
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM provider_quota_cycle
                    WHERE settled = ?
                    ORDER BY cycle_start
                    """,
                    (SETTLED_PENDING,),
                ).fetchall()
        except sqlite3.Error as error:
            raise QuotaCycleError("无法读取待结算周期") from error
        cycles = tuple(_cycle_from_row(row) for row in rows)
        latest: dict[tuple[str, str, str], float] = {}
        for cycle in cycles:
            key = (cycle.product, cycle.account_key, cycle.period)
            latest[key] = max(latest.get(key, cycle.cycle_start), cycle.cycle_start)
        # 中文注释：还在进行中的最新周期也是 pending，但不能提前结算。
        # 有更新的周期，或重置时间已到，才算这一轮结束。
        pending: list[QuotaCycle] = []
        all_rows = cycles
        # 最新周期可能已经 settled=done，待结算判断要看全表里的最大起点。
        try:
            with self._connection() as connection:
                bounds = connection.execute(
                    """
                    SELECT product, account_key, period, MAX(cycle_start) AS cycle_start
                    FROM provider_quota_cycle
                    GROUP BY product, account_key, period
                    """
                ).fetchall()
        except sqlite3.Error as error:
            raise QuotaCycleError("无法读取额度周期") from error
        for row in bounds:
            key = (str(row["product"]), str(row["account_key"]), str(row["period"]))
            latest[key] = float(row["cycle_start"])
        for cycle in all_rows:
            key = (cycle.product, cycle.account_key, cycle.period)
            ended = cycle.cycle_start < latest.get(key, cycle.cycle_start)
            if cycle.resets_at is not None and cycle.resets_at <= moment:
                ended = True
            if ended:
                pending.append(cycle)
        return tuple(pending)

    def settle(
        self,
        product: str,
        account_key: str,
        period: str,
        cycle_start: float,
        *,
        tokens: int | None,
        cost_usd: float | None,
        usage_ready: bool,
    ) -> None:
        """把一个已结束周期的 token 和美元写上；索引没就绪时保持待结算。"""

        normalized = normalize_period(period)
        if normalized is None:
            raise ValueError(f"未知额度周期: {period}")
        if not usage_ready:
            return
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE provider_quota_cycle
                    SET tokens = ?, cost_usd = ?, settled = ?
                    WHERE product = ? AND account_key = ? AND period = ?
                      AND cycle_start = ? AND settled = ?
                    """,
                    (
                        tokens,
                        cost_usd,
                        SETTLED_DONE,
                        product,
                        account_key,
                        normalized,
                        float(cycle_start),
                        SETTLED_PENDING,
                    ),
                )
        except sqlite3.Error as error:
            raise QuotaCycleError("无法结算额度周期") from error

    def backfill_checkpoint(self, path: str) -> tuple[int, int, int, int] | None:
        """返回文件的 inode、mtime、大小和已读偏移；没有检查点时返回 None。"""

        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT inode, mtime_ns, file_size, next_offset
                    FROM provider_quota_backfill_file
                    WHERE path = ?
                    """,
                    (path,),
                ).fetchone()
        except sqlite3.Error as error:
            raise QuotaCycleError("无法读取回填检查点") from error
        if row is None:
            return None
        return (
            int(row["inode"]),
            int(row["mtime_ns"]),
            int(row["file_size"]),
            int(row["next_offset"]),
        )

    def save_backfill_checkpoint(
        self,
        path: str,
        *,
        product: str,
        inode: int,
        mtime_ns: int,
        file_size: int,
        next_offset: int,
    ) -> None:
        """记下某个会话或日志文件已经回填到的位置。"""

        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO provider_quota_backfill_file (
                        path, product, inode, mtime_ns, file_size, next_offset
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        product = excluded.product,
                        inode = excluded.inode,
                        mtime_ns = excluded.mtime_ns,
                        file_size = excluded.file_size,
                        next_offset = excluded.next_offset
                    """,
                    (path, product, inode, mtime_ns, file_size, next_offset),
                )
        except sqlite3.Error as error:
            raise QuotaCycleError("无法写入回填检查点") from error

    def latest_open(self, now: float | None = None) -> tuple[QuotaCycle, ...]:
        """返回每个账号每个窗口当前还没到重置时间的最新周期。"""

        moment = time.time() if now is None else float(now)
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM provider_quota_cycle AS cycle
                    WHERE cycle_start = (
                        SELECT MAX(newer.cycle_start)
                        FROM provider_quota_cycle AS newer
                        WHERE newer.product = cycle.product
                          AND newer.account_key = cycle.account_key
                          AND newer.period = cycle.period
                    )
                    """
                ).fetchall()
        except sqlite3.Error as error:
            raise QuotaCycleError("无法读取当前额度周期") from error
        return tuple(
            cycle
            for cycle in (_cycle_from_row(row) for row in rows)
            if cycle.resets_at is not None and cycle.resets_at > moment
        )

    def _debounced_start(
        self,
        connection: sqlite3.Connection,
        product: str,
        account_key: str,
        period: str,
        raw_start: float,
    ) -> float:
        """取整，并在半个网格附近复用已有起点，避免抖动拆成两行。"""

        quantum = _quantum_for(period)
        snapped = quantize_cycle_start(raw_start, period)
        rows = connection.execute(
            """
            SELECT cycle_start FROM provider_quota_cycle
            WHERE product = ? AND account_key = ? AND period = ?
            """,
            (product, account_key, period),
        ).fetchall()
        for row in rows:
            start = float(row["cycle_start"])
            if abs(raw_start - start) < quantum:
                return start
        return snapped

    def _close_older(
        self,
        connection: sqlite3.Connection,
        product: str,
        account_key: str,
        period: str,
        cycle_start: float,
    ) -> list[QuotaCycle]:
        rows = connection.execute(
            """
            SELECT * FROM provider_quota_cycle
            WHERE product = ? AND account_key = ? AND period = ?
              AND cycle_start < ? AND settled = ?
            """,
            (product, account_key, period, cycle_start, SETTLED_PENDING),
        ).fetchall()
        return [_cycle_from_row(row) for row in rows]

    @staticmethod
    def _load_row(
        connection: sqlite3.Connection,
        product: str,
        account_key: str,
        period: str,
        cycle_start: float,
    ) -> QuotaCycle | None:
        row = connection.execute(
            """
            SELECT * FROM provider_quota_cycle
            WHERE product = ? AND account_key = ? AND period = ? AND cycle_start = ?
            """,
            (product, account_key, period, cycle_start),
        ).fetchone()
        if row is None:
            return None
        return _cycle_from_row(row)

    @staticmethod
    def _upsert(connection: sqlite3.Connection, cycle: QuotaCycle) -> None:
        connection.execute(
            """
            INSERT INTO provider_quota_cycle (
                product, account_key, period, cycle_start, max_used_percent,
                first_observed_at, last_observed_at, resets_at, sample_count,
                source, incomplete, settled, tokens, cost_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(product, account_key, period, cycle_start) DO UPDATE SET
                max_used_percent = excluded.max_used_percent,
                first_observed_at = excluded.first_observed_at,
                last_observed_at = excluded.last_observed_at,
                resets_at = excluded.resets_at,
                sample_count = excluded.sample_count,
                source = excluded.source,
                incomplete = excluded.incomplete,
                settled = provider_quota_cycle.settled,
                tokens = provider_quota_cycle.tokens,
                cost_usd = provider_quota_cycle.cost_usd
            """,
            (
                cycle.product,
                cycle.account_key,
                cycle.period,
                cycle.cycle_start,
                cycle.max_used_percent,
                cycle.first_observed_at,
                cycle.last_observed_at,
                cycle.resets_at,
                cycle.sample_count,
                cycle.source,
                int(cycle.incomplete),
                cycle.settled,
                cycle.tokens,
                cycle.cost_usd,
            ),
        )


def _compare_windows(
    snapshot: QuotaSnapshot,
) -> Iterator[tuple[str, QuotaWindow]]:
    for window in snapshot.windows:
        if is_design_window(window):
            continue
        period = quota_period(window)
        if period not in COMPARE_PERIODS:
            continue
        yield period, window


def _merge_observation(
    previous: QuotaCycle | None,
    *,
    product: str,
    account_key: str,
    period: str,
    cycle_start: float,
    used_percent: float,
    observed_at: float,
    resets_at: float,
    source: str,
) -> QuotaCycle | None:
    if previous is None:
        return QuotaCycle(
            product=product,
            account_key=account_key,
            period=period,
            cycle_start=cycle_start,
            max_used_percent=used_percent,
            first_observed_at=observed_at,
            last_observed_at=observed_at,
            resets_at=resets_at,
            sample_count=1,
            source=source,
            incomplete=cycle_is_incomplete(1, observed_at, resets_at),
            settled=SETTLED_PENDING,
            tokens=None,
            cost_usd=None,
        )
    # 中文注释：明显晚到的旧快照不能把末次观察时间往回拨，只允许抬高最高值。
    late = observed_at < previous.last_observed_at - 1.0
    if late and used_percent <= previous.max_used_percent:
        return None
    sample_count = previous.sample_count if late else previous.sample_count + 1
    last_observed = previous.last_observed_at if late else max(
        previous.last_observed_at, observed_at
    )
    first_observed = min(previous.first_observed_at, observed_at)
    current_reset = previous.resets_at if late else resets_at
    return QuotaCycle(
        product=product,
        account_key=account_key,
        period=period,
        cycle_start=cycle_start,
        max_used_percent=max(previous.max_used_percent, used_percent),
        first_observed_at=first_observed,
        last_observed_at=last_observed,
        resets_at=current_reset,
        sample_count=sample_count,
        source=_merge_source(previous.source, source),
        incomplete=cycle_is_incomplete(sample_count, last_observed, current_reset),
        settled=previous.settled,
        tokens=previous.tokens,
        cost_usd=previous.cost_usd,
    )


def _merge_source(previous: str, incoming: str) -> str:
    if incoming == SOURCE_LIVE:
        return SOURCE_LIVE
    if previous == SOURCE_LIVE:
        return SOURCE_LIVE
    return previous or incoming


def _cycle_from_row(row: sqlite3.Row) -> QuotaCycle:
    tokens = row["tokens"]
    cost = row["cost_usd"]
    resets_at = row["resets_at"]
    return QuotaCycle(
        product=str(row["product"]),
        account_key=str(row["account_key"]),
        period=str(row["period"]),
        cycle_start=float(row["cycle_start"]),
        max_used_percent=float(row["max_used_percent"]),
        first_observed_at=float(row["first_observed_at"]),
        last_observed_at=float(row["last_observed_at"]),
        resets_at=None if resets_at is None else float(resets_at),
        sample_count=int(row["sample_count"]),
        source=str(row["source"]),
        incomplete=bool(row["incomplete"]),
        settled=str(row["settled"]),
        tokens=None if tokens is None else int(tokens),
        cost_usd=None if cost is None else float(cost),
    )


def usage_window_bounds(cycle: QuotaCycle) -> tuple[float, float]:
    """返回结算时要统计用量的起止时间。"""

    nominal = _NOMINAL_WINDOW_SECONDS[cycle.period]
    if cycle.resets_at is not None:
        return cycle.resets_at - nominal, cycle.resets_at
    return cycle.cycle_start, cycle.last_observed_at


def sum_account_usage(
    index_path: Path,
    *,
    product: str,
    account_key: str,
    start: float,
    end: float,
) -> tuple[bool, int | None, float | None]:
    """按账号和时间段汇总 token 与 API 等价美元。

    返回 ``(索引是否就绪, tokens, cost)``。库文件还不存在时视为没就绪。
    周期结束得比索引里最早一条还早时，token 和美元留空，只保留额度百分比。
    """

    path = index_path.expanduser()
    if not path.is_file():
        return False, None, None
    try:
        store = _UsageIndexStore(path)
    except (OSError, sqlite3.DatabaseError):
        return False, None, None
    try:
        total_rows = store.count_deltas()
        if total_rows <= 0:
            return True, 0, 0.0
        if store.count_deltas_before(float(end) + 1e-6) <= 0:
            return True, None, None
        rows = store.search_deltas(
            since=float(start),
            until=float(end),
            account=account_key,
        )
    except (OSError, sqlite3.DatabaseError):
        return False, None, None
    finally:
        store.close()
    return True, *_totals_from_rows(rows, product=product, account_key=account_key)


def _totals_from_rows(
    rows: Sequence[object],
    *,
    product: str,
    account_key: str,
) -> tuple[int, float | None]:
    matched = []
    for row in rows:
        row_account = getattr(row, "account_key", None)
        row_product = getattr(row, "product", None)
        if row_account != account_key:
            continue
        if row_product not in (None, "", product):
            continue
        matched.append(row)
    total_paths = {
        row.path for row in matched if getattr(row, "kind", "") == "total"
    }
    tokens = 0
    cost = 0.0
    priced = False
    seen = False
    for row in matched:
        if row.kind != "total" and row.path in total_paths:
            continue
        delta = row.delta()
        if delta is None:
            continue
        seen = True
        usage = delta.billing_usage or delta.usage
        if not isinstance(usage, TokenUsage):
            continue
        tokens += int(usage.total_tokens)
        estimate = _estimate_usage(usage, delta.model, delta.timestamp)
        amount = estimate.get("estimated_cost_usd")
        if amount is not None:
            priced = True
            cost += float(amount)
    if not seen:
        return 0, 0.0
    return tokens, cost if priced else None


def settle_pending_cycles(
    store: QuotaCycleStore,
    index_path: Path | None,
    *,
    now: float | None = None,
) -> int:
    """给已经结束的周期补上 token 和美元。索引没就绪的留在待结算。"""

    pending = store.pending_cycles(now=now)
    if not pending or index_path is None:
        return 0
    done = 0
    for cycle in pending:
        start, end = usage_window_bounds(cycle)
        ready, tokens, cost = sum_account_usage(
            index_path,
            product=cycle.product,
            account_key=cycle.account_key,
            start=start,
            end=end,
        )
        if not ready:
            continue
        store.settle(
            cycle.product,
            cycle.account_key,
            cycle.period,
            cycle.cycle_start,
            tokens=tokens,
            cost_usd=cost,
            usage_ready=True,
        )
        done += 1
    return done


def _file_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return stat_result.st_ino, stat_result.st_mtime_ns, stat_result.st_size


def backfill_codex_cycles(
    store: QuotaCycleStore,
    sessions: Sequence[tuple[str, Path]],
    *,
    reader: JsonlSessionReader | None = None,
    now: float | None = None,
    stop: Callable[[], bool] | None = None,
) -> int:
    """从 Codex 会话 JSONL 的 rate_limits 增量回填周期，不走用量索引热路径。"""

    parser = reader or JsonlSessionReader()
    moment = time.time() if now is None else float(now)
    observed = 0
    for account_key, root in sessions:
        if stop is not None and stop():
            break
        root_path = Path(root)
        files = (root_path,) if root_path.is_file() else _jsonl_files(root_path)
        for path in files:
            if stop is not None and stop():
                break
            observed += _backfill_codex_file(
                store,
                parser,
                account_key,
                path,
                now=moment,
                stop=stop,
            )
    return observed


def _jsonl_files(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    try:
        return tuple(sorted(path for path in root.rglob("*.jsonl") if path.is_file()))
    except OSError:
        return ()


def _backfill_codex_file(
    store: QuotaCycleStore,
    reader: JsonlSessionReader,
    account_key: str,
    path: Path,
    *,
    now: float,
    stop: Callable[[], bool] | None,
) -> int:
    signature = _file_signature(path)
    if signature is None:
        return 0
    checkpoint = store.backfill_checkpoint(str(path))
    offset = 0
    if checkpoint is not None and checkpoint[:3] == signature:
        offset = checkpoint[3]
        if offset >= signature[2]:
            return 0
    elif checkpoint is not None and checkpoint[0] != signature[0]:
        offset = 0
    elif checkpoint is not None and checkpoint[2] > signature[2]:
        offset = 0
    elif checkpoint is not None:
        offset = min(checkpoint[3], signature[2])
    observed = 0
    while True:
        if stop is not None and stop():
            break
        tail = reader.read(
            path,
            offset=offset,
            now=now,
            maximum_bytes=_BACKFILL_CHUNK_BYTES,
        )
        for event in tail.events:
            if not event.observation.rate_limits:
                continue
            windows = tuple(
                QuotaWindow(
                    limit_id="codex",
                    name=window.name,
                    used_percent=window.used_percent,
                    window_minutes=window.window_minutes,
                    resets_at=window.reset_at,
                )
                for window in event.observation.rate_limits
            )
            if not windows:
                continue
            snapshot = QuotaSnapshot(
                observed_at=event.timestamp or now,
                windows=windows,
                source="session-jsonl",
            )
            store.observe(
                "codex",
                account_key,
                snapshot,
                source=SOURCE_BACKFILL_CODEX,
            )
            observed += 1
        previous = offset
        offset = tail.next_offset
        if offset <= previous or offset >= signature[2]:
            break
    store.save_backfill_checkpoint(
        str(path),
        product="codex",
        inode=signature[0],
        mtime_ns=signature[1],
        file_size=signature[2],
        next_offset=offset,
    )
    return observed


def backfill_grok_cycles(
    store: QuotaCycleStore,
    homes: Sequence[tuple[str, Path]],
    *,
    stop: Callable[[], bool] | None = None,
) -> int:
    """从 Grok unified.jsonl 的历史 billing 事件做稀疏回填。"""

    observed = 0
    for account_key, home in homes:
        if stop is not None and stop():
            break
        log_path = grok_unified_log(Path(home))
        signature = _file_signature(log_path)
        if signature is None:
            continue
        checkpoint = store.backfill_checkpoint(str(log_path))
        offset = 0
        if checkpoint is not None and checkpoint[:3] == signature and checkpoint[3] >= signature[2]:
            continue
        if checkpoint is not None and checkpoint[0] == signature[0] and checkpoint[2] <= signature[2]:
            offset = checkpoint[3]
        while True:
            if stop is not None and stop():
                break
            snapshots, next_offset = read_grok_billing_snapshots(
                home,
                offset=offset,
                maximum_bytes=_BACKFILL_CHUNK_BYTES,
            )
            for snapshot in snapshots:
                store.observe(
                    "grok",
                    account_key,
                    snapshot,
                    source=SOURCE_BACKFILL_GROK,
                )
                observed += 1
            if next_offset <= offset or next_offset >= signature[2]:
                offset = next_offset
                break
            offset = next_offset
        store.save_backfill_checkpoint(
            str(log_path),
            product="grok",
            inode=signature[0],
            mtime_ns=signature[1],
            file_size=signature[2],
            next_offset=offset,
        )
    return observed


_ReadQuota = Callable[..., QuotaSnapshot | None]
_HomesProvider = Callable[[], Mapping[str, Sequence[Path]]]
_SessionsProvider = Callable[[], Sequence[tuple[str, Path]]]


class QuotaSampler:
    """后台拉取 Claude / Grok 额度，并顺手结算、回填周期表。

    Codex 仍由监控主循环写入。这里不调用 ``clear_quota_caches``，读取函数
    自带的成功缓存和 429 退避继续生效。
    """

    def __init__(
        self,
        state_dir: Path,
        homes_provider: _HomesProvider,
        *,
        usage_index: Path | None = None,
        cycle_store: QuotaCycleStore | None = None,
        baseline_store: ProviderQuotaBaselineStore | None = None,
        read_claude: _ReadQuota | None = None,
        read_grok: _ReadQuota | None = None,
        codex_sessions: _SessionsProvider | None = None,
        grok_accounts: _SessionsProvider | None = None,
        logger: logging.Logger | None = None,
        claude_interval: float = CLAUDE_SAMPLE_INTERVAL,
        grok_interval: float = GROK_SAMPLE_INTERVAL,
        pre_reset_lead: float = PRE_RESET_LEAD_SECONDS,
        initial_delay: float = 1.0,
    ) -> None:
        self.state_dir = state_dir.expanduser()
        self._homes_provider = homes_provider
        self.usage_index = usage_index or (self.state_dir / "usage-index.sqlite3")
        self.cycle_store = cycle_store
        self.baseline_store = baseline_store
        self._read_claude = read_claude or read_claude_quota
        self._read_grok = read_grok or read_grok_quota
        self._codex_sessions = codex_sessions or (lambda: ())
        self._grok_accounts = grok_accounts
        self.logger = logger or _LOGGER
        self.claude_interval = claude_interval
        self.grok_interval = grok_interval
        self.pre_reset_lead = pre_reset_lead
        self.initial_delay = initial_delay
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._backfill_thread: threading.Thread | None = None
        self._pre_reset_done: set[tuple[str, str, str, float]] = set()

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """启动采样线程和一次增量回填线程。"""

        if self.is_running:
            return
        self._ensure_stores()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="a-token-monitor-quota-sampler",
            daemon=True,
        )
        self._backfill_thread = threading.Thread(
            target=self._backfill_loop,
            name="a-token-monitor-quota-backfill",
            daemon=True,
        )
        self._thread.start()
        self._backfill_thread.start()

    def close(self) -> None:
        """请求停止并等待采样、回填线程退出。"""

        self._stop.set()
        for thread in (self._thread, self._backfill_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2)
        self._thread = None
        self._backfill_thread = None

    def run_once(self, now: float | None = None) -> None:
        """同步采一轮，供测试和临近重置时的加采复用。"""

        self._ensure_stores()
        moment = time.time() if now is None else float(now)
        self._sample_product("claude", moment)
        self._sample_product("grok", moment)
        self._settle(moment)

    def _ensure_stores(self) -> None:
        if self.cycle_store is None:
            self.cycle_store = QuotaCycleStore(self.state_dir)
        if self.baseline_store is None:
            try:
                self.baseline_store = ProviderQuotaBaselineStore(self.state_dir)
            except ProviderQuotaBaselineError:
                self.logger.warning("provider 今日额度基线库不可用，周期采样仍会继续")
                self.baseline_store = None

    def _run(self) -> None:
        if self._stop.wait(self.initial_delay):
            return
        next_claude = 0.0
        next_grok = 0.0
        while not self._stop.is_set():
            moment = time.time()
            try:
                if moment >= next_claude:
                    self._sample_product("claude", moment)
                    next_claude = moment + self.claude_interval
                if moment >= next_grok:
                    self._sample_product("grok", moment)
                    next_grok = moment + self.grok_interval
                self._sample_pre_reset(moment)
                self._settle(moment)
            except Exception as error:  # noqa: BLE001 - 采样失败不能关掉守护线程
                self.logger.warning("额度周期采样失败: %s", error)
            delay = min(15.0, max(1.0, min(next_claude, next_grok) - time.time()))
            if self._stop.wait(delay):
                return

    def _backfill_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.backfill_once()
            except Exception as error:  # noqa: BLE001 - 回填失败留到下一轮
                self.logger.warning("额度周期回填失败: %s", error)
            if self._stop.wait(BACKFILL_INTERVAL):
                return

    def backfill_once(self) -> None:
        """增量回填 Codex 会话和 Grok billing 日志。"""

        store = self.cycle_store
        if store is None:
            return
        if self._stop.is_set():
            return
        backfill_codex_cycles(
            store,
            tuple(self._codex_sessions()),
            stop=self._stop.is_set,
        )
        homes = self._grok_backfill_homes()
        backfill_grok_cycles(store, homes, stop=self._stop.is_set)
        self._settle(time.time())

    def _grok_backfill_homes(self) -> tuple[tuple[str, Path], ...]:
        if self._grok_accounts is not None:
            return tuple(self._grok_accounts())
        homes = self._homes().get("grok", ())
        found: list[tuple[str, Path]] = []
        for home in homes:
            try:
                account = read_grok_account(Path(home))
            except OSError:
                continue
            found.append((account.account_key, Path(home)))
        return tuple(found)

    def _homes(self) -> Mapping[str, Sequence[Path]]:
        try:
            return self._homes_provider() or {}
        except Exception as error:  # noqa: BLE001 - 目录探测失败时这一轮跳过
            self.logger.warning("读取额度采样目录失败: %s", error)
            return {}

    def _sample_pre_reset(self, now: float) -> None:
        store = self.cycle_store
        if store is None:
            return
        due: set[str] = set()
        try:
            open_cycles = store.latest_open(now)
        except QuotaCycleError as error:
            self.logger.warning("读取临近重置的额度周期失败: %s", error)
            return
        for cycle in open_cycles:
            if cycle.resets_at is None:
                continue
            remaining = cycle.resets_at - now
            if not 0 < remaining <= self.pre_reset_lead:
                continue
            key = (cycle.product, cycle.account_key, cycle.period, cycle.cycle_start)
            if key in self._pre_reset_done:
                continue
            self._pre_reset_done.add(key)
            if cycle.product in {"claude", "grok"}:
                due.add(cycle.product)
        for product in sorted(due):
            self._sample_product(product, now)

    def _sample_product(self, product: str, now: float) -> None:
        homes = tuple(self._homes().get(product, ()))
        if not homes:
            return
        reader = self._read_claude if product == "claude" else self._read_grok
        account_reader = read_claude_account if product == "claude" else read_grok_account
        store = self.cycle_store
        if store is None:
            return
        for home in homes:
            if self._stop.is_set():
                return
            try:
                account = account_reader(Path(home))
                snapshot = reader(Path(home), now=now)
            except Exception as error:  # noqa: BLE001 - 单个目录失败不影响其他账号
                self.logger.warning("%s 额度采样失败: %s", product, error)
                continue
            if snapshot is None:
                continue
            if self.baseline_store is not None:
                try:
                    self.baseline_store.observe(product, account.account_key, snapshot)
                except ProviderQuotaBaselineError as error:
                    self.logger.warning("%s 今日额度基线写入失败: %s", product, error)
            try:
                store.observe(product, account.account_key, snapshot, source=SOURCE_LIVE)
            except QuotaCycleError as error:
                self.logger.warning("%s 额度周期写入失败: %s", product, error)

    def _settle(self, now: float) -> None:
        store = self.cycle_store
        if store is None:
            return
        try:
            settle_pending_cycles(store, self.usage_index, now=now)
        except (QuotaCycleError, OSError) as error:
            self.logger.warning("额度周期结算失败: %s", error)


def record_codex_quota(
    store: QuotaCycleStore | None,
    account_key: str,
    snapshot: QuotaSnapshot | None,
    *,
    usage_index: Path | None = None,
    now: float | None = None,
) -> None:
    """监控主循环保存 Codex 额度时同步写入周期表。"""

    if store is None or snapshot is None or not account_key.strip():
        return
    try:
        store.observe("codex", account_key, snapshot, source=SOURCE_LIVE)
        if usage_index is not None:
            settle_pending_cycles(store, usage_index, now=now)
    except QuotaCycleError:
        _LOGGER.warning("Codex 额度周期写入失败", exc_info=True)
