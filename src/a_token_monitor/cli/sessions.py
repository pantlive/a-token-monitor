"""sessions 子命令：列出活动会话，附带用量与切换新会话提醒。"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Mapping

from ..accounts import CodexAccount
from ..multi_account import MultiAccountMonitor
from ..monitor import MonitorConfig
from ..multi_models import (
    DetectionConfidence,
    SessionStatus,
    TrackedSession,
    session_view,
)
from ..providers import home_providers
from ..usage import (
    SessionSwitchThresholds,
    UsageAggregator,
    enrich_session_views,
)
from .common import (
    _accounts,
    _dump_json,
    _effective_scan_dirs,
    _format_count,
    _guard_provider,
)
from .disk import (
    _session_housekeeping,
)


def _show_sessions(args: argparse.Namespace) -> int:
    """发现并列出活动会话，或执行会话归档/清理/恢复。"""

    if args.restore is not None or args.archive or args.clean:
        return _session_housekeeping(args)
    accounts = _accounts(args)
    effective_dirs = _effective_scan_dirs(args)
    monitor = MultiAccountMonitor(
        accounts=accounts,
        state_dir=args.state_dir,
        config=MonitorConfig(
            codex_path=args.codex,
            auto_resume=False,
            session_turn_warn=args.session_turn_warn,
            session_context_warn_tokens=args.session_context_warn_tokens,
        ),
        homes=effective_dirs.provider_homes(("grok", "kimi", "dsh", "commandcode", "claude")),
    )
    try:
        monitor.start()
        monitor.run_once()
    finally:
        monitor.close()

    sessions_by_account: list[tuple[CodexAccount, list[TrackedSession]]] = []
    for item in monitor.account_monitors:
        sessions = item.registry.list_sessions(active_only=not args.all)
        if not args.all:
            sessions = [
                session
                for session in sessions
                if session.pids
                or (
                    session.confidence == DetectionConfidence.APP_SERVER
                    and session.status
                    in {
                        SessionStatus.RUNNING,
                        SessionStatus.WAITING_FOR_APPROVAL,
                    }
                )
            ]
        sessions_by_account.append((item.account, sessions))

    sessions = [
        session
        for _, account_sessions in sessions_by_account
        for session in account_sessions
    ]
    summaries = [
        session_view(session, account.account_id or account.name)
        for account, account_sessions in sessions_by_account
        for session in account_sessions
    ]
    extra_sessions: list[tuple[str, TrackedSession]] = []
    for spec in home_providers():
        if spec.read_account is None or spec.active_sessions is None:
            continue
        for home in effective_dirs.homes(spec.key):
            if not home.is_dir():
                continue
            try:
                provider_account = spec.read_account(home)
                for session in spec.active_sessions(home):
                    extra_sessions.append((provider_account.display_name, session))
                    summaries.append(
                        session_view(
                            session,
                            provider_account.display_name,
                            account_id=provider_account.account_id,
                            profile_name=provider_account.profile_name,
                            product=spec.product_id,
                        )
                    )
            except Exception:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
                _guard_provider(spec.display_name, home)

    thresholds = SessionSwitchThresholds(
        turn_warn=args.session_turn_warn,
        context_warn_tokens=args.session_context_warn_tokens,
    )
    summaries, reminders = _enrich_session_summaries(
        args,
        monitor,
        summaries,
        thresholds,
    )
    views_by_path = {
        str(item.get("jsonl_path") or ""): item
        for item in summaries
        if item.get("jsonl_path")
    }
    if args.json:
        sys.stdout.write(f"{_dump_json(summaries)}\n")
        return 0
    if not sessions and not extra_sessions:
        sys.stdout.write("没有发现活动会话。\n")
        return 0
    for account, account_sessions in sessions_by_account:
        for session in account_sessions:
            pids = ",".join(str(pid) for pid in session.pids) or "无"
            sys.stdout.write(
                f"{account.account_id or '未识别'} ({account.name}) | "
                f"{session.session_id or session.thread_id} | "
                f"{session.status.value} | PID {pids} | "
                f"{session.cwd or '目录未知'}"
                f"{_session_usage_note(views_by_path.get(str(session.jsonl_path or '')))}\n"
            )
    for label, session in extra_sessions:
        pids = ",".join(str(pid) for pid in session.pids) or "无"
        sys.stdout.write(
            f"{label} | {session.session_id or session.thread_id} | "
            f"{session.status.value} | PID {pids} | "
            f"{session.cwd or '目录未知'}"
            f"{_session_usage_note(views_by_path.get(str(session.jsonl_path or '')))}\n"
        )
    for reminder in reminders:
        sys.stdout.write(f"[{reminder['level']}] {reminder['message']}\n")
    return 0


def _enrich_session_summaries(
    args: argparse.Namespace,
    monitor: MultiAccountMonitor,
    summaries: list[dict[str, object]],
    thresholds: SessionSwitchThresholds,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """用统一用量入口给会话视图补充 token、轮数与切换提醒。"""

    effective = _effective_scan_dirs(args)
    aggregator = UsageAggregator(
        cache_path=args.state_dir.expanduser() / "usage-index.sqlite3",
        homes=effective.provider_homes(),
        # 中文注释：一次性命令与 daemon 共享索引文件，不能按自己的范围清理。
        prune_stale=False,
    )
    try:
        aggregator.refresh_index(
            monitor.registries,
            monitor.dashboard_account_metadata,
        )
    except (OSError, ValueError):
        logging.getLogger(__name__).debug(
            "会话用量索引刷新失败，跳过轮数与上下文提示",
            exc_info=True,
        )
    return enrich_session_views(summaries, aggregator, thresholds)


def _session_usage_note(view: object) -> str:
    """返回附在会话行尾的轮数/上下文说明（读取统一视图的 usage 字段）。"""

    if not isinstance(view, Mapping):
        return ""
    usage = view.get("usage")
    if not isinstance(usage, Mapping):
        return ""
    note = (
        f" | {usage.get('turns', 0)} 轮"
        f" / 上下文 {_format_count(usage.get('context_tokens'))} token"
        f" / 累计 {_format_count(usage.get('total_tokens'))} token"
        f" | {usage.get('model') or '未知模型'}"
    )
    if usage.get("reminder"):
        note += " | ⚠ 建议开新会话"
    return note
