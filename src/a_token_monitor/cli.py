"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .accounts import CodexAccount, build_account_specs
from .claude import list_claude_active_sessions, read_claude_account
from .i18n import (
    active_language,
    localize_payload,
    resolve_language,
    translate,
    use_language,
)
from .agents import product_label
from .housekeeping import (
    DEFAULT_SINGLE_WARN_GIB,
    DEFAULT_TOTAL_WARN_GIB,
    AuditTarget,
    CleanupCriteria,
    DiskThresholds,
    HousekeepingError,
    HousekeepingMonitor,
)
from .alerts import (
    DEFAULT_RETENTION_DAYS,
    AlertQuery,
    AlertStoreError,
    TrafficAlertStore,
)
from .commandcode import (
    list_commandcode_active_sessions,
    read_commandcode_account,
    read_commandcode_quota,
)
from .grok import (
    list_grok_active_sessions,
    read_grok_account,
    read_grok_quota,
)
from .dsh import (
    list_dsh_active_sessions,
    read_dsh_account,
    read_dsh_quota,
)
from .kimi import (
    list_kimi_active_sessions,
    read_kimi_account,
    read_kimi_quota,
)
from .app_server import AppServerClient, AppServerConfig, AppServerError
from .discovery import ProcessScanner
from .multi_account import MultiAccountMonitor
from .monitor import MonitorConfig
from .multi_models import (
    DetectionConfidence,
    SessionStatus,
    TrackedSession,
    session_view,
)
from .models import JobState
from .registry import MultiSessionRegistry, RegistryError
from .quota import (
    QuotaSnapshot,
    QuotaWindow,
    quota_period,
    quota_period_label,
    quota_window_duration,
)
from .quota_fallback import read_jsonl_quota, recent_session_paths
from .retention import (
    DEFAULT_SESSION_RETENTION_DAYS,
    DEFAULT_USAGE_RETENTION_DAYS,
)
from .scan_dirs import (
    EffectiveScanDirs,
    ProviderDirsState,
    ScanDirsConfig,
    ScanDirsController,
    ScanDirsError,
    load_effective_scan_dirs,
    resolve_effective,
)
from .service import (
    ServiceConfig,
    create_service_manager,
    ServiceError,
    resolve_executable,
    run_saved_service,
)
from .storage import StateError, StateStore
from .traffic import TrafficMonitor, TrafficThresholds, format_bytes
from .usage import (
    DEFAULT_SEARCH_DAYS,
    DEFAULT_SESSION_CONTEXT_WARN_TOKENS,
    DEFAULT_SESSION_TURN_WARN,
    SessionSwitchThresholds,
    UsageAggregator,
    enrich_session_views,
    search_since_days,
)


def default_state_dir() -> Path:
    """返回默认的本地状态目录。

    两次改名的旧目录 ``~/.token-monitor`` 和 ``~/.codex-reset-monitor`` 里可能已经
    有额度快照、会话记录和用量索引。新目录尚未建立而旧目录存在时继续使用旧目录，
    避免升级后丢失历史；建立 ``~/.a-token-monitor`` 后即自动切换。
    """

    new_dir = Path.home() / ".a-token-monitor"
    if new_dir.is_dir():
        return new_dir
    for legacy_dir in (
        Path.home() / ".token-monitor",
        Path.home() / ".codex-reset-monitor",
    ):
        if legacy_dir.is_dir():
            return legacy_dir
    return new_dir


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


