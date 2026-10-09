"""检索、索引范围与会话用量查询（``UsageAggregator`` 的查询部分）。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .aggregates import (
    _ModelCost,
)
from .periods import (
    _local_day,
)
from .pricing import (
    _estimate_usage,
)
from .records import (
    SessionUsage,
    TokenUsage,
    _IndexRow,
    _round_number,
)
from .search import (
    _DEFAULT_SEARCH_LIMIT,
    _FACETS_CACHE_SECONDS,
    _MAX_SEARCH_LIMIT,
    _MAX_SEARCH_ROWS,
    _SEARCH_CACHE_MAX,
    _SEARCH_CACHE_SECONDS,
    _USAGE_SEARCH_GROUPS,
    _USAGE_SEARCH_SORTS,
    _account_fields,
    _accumulate_search_bucket,
    _empty_search_result,
    _new_search_bucket,
    _search_bucket_key,
    _search_row_to_dict,
    _search_sort_key,
    _session_usage_from_deltas,
    _session_usage_from_rows,
)
from .store import (
    _UsageIndexStore,
)


class _UsageQueryMixin:
    """``UsageAggregator`` 的混入类；依赖其 ``__init__`` 建立的实例属性。"""

    @staticmethod
    def empty_search(
        group: str = "session",
        sort: str = "recent",
        limit: int = _DEFAULT_SEARCH_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """返回没有用量索引时仍可被 Dashboard 使用的空检索结构。"""

        return _empty_search_result(group=group, sort=sort, limit=limit, offset=offset)

    def usage_facets(self, now: float | None = None) -> dict[str, Any]:
        """返回索引整体范围，供检索页填充模型下拉和范围提示。"""

        observed_at = time.time() if now is None else float(now)
        with self._snapshot_lock:
            cached = self._facets_cache
            if (
                cached is not None
                and observed_at - self._facets_cached_at < _FACETS_CACHE_SECONDS
            ):
                return cached
        store = self._persistent
        facets = (
            store.facets()
            if store is not None
            else {
                "records": 0,
                "sessions": 0,
                "models": [],
                "accounts": [],
                "first_at": None,
                "last_at": None,
            }
        )
        facets["available"] = store is not None and facets["records"] > 0
        with self._snapshot_lock:
            self._facets_cache = facets
            self._facets_cached_at = observed_at
        return facets

    def search(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        models: Sequence[str] = (),
        session: str | None = None,
        project: str | None = None,
        account: str | None = None,
        keyword: str | None = None,
        group: str = "session",
        sort: str = "recent",
        limit: int = _DEFAULT_SEARCH_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """按日期、模型、账号和会话检索已落盘的 token 用量历史。

        优先走 SQL 聚合（把逐条用量留在 SQLite 里），只有 JSON1 不可用或旧索引
        还没补齐长上下文标记时才退回逐条扫描；相同筛选条件命中 30 秒缓存。
        """

        if group not in _USAGE_SEARCH_GROUPS:
            raise ValueError(f"未知分组方式: {group}")
        if sort not in _USAGE_SEARCH_SORTS:
            raise ValueError(f"未知排序方式: {sort}")
        if limit <= 0 or limit > _MAX_SEARCH_LIMIT:
            raise ValueError(f"limit 必须在 1 到 {_MAX_SEARCH_LIMIT} 之间")
        if offset < 0:
            raise ValueError("offset 不能小于 0")
        store = self._persistent
        if store is None:
            return self.empty_search(group=group, sort=sort, limit=limit, offset=offset)
        cache_key = (
            since,
            until,
            tuple(models),
            session,
            project,
            account,
            keyword,
            group,
            sort,
            limit,
            offset,
        )
        cached = self._cached_search(cache_key)
        if cached is not None:
            return cached
        if store.supports_json_aggregation() and not store.has_pending_long_context():
            result = self._search_grouped(
                store,
                since=since,
                until=until,
                models=models,
                session=session,
                project=project,
                account=account,
                keyword=keyword,
                group=group,
                sort=sort,
                limit=limit,
                offset=offset,
            )
        else:
            result = self._search_scan(
                store,
                since=since,
                until=until,
                models=models,
                session=session,
                project=project,
                account=account,
                keyword=keyword,
                group=group,
                sort=sort,
                limit=limit,
                offset=offset,
            )
        self._store_search_cache(cache_key, result)
        return result

    def _search_grouped(
        self,
        store: "_UsageIndexStore",
        *,
        since: float | None = None,
        until: float | None = None,
        models: Sequence[str] = (),
        session: str | None = None,
        project: str | None = None,
        account: str | None = None,
        keyword: str | None = None,
        group: str = "session",
        sort: str = "recent",
        limit: int = _DEFAULT_SEARCH_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """用 SQL 聚合结果拼装检索视图，成本按分桶的长上下文标记估算。"""

        return store.search_page(
            since=since, until=until, models=models, session=session, project=project,
            account=account, keyword=keyword, group=group, sort=sort,
            limit=limit, offset=offset,
        )

    def _search_payload(
        self,
        *,
        buckets: Mapping[tuple[Any, ...], dict[str, Any]],
        totals_usage: TokenUsage,
        totals_cost: _ModelCost,
        session_paths: set[str],
        model_names: set[str],
        scanned_records: int,
        first_at: float | None,
        last_at: float | None,
        truncated: bool,
        group: str,
        sort: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        """把分桶结果整理成 Dashboard / CLI 使用的返回结构。"""

        results = [_search_row_to_dict(bucket) for bucket in buckets.values()]
        results.sort(key=_search_sort_key(sort))
        page = results[offset : offset + limit]
        totals = {
            "usage": totals_usage.to_dict(),
            "total_tokens": totals_usage.total_tokens,
            "cost_usd": (
                _round_number(totals_cost.estimated_cost_usd)
                if totals_cost.estimated_cost_usd is not None
                else None
            ),
            "api_pricing_known": totals_cost.api_pricing_known,
            "cache_savings_usd": (
                _round_number(totals_cost.cache_savings_usd)
                if totals_cost.cache_savings_usd is not None
                else None
            ),
            "rows": len(results),
            "records": scanned_records,
            "sessions": len(session_paths),
            "models": len(model_names),
            "first_at": first_at,
            "last_at": last_at,
        }
        return {
            "available": True,
            "group": group,
            "sort": sort,
            "limit": limit,
            "offset": offset,
            "matched_rows": len(results),
            "has_more": offset + len(page) < len(results),
            "truncated": truncated,
            "scanned_records": scanned_records,
            "rows": page,
            "totals": totals,
        }

    def _cached_search(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        """读取检索结果缓存；过期条目直接丢弃。"""

        now = time.monotonic()
        with self._snapshot_lock:
            entry = self._search_cache.get(key)
            if entry is None:
                return None
            if now - entry[0] >= _SEARCH_CACHE_SECONDS:
                self._search_cache.pop(key, None)
                return None
            return entry[1]

    def _store_search_cache(
        self,
        key: tuple[Any, ...],
        payload: dict[str, Any],
    ) -> None:
        """写入检索结果缓存，超过上限时丢弃最旧的一半。"""

        with self._snapshot_lock:
            self._search_cache[key] = (time.monotonic(), payload)
            if len(self._search_cache) > _SEARCH_CACHE_MAX:
                for stale in sorted(
                    self._search_cache,
                    key=lambda item: self._search_cache[item][0],
                )[: len(self._search_cache) - _SEARCH_CACHE_MAX]:
                    self._search_cache.pop(stale, None)

    def _search_scan(
        self,
        store: "_UsageIndexStore",
        *,
        since: float | None = None,
        until: float | None = None,
        models: Sequence[str] = (),
        session: str | None = None,
        project: str | None = None,
        account: str | None = None,
        keyword: str | None = None,
        group: str = "session",
        sort: str = "recent",
        limit: int = _DEFAULT_SEARCH_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """逐条读取索引并聚合（JSON1 不可用时的回退路径）。"""

        rows = store.search_deltas(
            since=since,
            until=until,
            models=models,
            session=session,
            project=project,
            account=account,
            keyword=keyword,
            limit=_MAX_SEARCH_ROWS + 1,
        )
        truncated = len(rows) > _MAX_SEARCH_ROWS
        if truncated:
            rows = rows[:_MAX_SEARCH_ROWS]
        total_deltas = {row.path for row in rows if row.kind == "total"}
        buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
        totals_usage = TokenUsage()
        totals_cost = _ModelCost()
        session_paths: set[str] = set()
        model_names: set[str] = set()
        scanned_records = 0
        first_at: float | None = None
        last_at: float | None = None
        for row in rows:
            # 中文注释：一个文件同时有 total 和 fallback 时只用 total，
            # 与 _CachedFile.deltas 的取值规则保持一致，避免重复计数。
            if row.kind != "total" and row.path in total_deltas:
                continue
            delta = row.delta()
            if delta is None:
                continue
            scanned_records += 1
            estimate = _estimate_usage(delta.billing_usage or delta.usage, delta.model)
            date_key = _local_day(delta.timestamp)
            account_key, account_name, account_id, product = _account_fields(
                row.account_key,
                row.account_name,
                row.account_id,
                row.product,
            )
            key = _search_bucket_key(group, date_key, row.path, delta.model, account_key)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = _new_search_bucket(
                    group,
                    key,
                    date_key,
                    row.path,
                    delta.model,
                    row.project,
                    delta.timestamp,
                    account_key=account_key,
                    account_name=account_name,
                    account_id=account_id,
                )
                buckets[key] = bucket
            _accumulate_search_bucket(
                bucket,
                usage=delta.usage,
                estimate=estimate,
                model=delta.model,
                first_at=delta.timestamp,
                last_at=delta.timestamp,
                records=1,
                project=row.project,
                account_name=account_name,
                product=product,
            )
            totals_usage = totals_usage.add(delta.usage)
            totals_cost.add(estimate)
            session_paths.add(row.path)
            model_names.add(delta.model)
            first_at = (
                delta.timestamp if first_at is None else min(first_at, delta.timestamp)
            )
            last_at = (
                delta.timestamp if last_at is None else max(last_at, delta.timestamp)
            )

        return self._search_payload(
            buckets=buckets,
            totals_usage=totals_usage,
            totals_cost=totals_cost,
            session_paths=session_paths,
            model_names=model_names,
            scanned_records=scanned_records,
            first_at=first_at,
            last_at=last_at,
            truncated=truncated,
            group=group,
            sort=sort,
            limit=limit,
            offset=offset,
        )

    def session_usages(self, paths: Sequence[str]) -> dict[str, SessionUsage]:
        """按 JSONL 路径返回会话的轮数、上下文和累计用量。"""

        wanted = [str(path) for path in paths if path]
        if not wanted:
            return {}
        usages: dict[str, SessionUsage] = {}
        store = self._persistent
        if store is not None:
            rows = store.session_rows(wanted)
            grouped: dict[str, list[_IndexRow]] = {}
            for row in rows:
                grouped.setdefault(row.path, []).append(row)
            for path, items in grouped.items():
                summary = _session_usage_from_rows(path, items)
                if summary is not None:
                    usages[path] = summary
        for path in wanted:
            if path in usages:
                continue
            # 中文注释：只查询当前请求的会话，避免每五秒遍历全部文件缓存。
            cached = self._cache.get(Path(path))
            if cached is None:
                continue
            summary = _session_usage_from_deltas(path, cached.deltas)
            if summary is not None:
                usages[path] = summary
        return usages
