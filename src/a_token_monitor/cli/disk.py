"""disk 子命令：统计 agent 数据目录占用，预览和执行会话归档清理。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Callable, Sequence

from ..housekeeping import (
    DEFAULT_SINGLE_WARN_GIB,
    DEFAULT_TOTAL_WARN_GIB,
    AuditTarget,
    CleanupCriteria,
    DiskThresholds,
    HousekeepingError,
    HousekeepingMonitor,
    default_sessions_root,
)
from ..multi_account import MultiAccountMonitor, external_active_session_paths
from ..monitor import MonitorConfig
from ..providers import home_providers
from ..traffic import format_bytes
from .common import (
    _accounts,
    _dump_json,
    _effective_scan_dirs,
    _format_alert_time,
)


def _housekeeping_targets(args: argparse.Namespace) -> tuple[AuditTarget, ...]:
    """根据 CLI 参数收集需要统计占用的 agent 数据目录。"""

    targets: list[AuditTarget] = []
    for account in _accounts(args):
        targets.append(
            AuditTarget(
                label=f"Codex ({account.name})",
                product="codex",
                path=account.home,
                sessions_root=account.home / "sessions",
            )
        )
    effective_dirs = _effective_scan_dirs(args)
    for spec in home_providers():
        for home in effective_dirs.homes(spec.key):
            targets.append(
                AuditTarget(
                    spec.display_name,
                    spec.product_id,
                    home,
                    sessions_root=default_sessions_root(spec.product_id, home),
                )
            )
    targets.append(
        AuditTarget("监控状态目录", "state", args.state_dir.expanduser())
    )
    return tuple(targets)


def _housekeeping_monitor(args: argparse.Namespace) -> HousekeepingMonitor:
    """构造命令行使用的磁盘/会话管家。"""

    state_dir = args.state_dir.expanduser()
    return HousekeepingMonitor(
        targets=_housekeeping_targets(args),
        thresholds=DiskThresholds.from_gb(
            single_warn_gb=getattr(
                args,
                "disk_warn_gb",
                DEFAULT_SINGLE_WARN_GIB,
            ),
            total_warn_gb=getattr(
                args,
                "disk_total_warn_gb",
                DEFAULT_TOTAL_WARN_GIB,
            ),
        ),
        archive_dir=state_dir / "archives",
        active_paths=_active_session_paths(args),
        logger=logging.getLogger(__name__),
    )


def _show_disk(args: argparse.Namespace) -> int:
    """输出 agent 数据目录占用、提醒和可归档/清理的会话预览。"""

    monitor = _housekeeping_monitor(args)
    report = monitor.refresh(force=True)
    preview = monitor.preview(
        CleanupCriteria(older_than_days=args.days)
    )
    if args.json:
        sys.stdout.write(
            f"{_dump_json({'report': report, 'preview': preview})}\n"
        )
        return 0
    totals = report["totals"]
    sys.stdout.write(
        f"agent 数据目录合计 {format_bytes(totals['bytes'])} / "
        f"{totals['files']} 个文件"
        f"（会话文件 {format_bytes(totals['session_bytes'])} / "
        f"{totals['session_files']} 个）· 归档目录 {report['archive_dir']}\n"
    )
    for entry in report["directories"]:
        sys.stdout.write(
            f"{entry['label']} | {entry['path']} | "
            f"{format_bytes(entry['bytes'])} / {entry['files']} 个文件 | "
            f"会话 {format_bytes(entry['session_bytes'])} / "
            f"{entry['session_files']} 个"
            f"{'' if entry['cleanable'] else ' | 仅统计，不在此处清理'}\n"
        )
        for child in entry["top_children"][: args.top]:
            sys.stdout.write(
                f"    ↳ {child['name']} {format_bytes(child['bytes'])}\n"
            )
    if report["reminders"]:
        for reminder in report["reminders"]:
            sys.stdout.write(
                f"[{reminder['level']}] {reminder['title']}：{reminder['detail']}\n"
            )
    else:
        sys.stdout.write("磁盘占用未超过提醒阈值。\n")
    sys.stdout.write(
        f"预览：{preview['count']} 个超过 {args.days} 天的会话文件可归档或清理，"
        f"约 {format_bytes(preview['bytes'])}"
        f"（跳过活动会话 {preview['skipped_active']} 个、过新 "
        f"{preview['skipped_recent']} 个）。\n"
    )
    sys.stdout.write(
        "执行：a-token-monitor sessions --archive --older-than "
        f"{args.days} --yes 或 sessions --clean --older-than {args.days} --yes\n"
    )
    return 0


def _session_housekeeping(args: argparse.Namespace) -> int:
    """执行会话归档、清理或恢复，并保证默认只预览。"""

    monitor = _housekeeping_monitor(args)
    if args.restore is not None:
        result = monitor.restore(args.restore, destination=args.to)
        if args.json:
            sys.stdout.write(
                f"{_dump_json(result)}\n"
            )
        else:
            sys.stdout.write(
                f"已从 {result['archive']} 恢复 {result['restored']} 个会话文件到 "
                f"{result['destination']}。\n"
            )
        return 0
    criteria = CleanupCriteria(
        older_than_days=args.older_than,
        min_bytes=int(args.min_size_mb * 1024 * 1024),
        paths=_resolve_session_paths(monitor, args.session_keys),
    )
    if args.archive and args.clean:
        raise ValueError("--archive 和 --clean 不能同时使用")
    if not args.archive and not args.clean:
        preview = monitor.preview(criteria)
        if args.json:
            sys.stdout.write(
                f"{json.dumps(preview, ensure_ascii=False, indent=2)}\n"
            )
        else:
            _print_cleanup_preview(preview, args.older_than)
        return 0
    if not args.yes:
        preview = monitor.preview(criteria)
        if args.json:
            sys.stdout.write(
                f"{json.dumps(preview, ensure_ascii=False, indent=2)}\n"
            )
            return 0
        _print_cleanup_preview(preview, args.older_than)
        sys.stdout.write(
            "以上只是预览；确认后重新执行并加上 --yes 才会真正"
            f"{'归档' if args.archive else '删除'}。\n"
        )
        return 0
    if args.archive:
        result = monitor.archive(criteria, confirm=True)
    else:
        result = monitor.clean(criteria, confirm=True)
    if args.json:
        sys.stdout.write(f"{_dump_json(result)}\n")
        return 0
    if result["count"] == 0:
        sys.stdout.write("没有符合条件的会话文件，未做任何改动。\n")
        return 0
    if result["action"] == "archive":
        sys.stdout.write(
            f"已归档并删除 {result['deleted']} 个会话文件，"
            f"释放 {format_bytes(result['bytes'])}；归档 {result['archive']}，"
            f"manifest {result['manifest']}。\n"
        )
        sys.stdout.write(
            f"恢复：a-token-monitor sessions --restore {result['archive']}\n"
        )
    else:
        sys.stdout.write(
            f"已清理 {result['deleted']} 个会话文件，"
            f"释放 {format_bytes(result['bytes'])}。\n"
        )
    if result["failed"]:
        sys.stdout.write(f"{len(result['failed'])} 个文件删除失败。\n")
    return 0


def _resolve_session_paths(
    monitor: HousekeepingMonitor,
    keys: Sequence[str] | None,
) -> tuple[str, ...]:
    """把 --session 的会话 ID 或路径解析成可归档的 JSONL 路径。"""

    if not keys:
        return ()
    available = {item.session_id: str(item.path) for item in monitor.sessions()}
    by_path = {str(item.path) for item in monitor.sessions()}
    resolved: list[str] = []
    for key in keys:
        text = str(key).strip()
        if not text:
            continue
        candidate = str(Path(text).expanduser())
        if candidate in by_path:
            resolved.append(candidate)
            continue
        match = available.get(text) or available.get(Path(text).stem)
        if match is None:
            raise HousekeepingError(
                f"找不到会话 {text}；可用 a-token-monitor sessions --all 查看会话 ID"
            )
        resolved.append(match)
    return tuple(resolved)


def _print_cleanup_preview(preview: dict[str, object], days: int) -> None:
    """打印一次归档/清理预览。"""

    criteria = preview.get("criteria") or {}
    scope = (
        "指定的会话"
        if criteria.get("paths")
        else f"超过 {days} 天的会话文件"
    )
    sys.stdout.write(
        f"将处理 {preview['count']} 个{scope}，"
        f"约 {format_bytes(int(preview['bytes']))}"
        f"（跳过活动会话 {preview['skipped_active']} 个、过新 "
        f"{preview['skipped_recent']} 个、小于下限 {preview['skipped_small']} 个）。\n"
    )
    for item in preview.get("unmatched") or []:
        sys.stdout.write(f"  跳过无法匹配的会话：{item}\n")
    for item in list(preview.get("files") or [])[:20]:
        sys.stdout.write(
            f"  {_format_alert_time(float(item['modified_at']))} | "
            f"{item['session_id']} | {format_bytes(int(item['size']))} | "
            f"{item['path']}\n"
        )
    if preview.get("truncated"):
        sys.stdout.write("  …（仅显示前 20 个）\n")


def _active_session_paths(
    args: argparse.Namespace,
) -> "Callable[[], set[str]]":
    """返回读取活动会话 JSONL 路径的回调，用于保护正在运行的会话。"""

    def collect() -> set[str]:
        effective_dirs = _effective_scan_dirs(args)
        monitor = MultiAccountMonitor(
            accounts=_accounts(args),
            state_dir=args.state_dir,
            config=MonitorConfig(auto_resume=False),
            homes=effective_dirs.provider_homes(),
        )
        paths: set[str] = set()
        for item in monitor.account_monitors:
            for session in item.registry.list_sessions(active_only=True):
                if session.pids and session.jsonl_path:
                    paths.add(str(Path(session.jsonl_path)))
        paths.update(
            external_active_session_paths(
                homes=effective_dirs.provider_homes(),
            )
        )
        return paths

    return collect
