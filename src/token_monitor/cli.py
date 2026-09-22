"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .accounts import CodexAccount, build_account_specs
from .grok import read_grok_account, read_grok_quota, resolve_grok_homes
from .dsh import (
    list_dsh_active_sessions,
    read_dsh_account,
    read_dsh_quota,
    resolve_dsh_homes,
)
from .kimi import (
    list_kimi_active_sessions,
    read_kimi_account,
    read_kimi_quota,
    resolve_kimi_homes,
)
from .app_server import AppServerClient, AppServerConfig, AppServerError
from .discovery import ProcessScanner
from .multi_account import MultiAccountMonitor
from .monitor import MonitorConfig
from .multi_models import DetectionConfidence, SessionStatus, TrackedSession
from .models import JobState
from .registry import MultiSessionRegistry, RegistryError
from .quota import QuotaSnapshot
from .quota_fallback import read_jsonl_quota, recent_session_paths
from .service import (
    ServiceConfig,
    ServiceError,
    UserServiceManager,
    resolve_executable,
    run_saved_service,
)
from .storage import StateError, StateStore
from .traffic import TrafficMonitor, TrafficThresholds, format_bytes


def default_state_dir() -> Path:
    """返回默认的本地状态目录。"""

    return Path.home() / ".codex-reset-monitor"


def _positive_float(value: str) -> float:
    """解析必须为正数的命令行参数。"""

    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return parsed


def _non_negative_float(value: str) -> float:
    """解析不能为负数的命令行参数。"""

    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("不能小于 0")
    return parsed


def _port(value: str) -> int:
    """解析网页监听端口。"""

    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是整数端口") from error
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("端口必须在 1 到 65535 之间")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""

    parser = argparse.ArgumentParser(
        prog="codex-reset-monitor",
        description="监控 Codex / Grok / Kimi / DeepSeek Harness 等 code agent 的额度、会话、用量和异常流量。",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=default_state_dir(),
        help="状态和日志目录（默认: ~/.codex-reset-monitor）",
    )
    parser.add_argument(
        "--codex-home",
        dest="codex_homes",
        type=Path,
        action="append",
        help=(
            "Codex 登录目录，可重复传入多个账号（默认: ~/.codex；例如 ~/.codex-work）"
        ),
    )
    parser.add_argument(
        "--grok-home",
        dest="grok_homes",
        type=Path,
        action="append",
        help=(
            "Grok 登录目录，可重复传入；默认在存在时使用 ~/.grok 或 GROK_HOME"
        ),
    )
    parser.add_argument(
        "--kimi-home",
        dest="kimi_homes",
        type=Path,
        action="append",
        help=(
            "Kimi Code 数据目录，可重复传入；"
            "默认在存在时使用 ~/.kimi-code 或 KIMI_CODE_HOME"
        ),
    )
    parser.add_argument(
        "--dsh-home",
        dest="dsh_homes",
        type=Path,
        action="append",
        help=(
            "DeepSeek Harness 数据目录，可重复传入；"
            "默认在存在时使用 ~/.dsh 或 DSH_HOME"
        ),
    )
    parser.add_argument(
        "--session-root",
        type=Path,
        default=None,
        help=(
            "单账号时覆盖 session JSONL 根目录；多账号请让它使用"
            "各自 CODEX_HOME/sessions"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="显示更多监控日志",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser(
        "status",
        help="查看历史状态，不会调用 Codex",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出状态",
    )

    quota_parser = subparsers.add_parser(
        "quota",
        help="主动读取当前账户额度，不启动模型任务",
    )
    quota_parser.add_argument(
        "--codex",
        default="codex",
        help="Codex 可执行文件（默认: codex）",
    )
    quota_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出额度快照",
    )

    sessions_parser = subparsers.add_parser(
        "sessions",
        help="发现并列出所有当前活动的 Codex JSONL 会话",
    )
    sessions_parser.add_argument(
        "--codex",
        default="codex",
        help="Codex 可执行文件（默认: codex）",
    )
    sessions_parser.add_argument(
        "--all",
        action="store_true",
        help="同时显示历史上已记录但当前不活动的会话",
    )
    sessions_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出会话列表",
    )

    daemon_parser = subparsers.add_parser(
        "daemon",
        help="持续监控活动会话、额度、本地用量和异常流量",
    )
    _add_daemon_options(daemon_parser)

    traffic_parser = subparsers.add_parser(
        "traffic",
        help="扫描本机 code agent 进程的异常流量",
    )
    traffic_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出流量快照",
    )
    traffic_parser.add_argument(
        "--sample-seconds",
        type=_positive_float,
        default=1.0,
        help="两次采样间隔秒数，用于计算增量（默认: 1）",
    )
    _add_upload_threshold_options(traffic_parser)

    service_parser = subparsers.add_parser(
        "service",
        help="安装和管理无需保持终端打开的 systemd 用户服务",
    )
    service_actions = service_parser.add_subparsers(
        dest="service_action",
        required=True,
    )
    service_install = service_actions.add_parser(
        "install",
        help="保存当前配置并启用后台服务",
    )
    _add_daemon_options(service_install)
    service_actions.add_parser("start", help="启动后台服务")
    service_actions.add_parser("stop", help="停止后台服务")
    service_actions.add_parser("restart", help="重启后台服务")
    service_actions.add_parser("status", help="查看后台服务状态")
    service_logs = service_actions.add_parser(
        "logs",
        help="查看后台服务日志",
    )
    service_logs.add_argument(
        "--lines",
        type=int,
        default=100,
        help="显示最近日志行数（默认: 100）",
    )
    service_logs.add_argument(
        "--follow",
        action="store_true",
        help="持续跟踪新日志，按 Ctrl-C 退出",
    )
    service_actions.add_parser(
        "uninstall",
        help="停止并移除后台服务，保留监控数据库",
    )
    service_actions.add_parser(
        "run",
        help="按已保存配置在前台运行（供 systemd 调用）",
    )

    return parser


