"""alerts 子命令：查询落盘告警、管理已读状态并展示告警上下文。"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Mapping

from ..i18n import (
    active_language,
    translate,
)
from ..agents import product_label
from ..alerts import (
    AlertQuery,
    StoredAlert,
    TrafficAlertStore,
)
from ..alert_context import configured_alert_context_roots, load_alert_context
from ..discovery import default_session_root
from ..traffic import format_bytes
from .common import (
    _dump_json,
    _effective_scan_dirs,
    _format_alert_time,
)


def _alert_query_from_args(args: argparse.Namespace) -> AlertQuery:
    """把 alerts 命令参数转换成存储层筛选条件。"""

    if args.unread and args.read:
        raise ValueError("--unread 和 --read 不能同时使用")
    since = args.since
    if args.days is not None:
        since = time.time() - args.days * 86400.0
    acknowledged: bool | None = None
    if args.unread:
        acknowledged = False
    elif args.read:
        acknowledged = True
    return AlertQuery(
        since=since,
        until=args.until,
        levels=(args.level,) if args.level else (),
        kinds=(args.kind,) if args.kind else (),
        products=(args.product,) if args.product else (),
        acknowledged=acknowledged,
        keyword=args.query,
        limit=args.limit,
        offset=args.offset,
    )


def _show_alerts(args: argparse.Namespace) -> int:
    """查询已落盘的历史异常流量告警，并支持已读与清理操作。"""

    store = TrafficAlertStore(
        args.state_dir,
        retention_days=args.retention_days,
    )
    if args.clear_all:
        if not args.yes:
            raise ValueError("--clear-all 会删除全部历史告警，请显式加上 --yes")
        removed = store.clear_all()
        sys.stdout.write(f"已删除全部 {removed} 条历史告警。\n")
        return 0
    if args.prune:
        if args.dry_run:
            cutoff = time.time() - args.retention_days * 86400.0
            pending = store.count_before(cutoff)
            sys.stdout.write(
                f"按保留 {args.retention_days:g} 天预览：将删除 {pending} 条过期告警。\n"
            )
            return 0
        removed = store.prune(force=True)
        sys.stdout.write(
            f"已按保留 {args.retention_days:g} 天清理 {removed} 条过期告警。\n"
        )
        return 0
    if args.clear_before is not None:
        cutoff = time.time() - args.clear_before * 86400.0
        if args.dry_run:
            pending = store.count_before(cutoff)
            sys.stdout.write(
                f"预览：将删除 {pending} 条早于近 {args.clear_before:g} 天的告警。\n"
            )
            return 0
        removed = store.clear_before(cutoff)
        sys.stdout.write(f"已删除 {removed} 条历史告警。\n")
        return 0
    if args.clear:
        removed = store.clear(args.clear)
        sys.stdout.write(f"已删除 {removed} 条历史告警。\n")
        return 0
    if args.ack_all:
        changed = store.acknowledge(all_alerts=True)
        sys.stdout.write(f"已把 {changed} 条告警标记为已读。\n")
        return 0
    if args.ack:
        changed = store.acknowledge(args.ack)
        sys.stdout.write(f"已把 {changed} 条告警标记为已读。\n")
        return 0

    query = _alert_query_from_args(args)
    alerts, has_more = store.query_page(query)
    stats = store.stats(since=query.since)
    if args.quiet:
        return 1 if stats["unread"] else 0
    include_content = bool(getattr(args, "alert_context_content", False))
    roots = _cli_alert_roots(args)
    if args.json:
        payload = {
            "stats": stats,
            "alerts": [
                _alert_with_context(
                    item,
                    include_content=include_content,
                    roots=roots,
                )
                for item in alerts
            ],
            "has_more": has_more,
        }
        sys.stdout.write(f"{_dump_json(payload)}\n")
        return 1 if stats["unread"] else 0
    sys.stdout.write(
        f"匹配 {stats['total']} 条告警，未读 {stats['unread']} 条"
        f"（danger {stats['danger']} / warn {stats['warn']}），"
        f"保留 {store.retention_days:g} 天。\n"
    )
    if args.stats:
        return 1 if stats["unread"] else 0
    if not alerts:
        sys.stdout.write("没有符合条件的历史告警。\n")
        return 0
    for alert in alerts:
        mark = " " if alert.acknowledged else "未读"
        remote = alert.remote or "无外连"
        cwd = alert.cwd or "目录未知"
        product = product_label(alert.product)
        sys.stdout.write(
            f"#{alert.id} {mark} {_format_alert_time(alert.last_seen_at)} "
            f"{alert.level} {alert.kind} | {product} pid {alert.pid} | "
            f"外发峰值 {format_bytes(alert.peak_bytes)} / "
            f"{alert.window_seconds:g}s | {cwd} | {remote}"
            f"{f' | 合并 {alert.count} 次' if alert.count > 1 else ''}\n"
        )
        _write_alert_context(
            alert,
            include_content=include_content,
            roots=roots,
        )
    if has_more:
        sys.stdout.write(
            f"还有更多记录，使用 --offset {query.offset + query.limit} 继续查看。\n"
        )
    return 1 if stats["unread"] else 0


_ALERT_CONTEXT_REASONS = {
    "no_session": "告警时间窗内没有匹配该工作目录的会话文件（可能已清理或归档）",
    "unsupported_product": "该 agent 的会话格式暂不支持明细提取",
    "unreadable": "会话文件无法读取",
}


def _cli_alert_roots(args: argparse.Namespace) -> object:
    """按生效扫描目录组装告警会话根，同一轮只解析一次。"""

    effective = _effective_scan_dirs(args)
    codex_roots = []
    for home in effective.homes("codex"):
        sessions = Path(home) / "sessions"
        if sessions.is_dir():
            codex_roots.append(sessions)
    if not codex_roots:
        fallback = default_session_root()
        if fallback.is_dir():
            codex_roots.append(fallback)
    return configured_alert_context_roots(
        codex_sessions=tuple(codex_roots),
        homes=effective.provider_homes(),
    )


def _load_cli_alert_context(
    alert: StoredAlert,
    roots: object,
    *,
    include_content: bool,
) -> dict[str, object]:
    """按本轮生效的会话目录读取告警当时的行为。"""

    return load_alert_context(
        alert,
        roots,  # type: ignore[arg-type]
        include_content=include_content,
    )


def _translated_summaries(summary: object) -> list[str]:
    """把每条行为摘要按当前语言整句翻译，不拆用户内容。"""

    if not isinstance(summary, list):
        return []
    language = active_language()
    return [translate(str(item), language) for item in summary]


def _alert_context_brief(
    context: Mapping[str, object], *, include_content: bool
) -> dict[str, object]:
    """默认只带行为摘要；内容摘要留给显式开关。"""

    brief: dict[str, object] = {
        "found": context["found"],
        "reason": context["reason"],
        "activity_summary": _translated_summaries(context["activity_summary"]),
        "fallback": context["fallback"],
    }
    if include_content:
        brief["events"] = context["events"]
    return brief


def _alert_with_context(
    alert: StoredAlert,
    *,
    include_content: bool,
    roots: object,
) -> dict[str, object]:
    body = alert.to_dict()
    context = _load_cli_alert_context(
        alert,
        roots,
        include_content=include_content,
    )
    body["context"] = _alert_context_brief(
        context, include_content=include_content
    )
    return body


def _write_alert_context(
    alert: StoredAlert,
    *,
    include_content: bool,
    roots: object,
) -> None:
    """在告警行下面补一行当时的行为；没有会话时说明原因。"""

    context = _load_cli_alert_context(
        alert,
        roots,
        include_content=include_content,
    )
    summary = _translated_summaries(context["activity_summary"])
    if summary:
        label = translate("行为：", active_language())
        sys.stdout.write(f"    {label}{' · '.join(summary)}\n")
    else:
        reason = _ALERT_CONTEXT_REASONS.get(str(context.get("reason") or ""))
        if reason:
            sys.stdout.write(f"    {reason}\n")
    if not include_content:
        return
    events = context.get("events")
    if not isinstance(events, list):
        return
    for event in events:
        if not isinstance(event, Mapping):
            continue
        detail = str(event.get("detail") or event.get("label") or "")
        if detail:
            sys.stdout.write(f"    {event.get('kind', '')} {detail}\n")
