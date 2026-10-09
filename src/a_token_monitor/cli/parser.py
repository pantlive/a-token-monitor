"""命令行参数定义：全局选项、各子命令参数与界面语言本地化。"""

from __future__ import annotations

import argparse
from pathlib import Path

from ..i18n import (
    active_language,
    translate,
)
from ..housekeeping import (
    DEFAULT_SINGLE_WARN_GIB,
    DEFAULT_TOTAL_WARN_GIB,
)
from ..alerts import (
    DEFAULT_RETENTION_DAYS,
)
from ..providers import PROVIDER_SPECS
from ..retention import (
    DEFAULT_SESSION_RETENTION_DAYS,
    DEFAULT_USAGE_RETENTION_DAYS,
)
from ..usage import (
    DEFAULT_SEARCH_DAYS,
    DEFAULT_SESSION_CONTEXT_WARN_TOKENS,
    DEFAULT_SESSION_TURN_WARN,
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
    for spec in PROVIDER_SPECS.values():
        parser.add_argument(
            spec.cli_option,
            dest=spec.homes_field,
            type=Path,
            action="append",
            help=spec.cli_help,
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
        "--alert-context-content",
        action="store_true",
        help="显示脱敏后的告警会话内容摘要",
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
        "--alert-context-content",
        action="store_true",
        help="显示脱敏后的告警内容摘要（仅允许本机监听地址）",
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