def _add_daemon_options(parser: argparse.ArgumentParser) -> None:
    """为 daemon 和 service install 添加完全一致的运行参数。"""

    parser.add_argument(
        "--codex",
        default="codex",
        help="Codex 可执行文件（默认: codex）",
    )
    parser.add_argument(
        "--scan-interval",
        type=_positive_float,
        default=2.0,
        help="进程和 JSONL 扫描间隔秒数（默认: 2）",
    )
    parser.add_argument(
        "--reconcile-interval",
        type=_positive_float,
        default=30.0,
        help="App Server 会话对账间隔秒数（默认: 30）",
    )
    parser.add_argument(
        "--quota-interval",
        type=_positive_float,
        default=300.0,
        help="主动额度查询间隔秒数（默认: 300）",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="同时启动只读网页 Dashboard（默认 127.0.0.1:8765）",
    )
    parser.add_argument(
        "--dashboard-host",
        default="127.0.0.1",
        help="Dashboard 监听地址（默认: 127.0.0.1）",
    )
    parser.add_argument(
        "--dashboard-port",
        type=_port,
        default=8765,
        help="Dashboard 监听端口（默认: 8765）",
    )
    parser.add_argument(
        "--budget-usd",
        type=_positive_float,
        default=None,
        help="每月 API 等价金额预算（美元），用于 Dashboard 预算进度与告警",
    )
    _add_upload_threshold_options(parser)


def _add_upload_threshold_options(parser: argparse.ArgumentParser) -> None:
    """添加异常流量监控的告警阈值参数。"""

    parser.add_argument(
        "--upload-warn-mb",
        type=_positive_float,
        default=8.0,
        help="15 秒内外发达到该 MiB 时黄色告警（默认: 8）",
    )
    parser.add_argument(
        "--upload-alert-mb",
        type=_positive_float,
        default=32.0,
        help="15 秒内外发达到该 MiB 时红色告警（默认: 32）",
    )
    parser.add_argument(
        "--upload-window-warn-mb",
        type=_positive_float,
        default=64.0,
        help="5 分钟累计外发达到该 MiB 时黄色告警（默认: 64）",
    )
    parser.add_argument(
        "--upload-window-alert-mb",
        type=_positive_float,
        default=256.0,
        help="5 分钟累计外发达到该 MiB 时红色告警（默认: 256）",
    )


def _accounts(args: argparse.Namespace) -> tuple[CodexAccount, ...]:
    """把 CLI 参数解析为独立的 Codex 账号配置。"""

    return build_account_specs(
        homes=getattr(args, "codex_homes", None),
        state_dir=args.state_dir,
        session_root=getattr(args, "session_root", None),
    )