def _positive_int(value: str) -> int:
    """解析必须为正整数的命令行参数。"""

    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是整数") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
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

    language = argparse.ArgumentParser(add_help=False)
    language.add_argument(
        "--lang",
        choices=("auto", "zh", "en"),
        default="auto",
        help=(
            "输出语言：auto 按 LANG / LC_ALL / LC_MESSAGES 判断，zh 中文，en 英文"
            "（默认: auto）"
        ),
    )
    parser = argparse.ArgumentParser(
        prog="a-token-monitor",
        description="监控 Codex / Grok / Kimi / Command Code / DeepSeek Harness 等 code agent 的额度、会话、用量和异常流量。",
        parents=[language],
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=default_state_dir(),
        help="状态和日志目录（默认: ~/.a-token-monitor）",
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
        "--claude-home",
        dest="claude_homes",
        type=Path,
        action="append",
        help=(
            "Claude Code 数据目录，可重复传入；"
            "默认在存在时使用 ~/.claude 或 CLAUDE_CONFIG_DIR"
        ),
    )
    parser.add_argument(
        "--commandcode-home",
        dest="commandcode_homes",
        type=Path,
        action="append",
        help=(
            "Command Code 数据目录，可重复传入；"
            "默认在存在时使用 ~/.commandcode 或 COMMANDCODE_HOME"
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
        parents=[language],
        help="查看历史状态，不会调用 Codex",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出状态",
    )

    quota_parser = subparsers.add_parser(
        "quota",
        parents=[language],
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
        parents=[language],
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
    _add_session_reminder_options(sessions_parser)
    _add_session_cleanup_options(sessions_parser)

    daemon_parser = subparsers.add_parser(
        "daemon",
        parents=[language],
        help="持续监控活动会话、额度、本地用量和异常流量",
    )
    _add_daemon_options(daemon_parser)

    traffic_parser = subparsers.add_parser(
        "traffic",
        parents=[language],
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

    alerts_parser = subparsers.add_parser(
        "alerts",
        parents=[language],
        help="查询已落盘的历史异常流量告警，并管理已读状态；存在未读告警时退出码为 1",
    )
    alerts_parser.add_argument(
        "--days",
        type=_positive_float,
        default=None,
        help="只显示最近 N 天的告警",
    )
    alerts_parser.add_argument(
        "--since",
        type=float,
        default=None,
        help="只显示该 Unix 时间戳之后的告警",
    )
    alerts_parser.add_argument(
        "--until",
        type=float,
        default=None,
        help="只显示该 Unix 时间戳之前的告警",
    )
    alerts_parser.add_argument(
        "--level",
        choices=("warn", "danger"),
        default=None,
        help="只显示指定级别的告警",
    )
    alerts_parser.add_argument(
        "--kind",
        choices=("burst", "window"),
        default=None,
        help="只显示突发窗口（burst）或累计窗口（window）告警",
    )
    alerts_parser.add_argument(
        "--product",
        default=None,
        help="只显示指定 agent 产品的告警，例如 codex / dsh / kimi",
    )
    alerts_parser.add_argument(
        "--unread",
        action="store_true",
        help="只显示未读告警",
    )
    alerts_parser.add_argument(
        "--read",
        action="store_true",
        help="只显示已读告警",
    )
    alerts_parser.add_argument(
        "--query",
        default=None,
        help="按关键词搜索告警文本、命令、目录和对端地址",
    )
    alerts_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="最多显示多少条（默认: 50，最大 500）",
    )
    alerts_parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="跳过多少条，用于翻页（默认: 0）",
    )
    alerts_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出告警列表",
    )
    alerts_parser.add_argument(
        "--stats",
        action="store_true",
        help="只输出统计信息，不列出告警明细",
    )
    alerts_parser.add_argument(
        "--quiet",
        action="store_true",
        help="不输出内容，仅在存在未读告警时返回退出码 1",
    )
    alerts_parser.add_argument(
        "--ack",
        type=int,
        nargs="+",
        metavar="ID",
        help="把指定告警标记为已读",
    )
    alerts_parser.add_argument(
        "--ack-all",
        action="store_true",
        help="把所有未读告警标记为已读",
    )
    alerts_parser.add_argument(
        "--clear",
        type=int,
        nargs="+",
        metavar="ID",
        help="删除指定告警",
    )
    alerts_parser.add_argument(
        "--clear-before",
        type=_positive_float,
        default=None,
        metavar="DAYS",
        help="删除 N 天前的历史告警；与 --dry-run 组合可先预览条数",
    )
    alerts_parser.add_argument(
        "--clear-all",
        action="store_true",
        help="删除全部历史告警（需要 --yes）",
    )
    alerts_parser.add_argument(
        "--prune",
        action="store_true",
        help="按保留天数清理过期告警",
    )
    alerts_parser.add_argument(
        "--retention-days",
        type=_positive_float,
        default=DEFAULT_RETENTION_DAYS,
        help=f"清理时保留的天数（默认: {DEFAULT_RETENTION_DAYS:g}）",
    )
    alerts_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只预览 --clear-before / --prune 会删除的条数，不真正删除",
    )
    alerts_parser.add_argument(
        "--yes",
        action="store_true",
        help="确认执行 --clear-all 等破坏性操作",
    )

    usage_parser = subparsers.add_parser(
        "usage",
        parents=[language],
        help="按日期、模型、账号和会话检索已索引的 token 用量历史",
    )
    usage_parser.add_argument(
        "--days",
        type=_non_negative_float,
        default=DEFAULT_SEARCH_DAYS,
        help=f"只统计最近 N 天，0 表示全部历史（默认: {DEFAULT_SEARCH_DAYS}）",
    )
    usage_parser.add_argument(
        "--from",
        dest="date_from",
        default=None,
        metavar="YYYY-MM-DD",
        help="起始日期（本地自然日），优先于 --days",
    )
    usage_parser.add_argument(
        "--to",
        dest="date_to",
        default=None,
        metavar="YYYY-MM-DD",
        help="结束日期（本地自然日，含当天）",
    )
    usage_parser.add_argument(
        "--model",
        dest="models",
        action="append",
        default=None,
        help="只统计指定模型，可重复传入",
    )
    usage_parser.add_argument(
        "--session",
        default=None,
        help="按会话 ID 或 JSONL 文件名筛选",
    )
    usage_parser.add_argument(
        "--project",
        default=None,
        help="按项目 / 工作目录筛选",
    )
    usage_parser.add_argument(
        "--account",
        default=None,
        help="按账号筛选（匹配账号 ID、profile 名或产品，如 codex / grok）",
    )
    usage_parser.add_argument(
        "--query",
        default=None,
        help="按关键词搜索会话路径、项目和模型",
    )
    usage_parser.add_argument(
        "--group",
        choices=("session", "date", "model", "account"),
        default="session",
        help="分组方式：会话明细 / 按日期汇总 / 按模型汇总 / 按账号汇总（默认: session）",
    )
    usage_parser.add_argument(
        "--sort",
        choices=("recent", "tokens", "cost"),
        default="recent",
        help="排序方式：最近活动 / token 用量 / 估算金额（默认: recent）",
    )
    usage_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="最多显示多少行（默认: 50，最大 500）",
    )
    usage_parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="跳过多少行，用于翻页（默认: 0）",
    )
    usage_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出检索结果",
    )

    disk_parser = subparsers.add_parser(
        "disk",
        parents=[language],
        help="统计 .codex 等 agent 数据目录的磁盘占用，并预览可归档或清理的会话",
    )
    disk_parser.add_argument(
        "--days",
        type=_positive_int,
        default=30,
        help="预览只处理 N 天前最后修改的会话（默认: 30）",
    )
    disk_parser.add_argument(
        "--top",
        type=_positive_int,
        default=3,
        help="每个目录展示占用最大的 N 个子目录（默认: 3）",
    )
    disk_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出磁盘报告",
    )
    _add_disk_threshold_options(disk_parser)

    service_parser = subparsers.add_parser(
        "service",
        parents=[language],
        help="安装和管理无需保持终端打开的后台服务（Linux systemd / macOS launchd）",
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
        "plist",
        help="打印当前平台的服务定义（systemd 单元或 launchd plist）",
    )
    service_actions.add_parser(
        "run",
        help="按已保存配置在前台运行（供 systemd / launchd 调用）",
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
        help="同时启动网页 Dashboard（默认 127.0.0.1:8765）",
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
    _add_session_reminder_options(parser)
    _add_disk_threshold_options(parser)
    parser.add_argument(
        "--alert-retention-days",
        type=_positive_float,
        default=DEFAULT_RETENTION_DAYS,
        help=(
            "异常流量告警历史保留天数，超期自动清理"
            f"（默认: {DEFAULT_RETENTION_DAYS:g}）"
        ),
    )
    parser.add_argument(
        "--usage-retention-days",
        type=_positive_float,
        default=DEFAULT_USAGE_RETENTION_DAYS,
        help=(
            "用量索引历史保留天数，超期自动清理"
            f"（默认: {DEFAULT_USAGE_RETENTION_DAYS:g}；"
            "可在 Dashboard 设置页「历史数据」在线覆盖）"
        ),
    )
    parser.add_argument(
        "--session-retention-days",
        type=_positive_float,
        default=DEFAULT_SESSION_RETENTION_DAYS,
        help=(
            "已结束会话历史保留天数，超期自动清理"
            f"（默认: {DEFAULT_SESSION_RETENTION_DAYS:g}；"
            "可在 Dashboard 设置页「历史数据」在线覆盖）"
        ),
    )
    _add_upload_threshold_options(parser)


def _add_session_reminder_options(parser: argparse.ArgumentParser) -> None:
    """添加长会话提醒的两个阈值参数。"""

    parser.add_argument(
        "--session-turn-warn",
        type=_positive_int,
        default=DEFAULT_SESSION_TURN_WARN,
        help=(
            "会话轮数达到该值时提醒切换新会话"
            f"（默认: {DEFAULT_SESSION_TURN_WARN}）"
        ),
    )
    parser.add_argument(
        "--session-context-warn-tokens",
        type=_positive_int,
        default=DEFAULT_SESSION_CONTEXT_WARN_TOKENS,
        help=(
            "会话最近一次上下文 token 达到该值时提醒切换新会话"
            f"（默认: {DEFAULT_SESSION_CONTEXT_WARN_TOKENS}）"
        ),
    )


def _add_disk_threshold_options(parser: argparse.ArgumentParser) -> None:
    """添加磁盘占用提醒的两个阈值参数。"""

    parser.add_argument(
        "--disk-warn-gb",
        type=_positive_float,
        default=DEFAULT_SINGLE_WARN_GIB,
        help=(
            "单个 agent 数据目录占用达到该 GiB 时提醒"
            f"（默认: {DEFAULT_SINGLE_WARN_GIB:g}）"
        ),
    )
    parser.add_argument(
        "--disk-total-warn-gb",
        type=_positive_float,
        default=DEFAULT_TOTAL_WARN_GIB,
        help=(
            "全部 agent 数据目录合计占用达到该 GiB 时提醒"
            f"（默认: {DEFAULT_TOTAL_WARN_GIB:g}）"
        ),
    )


def _add_session_cleanup_options(parser: argparse.ArgumentParser) -> None:
    """添加会话归档与清理参数。"""

    parser.add_argument(
        "--older-than",
        type=_positive_int,
        default=30,
        metavar="DAYS",
        help="只处理 N 天前最后修改的会话文件（默认: 30）",
    )
    parser.add_argument(
        "--min-size-mb",
        type=_non_negative_float,
        default=0.0,
        metavar="MB",
        help="只处理不小于该 MiB 的会话文件（默认: 0）",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="把符合条件的会话压缩归档到归档目录，然后删除原文件",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="直接删除符合条件的会话文件",
    )
    parser.add_argument(
        "--session",
        dest="session_keys",
        action="append",
        default=None,
        metavar="ID|PATH",
        help=(
            "只处理指定会话（session ID 或 JSONL 路径），可重复传入；"
            "与 --archive/--clean 一起使用时忽略 --older-than"
        ),
    )
    parser.add_argument(
        "--restore",
        type=Path,
        default=None,
        metavar="ARCHIVE",
        help="从 tar.gz 归档恢复会话文件",
    )
    parser.add_argument(
        "--to",
        type=Path,
        default=None,
        metavar="DIR",
        help="恢复时的目标根目录（默认恢复到原始绝对路径）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="确认真正执行归档或清理；缺省只预览",
    )


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


def _cli_scan_homes(args: argparse.Namespace) -> dict[str, Sequence[Path] | None]:
    """从 CLI 参数收集各 provider 数据目录；未传入时为 None。"""

    return {
        "codex": getattr(args, "codex_homes", None),
        "claude": getattr(args, "claude_homes", None),
        "commandcode": getattr(args, "commandcode_homes", None),
        "dsh": getattr(args, "dsh_homes", None),
        "grok": getattr(args, "grok_homes", None),
        "kimi": getattr(args, "kimi_homes", None),
    }


def _effective_scan_dirs(args: argparse.Namespace) -> EffectiveScanDirs:
    """解析一次性命令的生效扫描目录（Web 配置 > 命令行参数 > 自动探测）。"""

    cli_homes = _cli_scan_homes(args)
    try:
        return load_effective_scan_dirs(args.state_dir, cli_homes)
    except ScanDirsError as error:
        # 中文注释：只读命令不应被损坏的 Web 配置阻断，
        # 记录警告后按命令行参数和自动探测继续。
        logging.getLogger(__name__).warning(
            "扫描目录配置损坏，本次忽略 Web 配置: %s",
            error,
        )
        return resolve_effective(ScanDirsConfig(), cli_homes)


def _daemon_homes(state: ProviderDirsState) -> tuple[Path, ...] | None:
    """自动探测来源传 None 交给监控器探测，其余来源按生效列表原样传入。"""

    return None if state.source == "auto" else state.effective


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
        sys.stdout.write(f"{_dump_json(summary)}\n")
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
            policy = QuotaWindow(
                limit_id="codex",
                name=str(name),
                used_percent=used_percent if isinstance(used_percent, (int, float)) else None,
                window_minutes=(
                    float(window["window_minutes"])
                    if isinstance(window.get("window_minutes"), (int, float))
                    else None
                ),
                resets_at=None,
            )
            sys.stdout.write(
                "  "
                f"{quota_period_label(quota_period(policy))}窗口（{name}）: "
                f"使用 {used_percent if used_percent is not None else '未知'}%，"
                f"窗口 {quota_window_duration(policy)}\n"
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


def _guard_provider(label: str, home: Path) -> None:
    """记录单个 provider 目录读取失败，继续处理其他 provider。"""

    logging.getLogger(__name__).exception(
        "%s 目录读取失败，已跳过该目录（其他 provider 不受影响）: %s",
        label,
        home,
    )


def _show_quota(args: argparse.Namespace) -> int:
    """主动读取所有配置账号的额度并输出精确窗口字段。"""

    accounts = _accounts(args)
    effective_dirs = _effective_scan_dirs(args)
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

    for grok_home in effective_dirs.homes("grok"):
        if not grok_home.is_dir():
            continue
        try:
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
        except Exception:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider('Grok', grok_home)
    for kimi_home in effective_dirs.homes("kimi"):
        if not kimi_home.is_dir():
            continue
        try:
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

        except Exception:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider('Kimi', kimi_home)
    for dsh_home in effective_dirs.homes("dsh"):
        if not dsh_home.is_dir():
            continue
        try:
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

        # Command Code 订阅额度走官方后台接口；读取失败时只输出账号与登录状态。
        except Exception:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider('DeepSeek Harness', dsh_home)
    for commandcode_home in effective_dirs.homes("commandcode"):
        if not commandcode_home.is_dir():
            continue
        try:
            commandcode_account = read_commandcode_account(commandcode_home)
            commandcode_snapshot = read_commandcode_quota(commandcode_home)
            if commandcode_snapshot is None:
                results.append(
                    {
                        "account": commandcode_account.display_name,
                        "account_id": commandcode_account.account_id,
                        "profile_name": commandcode_account.profile_name,
                        "codex_home": str(commandcode_home),
                        "product": "command-code",
                        "logged_in": commandcode_account.logged_in,
                        "windows": [],
                    }
                )
                continue
            summary = _quota_summary(commandcode_snapshot)
            summary["account"] = commandcode_account.display_name
            summary["account_id"] = commandcode_account.account_id
            summary["profile_name"] = commandcode_account.profile_name
            summary["codex_home"] = str(commandcode_home)
            summary["product"] = "command-code"
            summary["logged_in"] = commandcode_account.logged_in
            results.append(summary)

        except Exception:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider('Command Code', commandcode_home)
    if args.json:
        if len(results) == 1 and not errors:
            output: object = results[0]
        else:
            output = {"accounts": results, "errors": errors}
        sys.stdout.write(f"{_dump_json(output)}\n")
        return 0 if results else 2

    if not results and not errors:
        sys.stdout.write(
            "未发现任何账号：本机没有 CODEX_HOME，也没有配置 Grok / Kimi / "
            "DeepSeek Harness / Claude Code / Command Code 数据目录。\n"
        )
        return 2

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
            duration = quota_window_duration(window)
            period = quota_period_label(quota_period(window))
            reached = window.reached_type or "未命中"
            sys.stdout.write(
                f"{period}窗口（{window.limit_id}/{window.name}）: 使用 {used}，"
                f"窗口 {duration}，重置 {_format_timestamp(window.resets_at)}，"
                f"状态 {reached}\n"
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
                _write_quota_windows((window,))
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
                _write_quota_windows((window,))
        else:
            sys.stdout.write(
                "配额暂不可读（网络或登录状态问题），可在 kimi CLI 中用 /usage "
                "查看；本地用量与成本统计不受影响\n"
            )
        printed += 1
    for summary in results:
        if summary.get("product") != "command-code":
            continue
        if printed:
            sys.stdout.write("\n")
        sys.stdout.write(f"账号 ID: {summary.get('account_id') or '未识别'}\n")
        sys.stdout.write(f"Profile: {summary.get('profile_name')}\n")
        sys.stdout.write(f"COMMANDCODE_HOME: {summary.get('codex_home')}\n")
        sys.stdout.write(f"套餐: {summary.get('plan_type') or '未知'}\n")
        logged_in = "已登录" if summary.get("logged_in") else "未找到登录凭据"
        sys.stdout.write(f"登录状态: {logged_in}\n")
        metadata = summary.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        if metadata.get("period_credits_spent"):
            sys.stdout.write(
                f"本月消费: {metadata['period_credits_spent']} 名额"
                f"，剩余 {metadata.get('monthly_credits_remaining', '未知')}"
                f"，{metadata.get('days_left', '未知')} 天后重置\n"
            )
        if metadata.get("period_requests"):
            sys.stdout.write(f"本月请求: {metadata['period_requests']} 次\n")
        commandcode_windows = summary.get("windows")
        if isinstance(commandcode_windows, list) and commandcode_windows:
            sys.stdout.write(
                f"查询时间: {_format_timestamp(summary.get('observed_at'))}\n"
            )
            for window in commandcode_windows:
                if not isinstance(window, dict):
                    continue
                _write_quota_windows((window,))
        else:
            sys.stdout.write(
                "配额暂不可读（网络或登录状态问题），可在 command-code CLI 中"
                "用 /usage 查看\n"
            )
        printed += 1
    for error in errors:
        sys.stdout.write(f"账号 {error['account']} 查询失败: {error['error']}\n")
    return 0 if printed else 2


def _write_quota_windows(windows: Sequence[object], indent: str = "") -> None:
    """按统一周期口径打印额度窗口。

    各 provider 的返回格式不一样（limit_id / name / window_minutes 的字典），
    这里先转成 ``QuotaWindow`` 再用 ``quota_period`` 归类，命令行就不会再出现
    ``codex/primary`` 这种看不出周期的标题。
    """

    for window in windows:
        if not isinstance(window, dict):
            continue
        used_percent = window.get("used_percent")
        minutes = window.get("window_minutes")
        policy = QuotaWindow(
            limit_id=str(window.get("limit_id") or "codex"),
            name=str(window.get("name") or "unknown"),
            used_percent=(
                used_percent if isinstance(used_percent, (int, float)) else None
            ),
            window_minutes=(
                float(minutes) if isinstance(minutes, (int, float)) else None
            ),
            resets_at=None,
        )
        used = (
            f"{used_percent:g}%" if isinstance(used_percent, (int, float)) else "未知"
        )
        sys.stdout.write(
            f"{indent}{quota_period_label(quota_period(policy))}窗口"
            f"（{policy.limit_id}/{policy.name}）: 使用 {used}，"
            f"窗口 {quota_window_duration(policy)}，"
            f"重置 {_format_timestamp(window.get('resets_at'))}\n"
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
        grok_homes=effective_dirs.homes("grok"),
        kimi_homes=effective_dirs.homes("kimi"),
        dsh_homes=effective_dirs.homes("dsh"),
        commandcode_homes=effective_dirs.homes("commandcode"),
        claude_homes=effective_dirs.homes("claude"),
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
    for grok_home in effective_dirs.homes("grok"):
        if not grok_home.is_dir():
            continue
        try:
            grok_account = read_grok_account(grok_home)
            for session in list_grok_active_sessions(grok_home):
                extra_sessions.append((grok_account.display_name, session))
                summaries.append(
                    session_view(
                        session,
                        grok_account.display_name,
                        account_id=grok_account.account_id,
                        profile_name=grok_account.profile_name,
                        product="grok",
                    )
                )
        except Exception:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider('Grok', grok_home)
    for kimi_home in effective_dirs.homes("kimi"):
        if not kimi_home.is_dir():
            continue
        try:
            kimi_account = read_kimi_account(kimi_home)
            for session in list_kimi_active_sessions(kimi_home):
                extra_sessions.append((kimi_account.display_name, session))
                summaries.append(
                    session_view(
                        session,
                        kimi_account.display_name,
                        account_id=kimi_account.account_id,
                        profile_name=kimi_account.profile_name,
                        product="kimi",
                    )
                )
        except Exception:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider('Kimi', kimi_home)
    for dsh_home in effective_dirs.homes("dsh"):
        if not dsh_home.is_dir():
            continue
        try:
            dsh_account = read_dsh_account(dsh_home)
            for session in list_dsh_active_sessions(dsh_home):
                extra_sessions.append((dsh_account.display_name, session))
                summaries.append(
                    session_view(
                        session,
                        dsh_account.display_name,
                        account_id=dsh_account.account_id,
                        profile_name=dsh_account.profile_name,
                        product="dsh",
                    )
                )
        except Exception:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider('DeepSeek Harness', dsh_home)
    for commandcode_home in effective_dirs.homes("commandcode"):
        if not commandcode_home.is_dir():
            continue
        try:
            commandcode_account = read_commandcode_account(commandcode_home)
            for session in list_commandcode_active_sessions(commandcode_home):
                extra_sessions.append((commandcode_account.display_name, session))
                summaries.append(
                    session_view(
                        session,
                        commandcode_account.display_name,
                        account_id=commandcode_account.account_id,
                        profile_name=commandcode_account.profile_name,
                        product="command-code",
                    )
                )
        except Exception:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider('Command Code', commandcode_home)
    for claude_home in effective_dirs.homes("claude"):
        if not claude_home.is_dir():
            continue
        try:
            claude_account = read_claude_account(claude_home)
            for session in list_claude_active_sessions(claude_home):
                extra_sessions.append((claude_account.display_name, session))
                summaries.append(
                    session_view(
                        session,
                        claude_account.display_name,
                        account_id=claude_account.account_id,
                        profile_name=claude_account.profile_name,
                    )
                )
        except Exception:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider("Claude Code", claude_home)

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

    aggregator = UsageAggregator(
        cache_path=args.state_dir.expanduser() / "usage-index.sqlite3",
        claude_homes=_effective_scan_dirs(args).homes("claude"),
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
        sys.stdout.write(f"{_dump_json(payload)}\n")
        return 1 if snapshot.alerts else 0
    process_only = snapshot.source == "process-only"
    if snapshot.source == "unavailable":
        reason = snapshot.reason or "无法读取内核 TCP 计数"
        sys.stdout.write(f"{reason}；异常流量监控不可用。\n")
        return 2
    if process_only:
        sys.stdout.write(
            f"{snapshot.reason or '当前平台没有内核 TCP 计数'}"
            "；下面只列出 agent 进程与远端连接。\n"
        )
    if not snapshot.processes:
        sys.stdout.write("没有发现正在运行的 code agent 进程。\n")
        return 0
    if not process_only:
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
        if process_only:
            sys.stdout.write(
                f"{process.product} pid {process.pid} | {status} | "
                f"{process.cwd or '目录未知'} | {remotes}\n"
            )
            continue
        sys.stdout.write(
            f"{process.product} pid {process.pid} | {status} | "
            f"15s {format_bytes(process.burst_bytes)} | "
            f"5min {format_bytes(process.window_bytes)} | "
            f"{process.cwd or '目录未知'} | {remotes}\n"
        )
    for alert in snapshot.alerts:
        sys.stdout.write(f"{alert.level}: {alert.message}\n")
    return 1 if snapshot.alerts else 0


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
    if args.json:
        sys.stdout.write(
            f"{_dump_json({'stats': stats, 'alerts': [item.to_dict() for item in alerts], 'has_more': has_more})}\n"
        )
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
    if has_more:
        sys.stdout.write(
            f"还有更多记录，使用 --offset {query.offset + query.limit} 继续查看。\n"
        )
    return 1 if stats["unread"] else 0


def _format_alert_time(timestamp: float) -> str:
    """把 Unix 时间戳格式化成本地时间。"""

    return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M:%S")


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
        return datetime.strptime(value.strip(), "%Y-%m-%d").timestamp()
    except ValueError as error:
        raise ValueError(f"--from 需要 YYYY-MM-DD 格式: {value}") from error


def _cli_day_end(value: str | None) -> float | None:
    """把 YYYY-MM-DD 解析为本地当天最后一刻的时间戳。"""

    if value is None:
        return None
    start = _cli_day_start(value)
    return None if start is None else start + 86400.0 - 1e-6


def _format_count(value: object) -> str:
    """把 token 数量格式化为带千位分隔的整数。"""

    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return str(value)


def _show_usage_search(args: argparse.Namespace) -> int:
    """按日期、模型、账号和会话检索用量索引中的 token 历史记录。"""

    aggregator = UsageAggregator(
        cache_path=args.state_dir.expanduser() / "usage-index.sqlite3",
        claude_homes=_effective_scan_dirs(args).homes("claude"),
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
    for home in effective_dirs.homes("grok"):
        targets.append(AuditTarget("Grok", "grok", home))
    for home in effective_dirs.homes("kimi"):
        targets.append(AuditTarget("Kimi Code", "kimi", home))
    for home in effective_dirs.homes("dsh"):
        targets.append(AuditTarget("DeepSeek Harness", "dsh", home))
    for home in effective_dirs.homes("commandcode"):
        targets.append(AuditTarget("Command Code", "command-code", home))
    for home in effective_dirs.homes("claude"):
        targets.append(AuditTarget("Claude Code", "claude", home))
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
    monitor.active_paths = _active_session_paths(args)
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
            grok_homes=effective_dirs.homes("grok"),
            kimi_homes=effective_dirs.homes("kimi"),
            dsh_homes=effective_dirs.homes("dsh"),
            commandcode_homes=effective_dirs.homes("commandcode"),
            claude_homes=effective_dirs.homes("claude"),
        )
        paths: set[str] = set()
        for item in monitor.account_monitors:
            for session in item.registry.list_sessions(active_only=True):
                if session.pids and session.jsonl_path:
                    paths.add(str(Path(session.jsonl_path)))
        return paths

    return collect


def _monitor(args: argparse.Namespace) -> MultiAccountMonitor:
    """根据 daemon 参数创建多账号监控器。"""

    # 中文注释：daemon 的扫描目录按 Web 配置 > 命令行参数 > 自动探测解析；
    # controller 交给监控器，Dashboard 修改配置后可热重载。
    controller = ScanDirsController(args.state_dir, _cli_scan_homes(args))
    effective_dirs = controller.effective()
    codex_state = effective_dirs.state("codex")
    accounts = build_account_specs(
        homes=(None if codex_state.source == "auto" else codex_state.effective),
        state_dir=args.state_dir,
        session_root=getattr(args, "session_root", None),
    )

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
        alert_retention_days=args.alert_retention_days,
        usage_retention_days=args.usage_retention_days,
        session_retention_days=args.session_retention_days,
        session_turn_warn=args.session_turn_warn,
        session_context_warn_tokens=args.session_context_warn_tokens,
        disk_warn_gb=args.disk_warn_gb,
        disk_total_warn_gb=args.disk_total_warn_gb,
    )
    return MultiAccountMonitor(
        accounts=accounts,
        state_dir=args.state_dir,
        config=config,
        grok_homes=_daemon_homes(effective_dirs.state("grok")),
        kimi_homes=_daemon_homes(effective_dirs.state("kimi")),
        dsh_homes=_daemon_homes(effective_dirs.state("dsh")),
        commandcode_homes=_daemon_homes(effective_dirs.state("commandcode")),
        claude_homes=_daemon_homes(effective_dirs.state("claude")),
        scan_dirs_controller=controller,
    )


def _service_config(args: argparse.Namespace) -> ServiceConfig:
    """把 service install 参数转换成可持久化配置。"""

    accounts = _accounts(args)
    # 中文注释：没有 Codex 账号时不解析 Codex 可执行文件，
    # 允许在没装 Codex CLI 的机器上安装只监控其他 provider 的后台服务。
    codex_path = resolve_executable(args.codex) if accounts else str(args.codex)
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
        alert_retention_days=args.alert_retention_days,
        usage_retention_days=args.usage_retention_days,
        session_retention_days=args.session_retention_days,
        session_turn_warn=args.session_turn_warn,
        session_context_warn_tokens=args.session_context_warn_tokens,
        disk_warn_gb=args.disk_warn_gb,
        disk_total_warn_gb=args.disk_total_warn_gb,
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
        commandcode_homes=tuple(
            path.expanduser().resolve()
            for path in (getattr(args, "commandcode_homes", None) or ())
        ),
        claude_homes=tuple(
            path.expanduser().resolve()
            for path in (getattr(args, "claude_homes", None) or ())
        ),
    )


def _service_definition_path(manager: object) -> object:
    """返回服务定义文件路径：systemd 单元 / launchd plist / 计划任务 XML。"""

    for attribute in ("plist_path", "task_path", "unit_path"):
        value = getattr(manager, attribute, None)
        if value is not None:
            return value
    return getattr(manager, "unit_path", "")


def _render_service_definition(manager: object) -> str:
    """渲染当前平台的服务定义文本。"""

    for attribute in ("render_plist", "render_task_xml", "render_unit"):
        render = getattr(manager, attribute, None)
        if callable(render):
            return str(render())
    raise ServiceError("当前平台没有可用的服务定义模板")


def _manage_service(args: argparse.Namespace) -> int:
    """执行一个后台服务管理动作（systemd / launchd / 计划任务）。"""

    manager = create_service_manager(args.state_dir)
    action = args.service_action
    if action == "install":
        manager.install(_service_config(args))
        sys.stdout.write(
            f"后台服务已安装并启动（服务定义: {_service_definition_path(manager)}）。"
            "使用 service status 查看状态，service logs 查看日志。\n"
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
    if action == "plist":
        sys.stdout.write(_render_service_definition(manager))
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


def _configure_output_encoding() -> None:
    """把标准输出/错误固定为 UTF-8。

    中文注释：Windows 控制台在重定向（管道、计划任务）时按本地代码页编码，
    中文与 ``⚠`` 之类的符号会抛 ``UnicodeEncodeError``；这里统一成 UTF-8 并允许
    替换字符，保证任何平台、任何终端都能输出。
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # pragma: no cover - 特殊流不支持时忽略
            continue


def _dump_json(payload: object) -> str:
    """输出 JSON：英文环境下按目录表翻译字符串值，和 /api/* 的处理一致。"""

    return json.dumps(
        localize_payload(payload, active_language()),
        ensure_ascii=False,
        indent=2,
    )


def _localize_parser(parser: argparse.ArgumentParser) -> None:
    """把 argparse 里的文案换成当前语言，交给 argparse 自己按目标语言换行。

    必须在 ``parse_args`` 之前做：argparse 会按终端宽度自己折行，如果在输出阶段做
    子串替换，折行会把句子切断、匹配不上。
    """

    language = active_language()
    if language != "en":
        return
    if parser.description:
        parser.description = translate(parser.description, language)
    for action in parser._actions:  # noqa: SLF001 - argparse 没提供公开遍历接口
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for subparser in action.choices.values():
                _localize_parser(subparser)
        if action.help:
            action.help = translate(action.help, language)


def _requested_language(
    arguments: Sequence[str],
    explicit: str | None = None,
) -> str:
    """判断本次调用要用的语言：``--lang`` 显式值优先，否则按环境变量。

    单独抽出来是因为 ``--help`` 由 argparse 在解析阶段就打印，必须在解析前就知道语言。
    """

    if explicit is not None and explicit != "auto":
        return explicit
    for index, item in enumerate(arguments):
        if item == "--lang" and index + 1 < len(arguments):
            candidate = arguments[index + 1]
            if candidate in {"zh", "en"}:
                return candidate
            break
        if item.startswith("--lang="):
            candidate = item.split("=", 1)[1]
            if candidate in {"zh", "en"}:
                return candidate
            break
    return resolve_language()


def main(argv: Sequence[str] | None = None) -> int:
    """命令行主函数。"""

    _configure_output_encoding()
    arguments = list(sys.argv[1:] if argv is None else argv)
    # 必须在 parse_args 之前：argparse 处理 --help 时就直接打印并退出了，
    # 那时再包 stdout 已经来不及。
    use_language(_requested_language(arguments))
    parser = build_parser()
    _localize_parser(parser)
    args = parser.parse_args(arguments)
    use_language(_requested_language(arguments, explicit=args.lang))
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
        if args.command == "alerts":
            return _show_alerts(args)
        if args.command == "usage":
            return _show_usage_search(args)
        if args.command == "disk":
            return _show_disk(args)
        if args.command == "service":
            return _manage_service(args)
        if args.command == "daemon":
            return _monitor(args).run()
    except (
        AlertStoreError,
        AppServerError,
        HousekeepingError,
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
