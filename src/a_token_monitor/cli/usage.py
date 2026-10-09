"""usage 子命令：按日期、模型、账号和会话检索用量索引。"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from ..local_time import local_date_end, local_date_start
from ..usage import (
    UsageAggregator,
    search_since_days,
)
from .common import (
    _dump_json,
    _effective_scan_dirs,
    _format_alert_time,
    _format_count,
)


def _usage_search_bounds(args: argparse.Namespace) -> tuple[float | None, float | None]:
    """把 --days / --from / --to 解析成检索时间范围。"""

    if args.date_from is not None or args.date_to is not None:
        return (
            _cli_day_start(args.date_from),
            _cli_day_end(args.date_to),
        )
    if args.days and args.days > 0:
        return search_since_days(args.days), None
    return None, None


def _cli_day_start(value: str | None) -> float | None:
    """把 YYYY-MM-DD 解析为本地当天零点时间戳。"""

    if value is None:
        return None
    try:
        day = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as error:
        raise ValueError(f"--from 需要 YYYY-MM-DD 格式: {value}") from error
    return local_date_start(day)


def _cli_day_end(value: str | None) -> float | None:
    """把 YYYY-MM-DD 解析为本地当天最后一刻的时间戳。"""

    if value is None:
        return None
    if _cli_day_start(value) is None:
        return None
    return local_date_end(datetime.strptime(value.strip(), "%Y-%m-%d").date())


def _show_usage_search(args: argparse.Namespace) -> int:
    """按日期、模型、账号和会话检索用量索引中的 token 历史记录。"""

    effective = _effective_scan_dirs(args)
    aggregator = UsageAggregator(
        cache_path=args.state_dir.expanduser() / "usage-index.sqlite3",
        homes=effective.provider_homes(),
        # 中文注释：一次性命令与 daemon 共享索引文件，不能按自己的范围清理。
        prune_stale=False,
    )
    since, until = _usage_search_bounds(args)
    search = aggregator.search(
        since=since,
        until=until,
        models=tuple(args.models or ()),
        session=args.session,
        project=args.project,
        account=args.account,
        keyword=args.query,
        group=args.group,
        sort=args.sort,
        limit=args.limit,
        offset=args.offset,
    )
    facets = aggregator.usage_facets()
    if args.json:
        sys.stdout.write(
            f"{_dump_json({'facets': facets, 'search': search})}\n"
        )
        return 0
    if not facets.get("available"):
        sys.stdout.write(
            "没有可检索的用量索引；先让 daemon 完成一次用量索引再查询。\n"
        )
        return 0
    totals = search.get("totals", {})
    sys.stdout.write(
        f"索引覆盖 {_format_alert_time(facets['first_at'])} ~ "
        f"{_format_alert_time(facets['last_at'])} · "
        f"{_format_count(facets['records'])} 条记录 / {facets['sessions']} 个会话。\n"
    )
    if search.get("available") is False:
        sys.stdout.write("没有可检索的用量索引。\n")
        return 0
    cost = totals.get("cost_usd")
    cost_note = (
        f"${cost:.4f}" if isinstance(cost, (int, float)) else "部分模型无单价"
    )
    sys.stdout.write(
        f"匹配 {search['matched_rows']} 行 / {_format_count(totals['records'])} 条记录 / "
        f"{totals['sessions']} 个会话 · 合计 {_format_count(totals['total_tokens'])} token · "
        f"API 等价 {cost_note}"
        f"{'（命中扫描上限，仅统计最近部分记录）' if search['truncated'] else ''}。\n"
    )
    if not search["rows"]:
        sys.stdout.write("没有符合条件的用量记录。\n")
        return 0
    for row in search["rows"]:
        usage = row.get("usage", {})
        row_cost = row.get("estimated_cost_usd")
        row_cost_text = (
            f"${row_cost:.4f}" if isinstance(row_cost, (int, float)) else "未计价"
        )
        if args.group == "date":
            label = f"{row['date']} | {row['records']} 条 / {len(row['models'])} 个模型"
        elif args.group == "model":
            label = f"{(row['models'] or ['未知模型'])[0]} | {row['records']} 条"
        elif args.group == "account":
            products = "、".join(row.get("products") or ()) or "未知产品"
            label = (
                f"{row.get('account') or '未知账号'} | {row['records']} 条 / "
                f"{len(row['models'])} 个模型 | {products}"
            )
        else:
            account_note = f" | {row['account']}" if row.get("account") else ""
            label = (
                f"{_format_alert_time(row['last_at'])} | "
                f"{(row['session_id'] or '未知会话')[:12]} | {row['model']} | "
                f"{row['project'] or '目录未知'}{account_note}"
            )
        sys.stdout.write(
            f"{label} | 输入 {_format_count(usage.get('input_tokens', 0))}"
            f"（缓存 {_format_count(usage.get('cached_input_tokens', 0))}） | "
            f"输出 {_format_count(usage.get('output_tokens', 0))} | "
            f"合计 {_format_count(row['total_tokens'])} | {row_cost_text} | "
            f"{row['records']} 条\n"
        )
    if search["has_more"]:
        sys.stdout.write(
            f"还有更多记录，使用 --offset {search['offset'] + search['limit']} 继续查看。\n"
        )
    return 0