def _state_summary(state: JobState) -> dict[str, object]:
    """生成不包含原始 prompt 的状态摘要。"""

    return {
        "job_id": state.job_id,
        "status": state.status.value,
        "cwd": state.cwd,
        "codex_home": state.codex_home,
        "session_id": state.session_id,
        "pid": state.pid,
        "reset_at": state.reset_at,
        "rate_limits": state.rate_limits,
        "last_exit_code": state.last_exit_code,
        "last_error": state.last_error,
        "log_file": state.log_file,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
    }


def _format_timestamp(value: float | None) -> str:
    """把 Unix 时间转换为带时区的本地显示。"""

    if value is None:
        return "未知"
    local_time = datetime.fromtimestamp(value).astimezone()
    return local_time.isoformat(timespec="seconds")


def _show_status(store: StateStore, as_json: bool) -> int:
    """输出状态摘要。"""

    state = store.load()
    if state is None:
        if as_json:
            sys.stdout.write("null\n")
        else:
            sys.stdout.write("没有任务状态。\n")
        return 0

    summary = _state_summary(state)
    if as_json:
        sys.stdout.write(f"{json.dumps(summary, ensure_ascii=False, indent=2)}\n")
        return 0

    sys.stdout.write(f"任务: {state.job_id}\n")
    sys.stdout.write(f"状态: {state.status.value}\n")
    sys.stdout.write(f"CODEX_HOME: {state.codex_home or '当前环境'}\n")
    sys.stdout.write(f"目录: {state.cwd}\n")
    sys.stdout.write(f"Session: {state.session_id or '未知'}\n")
    if state.rate_limits:
        sys.stdout.write("已观察额度窗口:\n")
        for name, window in state.rate_limits.items():
            used_percent = window.get("used_percent")
            window_minutes = window.get("window_minutes")
            sys.stdout.write(
                "  "
                f"{name}: 使用 {used_percent if used_percent is not None else '未知'}%"
                f"，窗口 {window_minutes if window_minutes is not None else '未知'}"
                " 分钟\n"
            )
    sys.stdout.write(f"日志: {state.log_file}\n")
    if state.last_error:
        sys.stdout.write(f"最近错误: {state.last_error}\n")
    return 0


def _quota_summary(snapshot: QuotaSnapshot) -> dict[str, object]:
    """生成不包含敏感信息的额度摘要。"""

    return {
        "observed_at": snapshot.observed_at,
        "plan_type": snapshot.plan_type,
        "source": snapshot.source,
        "raw_limit_ids": list(snapshot.raw_limit_ids),
        "metadata": dict(snapshot.metadata),
        "windows": [
            {
                "limit_id": window.limit_id,
                "name": window.name,
                "used_percent": window.used_percent,
                "window_minutes": window.window_minutes,
                "resets_at": window.resets_at,
                "reached_type": window.reached_type,
                "is_exhausted": window.is_exhausted,
            }
            for window in snapshot.windows
        ],
    }


def _read_account_quota(
    account: CodexAccount,
    codex_path: str,
) -> QuotaSnapshot:
    """主动读取一个账号的额度，失败时安全退回该账号的 JSONL。"""

    registry = MultiSessionRegistry(account.state_dir)
    client = AppServerClient(
        AppServerConfig(
            codex_path=codex_path,
            codex_home=account.home,
        )
    )
    try:
        try:
            client.start()
            snapshot = client.read_rate_limits()
        except AppServerError as error:
            scanner = ProcessScanner(session_root=account.session_root)
            active_paths = tuple(
                path for process in scanner.scan() for path in process.open_jsonl_paths
            )
            known_paths = tuple(
                Path(session.jsonl_path)
                for session in registry.list_sessions(active_only=False)
                if session.jsonl_path is not None
            )
            snapshot = read_jsonl_quota(
                recent_session_paths(
                    account.session_root,
                    active_paths=active_paths,
                    known_paths=known_paths,
                ),
            )
            if snapshot is None:
                raise error
            logging.getLogger(__name__).warning(
                "账号 %s 的 App Server 额度查询失败，使用本地 JSONL 快照: %s",
                account.name,
                error,
            )
        registry.save_quota(snapshot)
        return snapshot
    finally:
        client.close()


