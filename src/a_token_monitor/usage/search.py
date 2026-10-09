"""用量检索：SQLite 条件拼装、分组聚合与分页。"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from ..agents import product_label
from .aggregates import (
    _ModelCost,
)
from .pricing import (
    _estimate_usage,
    _estimate_with_pricing,
    _lookup_pricing,
    _unknown_pricing_estimate,
)
from .records import (
    SessionSwitchThresholds,
    SessionUsage,
    TokenUsage,
    UsageDelta,
    _IndexRow,
    _UNKNOWN_MODEL,
    _round_number,
    _state_from_json,
    _text_value,
    session_id_from_path,
)

if TYPE_CHECKING:
    from .aggregator import UsageAggregator


# 中文注释：用量检索分组和分页边界。单次检索最多扫描的原始记录数用于防止
# 「全部时间」条件下把整份索引读进内存，命中上限时明确返回 truncated。
_USAGE_SEARCH_GROUPS = ("session", "date", "model", "account")


_USAGE_SEARCH_SORTS = ("recent", "tokens", "cost")


# 中文注释：账号身份按文件落盘在 usage_file_account；子查询里把 path 改名，
# 这样 JOIN 到 usage_delta 时不会出现同名列歧义。
_FILE_ACCOUNT_VIEW = (
    "SELECT path AS account_path, account_key, account_name, account_id, product "
    "FROM usage_file_account"
)


# 中文注释：聚合行里最多展示几个账号 / 产品标签，避免分组结果被标签撑爆。
_MAX_SEARCH_ACCOUNT_TAGS = 8


_UNKNOWN_ACCOUNT = "未知账号"


_DEFAULT_SEARCH_LIMIT = 50


_MAX_SEARCH_LIMIT = 500


_MAX_SEARCH_ROWS = 200_000


_MAX_SEARCH_KEYWORD_BYTES = 200


_FACETS_CACHE_SECONDS = 60.0


# 中文注释：相同筛选条件的检索结果缓存 30 秒，避免来回切换筛选时重复聚合。
_SEARCH_CACHE_SECONDS = 30.0


_SEARCH_CACHE_MAX = 24


# 中文注释：命令行和 Dashboard 默认只检索最近 30 天，0 表示不限时间。
DEFAULT_SEARCH_DAYS = 30


def search_since_days(days: float, now: float | None = None) -> float:
    """把「近 N 天」换算成按分钟对齐的起始时间。

    对齐到分钟是为了让相同的筛选条件落到同一个检索缓存键上：如果直接用
    ``time.time() - days * 86400``，每次请求的起始时间都差几秒，缓存永远不命中。
    """

    observed_at = time.time() if now is None else float(now)
    aligned = math.floor(observed_at / 60.0) * 60.0
    return aligned - float(days) * 86_400.0


def _search_pattern(value: str | None) -> str | None:
    """把检索关键词转义成 LIKE 模式；空关键词返回 None。"""

    if value is None:
        return None
    text = str(value).strip()[:_MAX_SEARCH_KEYWORD_BYTES]
    if not text:
        return None
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def enrich_session_views(
    views: Sequence[dict[str, Any]],
    aggregator: "UsageAggregator | None",
    thresholds: "SessionSwitchThresholds | None" = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """给统一会话视图补充 token、上下文、轮数与切换新会话的提醒。

    Dashboard 和命令行共用这一个入口：视图里的 ``jsonl_path`` 是唯一取数依据，
    读取失败时静默返回原视图，不影响其他功能。返回 ``(视图列表, 提醒列表)``。
    """

    views_out = list(views)
    if aggregator is None:
        return views_out, []
    resolved = thresholds or SessionSwitchThresholds()
    paths = [
        str(view.get("jsonl_path"))
        for view in views_out
        if view.get("jsonl_path")
    ]
    if not paths:
        return views_out, []
    try:
        usages = aggregator.session_usages(paths)
    except (OSError, ValueError):
        return views_out, []
    reminders: list[dict[str, Any]] = []
    for view in views_out:
        path = str(view.get("jsonl_path") or "")
        if not path:
            continue
        usage = usages.get(path)
        if usage is None:
            continue
        payload = usage.to_dict()
        reminder = usage.reminder(resolved)
        payload["reminder"] = reminder
        view["usage"] = payload
        # 中文注释：统一模型的 token 字段由用量索引回填，discovery 阶段为 0。
        view["tokens"] = payload["total_tokens"]
        view["context_tokens"] = payload["context_tokens"]
        view["turns"] = payload["turns"]
        if not view.get("model"):
            view["model"] = payload["model"]
        if reminder is None:
            continue
        reminders.append(
            {
                **reminder,
                "thread_id": view.get("thread_id"),
                "account": view.get("account"),
                "product": view.get("product"),
                "cwd": view.get("cwd"),
                "project": view.get("project"),
            }
        )
    reminders.sort(key=lambda item: -int(item.get("context_tokens") or 0))
    return views_out, reminders


def _session_usage_from_rows(
    path: str,
    rows: Sequence[_IndexRow],
) -> SessionUsage | None:
    """把索引行折叠成一个会话的用量汇总。"""

    total_rows = [row for row in rows if row.kind == "total"]
    selected = total_rows or [row for row in rows if row.kind == "fallback"]
    deltas: list[UsageDelta] = []
    for row in selected:
        delta = row.delta()
        if delta is not None:
            deltas.append(delta)
    return _session_usage_from_deltas(path, tuple(deltas))


def _session_usage_from_deltas(
    path: str,
    deltas: Sequence[UsageDelta],
) -> SessionUsage | None:
    """把会话的 token 增量折叠成轮数、上下文和成本。"""

    if not deltas:
        return None
    totals = TokenUsage()
    cost = _ModelCost()
    for delta in deltas:
        totals = totals.add(delta.usage)
        cost.add(_estimate_usage(delta.billing_usage or delta.usage, delta.model))
    last = max(deltas, key=lambda item: item.timestamp)
    return SessionUsage(
        path=path,
        turns=len(deltas),
        total_tokens=totals.total_tokens,
        # 中文注释：一次增量的 input_tokens 就是该轮请求的上下文规模，
        # 取最近一轮作为「当前上下文」的近似值。
        context_tokens=last.usage.input_tokens,
        model=last.model,
        first_at=min(item.timestamp for item in deltas),
        last_at=last.timestamp,
        estimated_cost_usd=(
            _round_number(cost.estimated_cost_usd)
            if cost.estimated_cost_usd is not None
            else None
        ),
        api_pricing_known=cost.api_pricing_known,
    )


def _file_projects(connection: sqlite3.Connection) -> dict[str, str]:
    """读取每个 JSONL 文件解析出的 session 工作目录。"""

    try:
        rows = connection.execute(
            "SELECT path, state_json FROM usage_file_state"
        ).fetchall()
    except sqlite3.DatabaseError:
        return {}
    projects: dict[str, str] = {}
    for path, state_json in rows:
        if not isinstance(state_json, str):
            continue
        try:
            state = _state_from_json(state_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if state is not None and state.project:
            projects[str(path)] = state.project
    return projects


def _matching_paths(pattern: str, projects: Mapping[str, str]) -> list[str]:
    """返回项目路径匹配关键词的文件，用于按项目筛选记录。"""

    matches = [
        path
        for path, project in projects.items()
        if _matches_pattern(project, pattern)
    ]
    # 中文注释：极端情况下避免生成过长的 IN 子句。
    return matches[:500]


def _matches_pattern(value: str, pattern: str) -> bool:
    """按 LIKE 转义规则做一次大小写不敏感匹配。"""

    needle = pattern.strip("%").replace("\\%", "%").replace("\\_", "_")
    return needle.lower() in value.lower()


def _project_clause(
    pattern: str,
    projects: Mapping[str, str],
) -> tuple[str, list[Any]]:
    """生成项目筛选子句：记录自带项目或文件级项目命中都算匹配。"""

    paths = _matching_paths(pattern, projects)
    clause = "COALESCE(project, '') LIKE ? ESCAPE '\\'"
    parameters: list[Any] = [pattern]
    if paths:
        placeholders = ", ".join("?" for _ in paths)
        clause = f"({clause} OR path IN ({placeholders}))"
        parameters.extend(paths)
    return clause, parameters


def _keyword_clause(
    pattern: str,
    projects: Mapping[str, str],
) -> tuple[str, list[Any]]:
    """生成关键词子句：匹配会话路径、模型、项目或文件级项目。"""

    paths = _matching_paths(pattern, projects)
    clause = (
        "(path LIKE ? ESCAPE '\\' OR COALESCE(project, '') LIKE ? ESCAPE '\\' "
        "OR model LIKE ? ESCAPE '\\'"
    )
    parameters: list[Any] = [pattern, pattern, pattern]
    if paths:
        placeholders = ", ".join("?" for _ in paths)
        clause += f" OR path IN ({placeholders})"
        parameters.extend(paths)
    return f"{clause})", parameters


def _account_clause(pattern: str) -> str:
    """生成账号筛选子句：账号键、账号名、账号 ID 或产品命中都算匹配。"""

    return (
        "path IN (SELECT account_path FROM "
        f"({_FILE_ACCOUNT_VIEW}) WHERE account_key LIKE ? ESCAPE '\\' "
        "OR account_name LIKE ? ESCAPE '\\' "
        "OR COALESCE(account_id, '') LIKE ? ESCAPE '\\' "
        "OR COALESCE(product, '') LIKE ? ESCAPE '\\')"
    )


def _search_bucket_key(
    group: str,
    date_key: str,
    path: str,
    model: str,
    account_key: str | None = None,
) -> tuple[Any, ...]:
    """返回一个检索分组的键：按会话、按日期、按模型或按账号。"""

    if group == "date":
        return (date_key,)
    if group == "model":
        return (model,)
    if group == "account":
        return (account_key or "",)
    return (date_key, path, model)


def _new_search_bucket(
    group: str,
    key: tuple[Any, ...],
    day: str,
    path: str,
    model: str,
    project: str | None,
    timestamp: float,
    account_key: str | None = None,
    account_name: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """创建一个检索分组（会话明细 / 按日期 / 按模型 / 按账号共用）。"""

    return {
        "key": "|".join(str(part) for part in key),
        "date": day,
        "session_id": session_id_from_path(Path(path)) if group == "session" else None,
        "session_path": path if group == "session" else None,
        "model": model if group == "session" else None,
        "models": {},
        "project": project,
        "account": (
            (account_name or account_key or _UNKNOWN_ACCOUNT)
            if group in ("session", "account")
            else None
        ),
        "account_id": account_id if group == "account" else None,
        "account_key": account_key if group == "account" else None,
        "accounts": {},
        "products": {},
        "usage": TokenUsage(),
        "cost": _ModelCost(),
        "records": 0,
        "first_at": timestamp,
        "last_at": timestamp,
    }


def _accumulate_search_bucket(
    bucket: dict[str, Any],
    *,
    usage: TokenUsage,
    estimate: Mapping[str, Any],
    model: str,
    first_at: float,
    last_at: float,
    records: int,
    project: str | None,
    account_name: str | None = None,
    product: str | None = None,
) -> None:
    """把一批 token 与成本累加进检索分组。"""

    bucket["usage"] = bucket["usage"].add(usage)
    bucket["cost"].add(estimate)
    bucket["models"][model] = bucket["models"].get(model, 0) + usage.total_tokens
    bucket["records"] += records
    bucket["first_at"] = min(bucket["first_at"], first_at)
    bucket["last_at"] = max(bucket["last_at"], last_at)
    if bucket["project"] is None:
        bucket["project"] = project
    # 中文注释：按日期 / 按模型分组会合并多个账号，这里记录贡献最多的账号，
    # 供面板显示账号标签；单个账号的分组标签在创建时就已经确定。
    if account_name:
        bucket["accounts"][account_name] = (
            bucket["accounts"].get(account_name, 0) + usage.total_tokens
        )
    if product:
        bucket["products"][product] = bucket["products"].get(product, 0) + 1


def _token_usage_from_row(row: Mapping[str, Any], prefix: str = "") -> TokenUsage:
    """从 SQL 聚合行读取 token 求和字段。"""

    return TokenUsage(
        input_tokens=int(row.get(f"{prefix}input_tokens") or 0),
        cached_input_tokens=int(row.get(f"{prefix}cached_input_tokens") or 0),
        cache_write_input_tokens=int(row.get(f"{prefix}cache_write_input_tokens") or 0),
        output_tokens=int(row.get(f"{prefix}output_tokens") or 0),
        reasoning_output_tokens=int(row.get(f"{prefix}reasoning_output_tokens") or 0),
        total_tokens=int(row.get(f"{prefix}total_tokens") or 0),
    )


def _search_row_to_dict(bucket: Mapping[str, Any]) -> dict[str, Any]:
    """把一个检索分组转换为 Dashboard / CLI 安全输出。"""

    usage: TokenUsage = bucket["usage"]
    cost: _ModelCost = bucket["cost"]
    models = [
        name
        for name, _ in sorted(
            bucket["models"].items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    payload: dict[str, Any] = {
        "key": bucket["key"],
        "date": bucket["date"],
        "session_id": bucket["session_id"],
        "session_path": bucket["session_path"],
        "model": bucket["model"],
        "models": models[:8],
        "project": bucket["project"],
        "account": bucket.get("account"),
        "account_id": bucket.get("account_id"),
        "account_key": bucket.get("account_key"),
        "accounts": _top_tags(bucket.get("accounts")),
        "products": [
            product_label(item) for item in _top_tags(bucket.get("products"))
        ],
        "usage": usage.to_dict(),
        "total_tokens": usage.total_tokens,
        "records": bucket["records"],
        "first_at": bucket["first_at"],
        "last_at": bucket["last_at"],
    }
    payload.update(cost.to_dict())
    return payload


def _top_tags(values: Mapping[str, int] | None) -> list[str]:
    """把「标签 → 权重」映射整理成按权重倒序、最多 N 个的标签列表。"""

    if not values:
        return []
    ordered = sorted(values.items(), key=lambda item: (-item[1], item[0]))
    return [name for name, _ in ordered[:_MAX_SEARCH_ACCOUNT_TAGS]]


def _account_fields(
    account_key: object,
    account_name: object,
    account_id: object,
    product: object,
) -> tuple[str | None, str | None, str | None, str | None]:
    """把索引里的账号列整理成（键、显示名、账号 ID、产品）四元组。"""

    key = _text_value(account_key)
    name = _text_value(account_name) or key
    return key, name, _text_value(account_id), _text_value(product)


def _search_sort_key(sort: str) -> Any:
    """返回检索结果的排序键：最近、token 总量或金额。"""

    if sort == "tokens":
        return lambda row: (-int(row["total_tokens"]), -float(row["last_at"]))
    if sort == "cost":
        return lambda row: (
            row["estimated_cost_usd"] is None,
            -float(row["estimated_cost_usd"] or 0.0),
            -float(row["last_at"]),
        )
    return lambda row: (-float(row["last_at"]), -int(row["total_tokens"]))


def _empty_search_totals() -> dict[str, Any]:
    """返回空检索的合计结构。"""

    return {
        "usage": TokenUsage().to_dict(),
        "total_tokens": 0,
        "cost_usd": None,
        "api_pricing_known": True,
        "cache_savings_usd": None,
        "rows": 0,
        "records": 0,
        "sessions": 0,
        "models": 0,
        "first_at": None,
        "last_at": None,
    }


def _empty_search_result(
    group: str = "session",
    sort: str = "recent",
    limit: int = _DEFAULT_SEARCH_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """返回没有用量索引时仍可被 Dashboard 使用的空检索结构。"""

    return {
        "available": False,
        "group": group,
        "sort": sort,
        "limit": limit,
        "offset": offset,
        "matched_rows": 0,
        "has_more": False,
        "truncated": False,
        "scanned_records": 0,
        "rows": [],
        "totals": _empty_search_totals(),
    }


class _SqlSearchBucket:
    """SQLite 聚合器：沿用现有计价和标签规则，只保留当前分组的摘要。"""

    def __init__(self) -> None:
        self.bucket: dict[str, Any] | None = None

    def step(self, payload: str, group: str) -> None:
        """合并一个按日期、会话、模型及长上下文分好的 SQL 桶。"""

        row = json.loads(payload)
        path, model, day = (
            str(row["path"]),
            str(row["model"] or _UNKNOWN_MODEL),
            str(row["day"] or ""),
        )
        key, name, account_id, product = _account_fields(
            row.get("account_key"),
            row.get("account_name"),
            row.get("account_id"),
            row.get("product"),
        )
        usage = _token_usage_from_row(row)
        billing = _token_usage_from_row(row, "billing_")
        pricing = _lookup_pricing(model)
        estimate = (
            _estimate_with_pricing(billing, pricing, bool(row["long_context"]))
            if pricing is not None
            else _unknown_pricing_estimate()
        )
        if self.bucket is None:
            self.bucket = _new_search_bucket(
                group,
                _search_bucket_key(group, day, path, model, key),
                day,
                path,
                model,
                row.get("project"),
                float(row["first_at"]),
                account_key=key,
                account_name=name,
                account_id=account_id,
            )
        _accumulate_search_bucket(
            self.bucket,
            usage=usage,
            estimate=estimate,
            model=model,
            first_at=float(row["first_at"]),
            last_at=float(row["last_at"]),
            records=int(row["records"]),
            project=row.get("project"),
            account_name=name,
            product=product,
        )

    def finalize(self) -> str | None:
        """只序列化最终摘要，不向请求线程传回全部 SQL 分桶。"""

        return (
            json.dumps(_search_row_to_dict(self.bucket))
            if self.bucket is not None
            else None
        )


def _sql_search_page(
    connection: sqlite3.Connection,
    statement: str,
    parameters: Sequence[Any],
    projects: Mapping[str, str],
    page: tuple[str, str, int, int],
) -> dict[str, Any]:
    """用临时表复用筛选结果，分别查询完整合计和带 LIMIT/OFFSET 的页面。"""

    group, sort, limit, offset = page
    # 中文注释：大检索的中间结果由 SQLite 管理，允许落临时文件，避免 Python fetchall。
    connection.execute("PRAGMA temp_store=FILE")
    connection.create_function("usage_project", 1, projects.get)
    connection.create_aggregate("usage_search_bucket", 2, _SqlSearchBucket)
    connection.execute(
        "CREATE TEMP TABLE search_base AS SELECT *, usage_project(path) AS project "
        f"FROM ({statement})",
        parameters,
    )
    columns = [
        item[0]
        for item in connection.execute("SELECT * FROM search_base LIMIT 0").description
    ]
    row_json = "json_object(" + ",".join(f"'{name}', {name}" for name in columns) + ")"
    grouping = {
        "session": "day, path, model",
        "date": "day",
        "model": "model",
        "account": "COALESCE(account_key, '')",
    }[group]
    connection.execute(
        "CREATE TEMP TABLE search_buckets AS "
        f"SELECT usage_search_bucket({row_json}, ?) AS payload FROM search_base "
        f"GROUP BY {grouping} ORDER BY MIN(rowid)",
        (group,),
    )
    summary = connection.execute(
        f"SELECT usage_search_bucket({row_json}, 'all'), SUM(records), "
        "COUNT(DISTINCT path), COUNT(DISTINCT model), MIN(first_at), MAX(last_at) "
        "FROM search_base"
    ).fetchone()
    matched = int(
        connection.execute("SELECT COUNT(*) FROM search_buckets").fetchone()[0]
    )
    order = {
        "recent": "json_extract(payload, '$.last_at') DESC, json_extract(payload, '$.total_tokens') DESC",
        "tokens": "json_extract(payload, '$.total_tokens') DESC, json_extract(payload, '$.last_at') DESC",
        "cost": "json_extract(payload, '$.estimated_cost_usd') IS NULL, json_extract(payload, '$.estimated_cost_usd') DESC, json_extract(payload, '$.last_at') DESC",
    }[sort]
    rows = connection.execute(
        f"SELECT payload FROM search_buckets ORDER BY {order}, rowid LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    totals = _empty_search_totals()
    if summary[0] is not None:
        combined = json.loads(summary[0])
        totals.update(
            usage=combined["usage"],
            total_tokens=combined["total_tokens"],
            cost_usd=combined["estimated_cost_usd"],
            api_pricing_known=combined["api_pricing_known"],
            cache_savings_usd=combined["cache_savings_usd"],
            records=int(summary[1]),
            sessions=int(summary[2]),
            models=int(summary[3]),
            first_at=summary[4],
            last_at=summary[5],
        )
    totals["rows"] = matched
    return {
        "available": True,
        "group": group,
        "sort": sort,
        "limit": limit,
        "offset": offset,
        "matched_rows": matched,
        "has_more": offset + len(rows) < matched,
        "truncated": False,
        "scanned_records": totals["records"],
        "rows": [json.loads(row[0]) for row in rows],
        "totals": totals,
    }