def _show_quota(args: argparse.Namespace) -> int:
    """主动读取所有配置账号的额度并输出精确窗口字段。"""

    accounts = _accounts(args)
    results: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    snapshots: list[tuple[CodexAccount, QuotaSnapshot]] = []
    for account in accounts:
        try:
            snapshot = _read_account_quota(account, args.codex)
        except (AppServerError, OSError, RegistryError) as error:
            errors.append({"account": account.name, "error": str(error)})
            logging.getLogger(__name__).error(
                "账号 %s 额度查询失败: %s",
                account.name,
                error,
            )
            continue
        summary = _quota_summary(snapshot)
        summary["account"] = account.account_id or account.name
        summary["account_id"] = account.account_id
        summary["profile_name"] = account.name
        summary["codex_home"] = str(account.home)
        results.append(summary)
        snapshots.append((account, snapshot))

    for grok_home in resolve_grok_homes(getattr(args, "grok_homes", None)):
        grok_snapshot = read_grok_quota(grok_home)
        grok_account = read_grok_account(grok_home)
        if grok_snapshot is None:
            errors.append(
                {
                    "account": grok_account.display_name,
                    "error": "未找到 Grok billing 额度日志",
                }
            )
            continue
        summary = _quota_summary(grok_snapshot)
        summary["account"] = grok_account.display_name
        summary["account_id"] = grok_account.account_id
        summary["profile_name"] = grok_account.profile_name
        summary["codex_home"] = str(grok_home)
        summary["product"] = "grok"
        results.append(summary)

    # Kimi 配额走官方 /usages 接口；读取失败时只输出账号与登录状态。
    for kimi_home in resolve_kimi_homes(getattr(args, "kimi_homes", None)):
        kimi_account = read_kimi_account(kimi_home)
        kimi_snapshot = read_kimi_quota(kimi_home)
        if kimi_snapshot is None:
            results.append(
                {
                    "account": kimi_account.display_name,
                    "account_id": kimi_account.account_id,
                    "profile_name": kimi_account.profile_name,
                    "codex_home": str(kimi_home),
                    "product": "kimi",
                    "logged_in": kimi_account.logged_in,
                    "windows": [],
                }
            )
            continue
        summary = _quota_summary(kimi_snapshot)
        summary["account"] = kimi_account.display_name
        summary["account_id"] = kimi_account.account_id
        summary["profile_name"] = kimi_account.profile_name
        summary["codex_home"] = str(kimi_home)
        summary["product"] = "kimi"
        summary["logged_in"] = kimi_account.logged_in
        results.append(summary)

    for dsh_home in resolve_dsh_homes(getattr(args, "dsh_homes", None)):
        dsh_account = read_dsh_account(dsh_home)
        dsh_snapshot = read_dsh_quota(dsh_home)
        if dsh_snapshot is None:
            results.append(
                {
                    "account": dsh_account.display_name,
                    "account_id": dsh_account.account_id,
                    "profile_name": dsh_account.profile_name,
                    "codex_home": str(dsh_home),
                    "product": "dsh",
                    "has_credentials": dsh_account.has_credentials,
                    "windows": [],
                }
            )
            continue
        summary = _quota_summary(dsh_snapshot)
        summary["account"] = dsh_account.display_name
        summary["account_id"] = dsh_account.account_id
        summary["profile_name"] = dsh_account.profile_name
        summary["codex_home"] = str(dsh_home)
        summary["product"] = "dsh"
        summary["has_credentials"] = dsh_account.has_credentials
        results.append(summary)

    if args.json:
        if len(results) == 1 and not errors:
            output: object = results[0]
        else:
            output = {"accounts": results, "errors": errors}
        sys.stdout.write(f"{json.dumps(output, ensure_ascii=False, indent=2)}\n")
        return 0 if results else 2

    printed = 0
    for account, snapshot in snapshots:
        if printed:
            sys.stdout.write("\n")
        sys.stdout.write(f"账号 ID: {account.account_id or '未识别'}\n")
        sys.stdout.write(f"Profile: {account.name}\n")
        sys.stdout.write(f"CODEX_HOME: {account.home}\n")
        sys.stdout.write(f"套餐: {snapshot.plan_type or '未知'}\n")
        sys.stdout.write(f"查询时间: {_format_timestamp(snapshot.observed_at)}\n")
        for window in snapshot.windows:
            used = (
                f"{window.used_percent:g}%"
                if window.used_percent is not None
                else "未知"
            )
            duration = (
                f"{window.window_minutes:g} 分钟"
                if window.window_minutes is not None
                else "未知"
            )
            reached = window.reached_type or "未命中"
            sys.stdout.write(
                f"{window.limit_id}/{window.name}: 使用 {used}，窗口 {duration}，"
                f"重置 {_format_timestamp(window.resets_at)}，状态 {reached}\n"
            )
        printed += 1
    for summary in results:
        if summary.get("product") != "grok":
            continue
        if printed:
            sys.stdout.write("\n")
        sys.stdout.write(f"账号 ID: {summary.get('account_id') or '未识别'}\n")
        sys.stdout.write("Profile: grok\n")
        sys.stdout.write(f"GROK_HOME: {summary.get('codex_home')}\n")
        sys.stdout.write(f"套餐: {summary.get('plan_type') or '未知'}\n")
        sys.stdout.write(
            f"查询时间: {_format_timestamp(summary.get('observed_at'))}\n"
        )
        windows = summary.get("windows")
        if isinstance(windows, list):
            for window in windows:
                if not isinstance(window, dict):
                    continue
                used_percent = window.get("used_percent")
                used = (
                    f"{used_percent:g}%"
                    if isinstance(used_percent, (int, float))
                    else "未知"
                )
                sys.stdout.write(
                    f"{window.get('limit_id')}/{window.get('name')}: 使用 {used}，"
                    f"重置 {_format_timestamp(window.get('resets_at'))}\n"
                )
        printed += 1
    for summary in results:
        if summary.get("product") != "kimi":
            continue
        if printed:
            sys.stdout.write("\n")
        sys.stdout.write(f"Profile: {summary.get('profile_name')}\n")
        sys.stdout.write(f"KIMI_CODE_HOME: {summary.get('codex_home')}\n")
        logged_in = "已登录" if summary.get("logged_in") else "未找到登录凭据"
        sys.stdout.write(f"登录状态: {logged_in}\n")
        kimi_windows = summary.get("windows")
        if isinstance(kimi_windows, list) and kimi_windows:
            sys.stdout.write(
                f"查询时间: {_format_timestamp(summary.get('observed_at'))}\n"
            )
            for window in kimi_windows:
                if not isinstance(window, dict):
                    continue
                used_percent = window.get("used_percent")
                used = (
                    f"{used_percent:g}%"
                    if isinstance(used_percent, (int, float))
                    else "未知"
                )
                sys.stdout.write(
                    f"{window.get('limit_id')}/{window.get('name')}: 使用 {used}，"
                    f"重置 {_format_timestamp(window.get('resets_at'))}\n"
                )
        else:
            sys.stdout.write(
                "配额暂不可读（网络或登录状态问题），可在 kimi CLI 中用 /usage "
                "查看；本地用量与成本统计不受影响\n"
            )
        printed += 1
    for error in errors:
        sys.stdout.write(f"账号 {error['account']} 查询失败: {error['error']}\n")
    return 0 if printed else 2


def _session_summary(
    session: TrackedSession,
    account_name: str | None = None,
    account_id: str | None = None,
    profile_name: str | None = None,
    product: str | None = None,
) -> dict[str, object]:
    """生成不包含内部调度字段的多会话摘要。"""

    record: dict[str, object] = {
        "thread_id": session.thread_id,
        "session_id": session.session_id,
        "cwd": session.cwd,
        "source": session.source,
        "status": session.status.value,
        "confidence": session.confidence.value,
        "pids": list(session.pids),
        "process_backed": session.is_process_backed,
        "last_event_at": session.last_event_at,
        "last_event_type": session.last_event_type,
        "last_error": _display_session_error(session.last_error),
        "quota_reset_at": session.quota_reset_at,
        "account_id": account_id or session.account_id,
        "active": session.is_active,
    }
    if account_name is not None:
        record["account"] = account_name
    if profile_name is not None:
        record["profile_name"] = profile_name
    if product is not None:
        record["product"] = product
    return record


def _display_session_error(value: str | None) -> str | None:
    """隐藏旧版恢复记录，避免历史错误文本重新出现在命令行中。"""

    if not value:
        return None
    lowered = value.lower()
    legacy_terms = ("自动恢复", "不自动恢复", "续跑", "resume", "recovery")
    if any(term in lowered or term in value for term in legacy_terms):
        if "额度" in value or "quota" in lowered:
            return "额度限制事件"
        return "历史会话状态"
    return value


def _show_sessions(args: argparse.Namespace) -> int:
    """执行一次发现并列出所有账号的活动会话。"""

    accounts = _accounts(args)
    monitor = MultiAccountMonitor(
        accounts=accounts,
        state_dir=args.state_dir,
        config=MonitorConfig(codex_path=args.codex, auto_resume=False),
        kimi_homes=tuple(getattr(args, "kimi_homes", None) or ()),
        dsh_homes=tuple(getattr(args, "dsh_homes", None) or ()),
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
        _session_summary(session, account.account_id or account.name)
        for account, account_sessions in sessions_by_account
        for session in account_sessions
    ]
    extra_sessions: list[tuple[str, TrackedSession]] = []
    for kimi_home in resolve_kimi_homes(getattr(args, "kimi_homes", None)):
        kimi_account = read_kimi_account(kimi_home)
        for session in list_kimi_active_sessions(kimi_home):
            extra_sessions.append((kimi_account.display_name, session))
            summaries.append(
                _session_summary(
                    session,
                    kimi_account.display_name,
                    account_id=kimi_account.account_id,
                    profile_name=kimi_account.profile_name,
                    product="kimi",
                )
            )
    for dsh_home in resolve_dsh_homes(getattr(args, "dsh_homes", None)):
        dsh_account = read_dsh_account(dsh_home)
        for session in list_dsh_active_sessions(dsh_home):
            extra_sessions.append((dsh_account.display_name, session))
            summaries.append(
                _session_summary(
                    session,
                    dsh_account.display_name,
                    account_id=dsh_account.account_id,
                    profile_name=dsh_account.profile_name,
                    product="dsh",
                )
            )
    if args.json:
        sys.stdout.write(f"{json.dumps(summaries, ensure_ascii=False, indent=2)}\n")
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
                f"{session.cwd or '目录未知'}\n"
            )
    for label, session in extra_sessions:
        pids = ",".join(str(pid) for pid in session.pids) or "无"
        sys.stdout.write(
            f"{label} | {session.session_id or session.thread_id} | "
            f"{session.status.value} | PID {pids} | "
            f"{session.cwd or '目录未知'}\n"
        )
    return 0


def _show_traffic(args: argparse.Namespace) -> int:
    """采样两次 TCP 外发增量，用于异常流量监控。"""

    monitor = TrafficMonitor(
        thresholds=TrafficThresholds.from_mb(
            burst_warn_mb=args.upload_warn_mb,
            burst_danger_mb=args.upload_alert_mb,
            window_warn_mb=args.upload_window_warn_mb,
            window_danger_mb=args.upload_window_alert_mb,
        )
    )
    monitor.poll()
    time.sleep(args.sample_seconds)
    snapshot = monitor.poll()
    payload = snapshot.to_dict()
    if args.json:
        sys.stdout.write(f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n")
        return 1 if snapshot.alerts else 0
    if snapshot.source == "unavailable":
        sys.stdout.write("无法读取内核 TCP 计数，异常流量监控不可用。\n")
        return 2
    if not snapshot.processes:
        sys.stdout.write("没有发现正在运行的 code agent 进程。\n")
        return 0
    sys.stdout.write(
        f"采样间隔 {snapshot.interval_seconds:.1f} 秒 · "
        f"近 15 秒合计外发 {format_bytes(payload['totals']['burst_bytes'])}\n"
    )
    for process in snapshot.processes:
        remotes = ", ".join(
            item.remote
            for item in process.connections
            if not item.loopback and not item.service
        ) or "无外连"
        status = process.alert_level or "ok"
        sys.stdout.write(
            f"{process.product} pid {process.pid} | {status} | "
            f"15s {format_bytes(process.burst_bytes)} | "
            f"5min {format_bytes(process.window_bytes)} | "
            f"{process.cwd or '目录未知'} | {remotes}\n"
        )
    for alert in snapshot.alerts:
        sys.stdout.write(f"{alert.level}: {alert.message}\n")
    return 1 if snapshot.alerts else 0


def _monitor(args: argparse.Namespace) -> MultiAccountMonitor:
    """根据 daemon 参数创建多账号监控器。"""

    accounts = _accounts(args)

    config = MonitorConfig(
        codex_path=args.codex,
        scan_interval=args.scan_interval,
        reconcile_interval=args.reconcile_interval,
        quota_interval=args.quota_interval,
        auto_resume=False,
        dashboard=args.dashboard,
        dashboard_host=args.dashboard_host,
        dashboard_port=args.dashboard_port,
        budget_usd=args.budget_usd,
        upload_burst_warn_mb=args.upload_warn_mb,
        upload_burst_danger_mb=args.upload_alert_mb,
        upload_window_warn_mb=args.upload_window_warn_mb,
        upload_window_danger_mb=args.upload_window_alert_mb,
    )
    return MultiAccountMonitor(
        accounts=accounts,
        state_dir=args.state_dir,
        config=config,
        grok_homes=tuple(getattr(args, "grok_homes", None) or ()),
        kimi_homes=tuple(getattr(args, "kimi_homes", None) or ()),
        dsh_homes=tuple(getattr(args, "dsh_homes", None) or ()),
    )


def _service_config(args: argparse.Namespace) -> ServiceConfig:
    """把 service install 参数转换成可持久化配置。"""

    accounts = _accounts(args)
    codex_path = resolve_executable(args.codex)
    return ServiceConfig(
        state_dir=args.state_dir.expanduser().resolve(),
        codex_homes=tuple(account.home for account in accounts),
        session_root=(
            args.session_root.expanduser().resolve()
            if args.session_root is not None
            else None
        ),
        verbose=args.verbose,
        codex_path=str(codex_path),
        scan_interval=args.scan_interval,
        reconcile_interval=args.reconcile_interval,
        quota_interval=args.quota_interval,
        dashboard=args.dashboard,
        dashboard_host=args.dashboard_host,
        dashboard_port=args.dashboard_port,
        budget_usd=args.budget_usd,
        upload_burst_warn_mb=args.upload_warn_mb,
        upload_burst_danger_mb=args.upload_alert_mb,
        upload_window_warn_mb=args.upload_window_warn_mb,
        upload_window_danger_mb=args.upload_window_alert_mb,
        grok_homes=tuple(
            path.expanduser().resolve()
            for path in (getattr(args, "grok_homes", None) or ())
        ),
        kimi_homes=tuple(
            path.expanduser().resolve()
            for path in (getattr(args, "kimi_homes", None) or ())
        ),
        dsh_homes=tuple(
            path.expanduser().resolve()
            for path in (getattr(args, "dsh_homes", None) or ())
        ),
    )


def _manage_service(args: argparse.Namespace) -> int:
    """执行一个 systemd 用户服务管理动作。"""

    manager = UserServiceManager(args.state_dir)
    action = args.service_action
    if action == "install":
        manager.install(_service_config(args))
        sys.stdout.write(
            "后台服务已安装并启动。使用 service status 查看状态，"
            "service logs 查看日志。\n"
        )
        return 0
    if action == "start":
        manager.start()
        return 0
    if action == "stop":
        manager.stop()
        return 0
    if action == "restart":
        manager.restart()
        return 0
    if action == "status":
        return manager.status()
    if action == "logs":
        return manager.logs(lines=args.lines, follow=args.follow)
    if action == "uninstall":
        manager.uninstall()
        sys.stdout.write("后台服务已移除；SQLite 状态仍保留。\n")
        return 0
    if action == "run":
        return run_saved_service(args.state_dir)
    raise ServiceError(f"未知服务操作: {action}")


def _configure_logging(verbose: bool) -> None:
    """配置监控器日志，不改变 Codex 原始 stdout。"""

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """命令行主函数。"""

    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    try:
        if args.command == "status":
            return _show_status(StateStore(args.state_dir), args.json)
        if args.command == "quota":
            return _show_quota(args)
        if args.command == "sessions":
            return _show_sessions(args)
        if args.command == "traffic":
            return _show_traffic(args)
        if args.command == "service":
            return _manage_service(args)
        if args.command == "daemon":
            return _monitor(args).run()
    except (
        AppServerError,
        OSError,
        RegistryError,
        ServiceError,
        StateError,
        ValueError,
    ) as error:
        logging.getLogger(__name__).error("%s", error)
        return 2

    parser.error(f"未知命令: {args.command}")
    return 2
