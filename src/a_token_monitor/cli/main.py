"""命令行入口：解析参数、设置语言与日志，并分派到各子命令。"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Sequence

from ..i18n import (
    resolve_language,
    use_language,
)
from ..housekeeping import (
    HousekeepingError,
)
from ..alerts import (
    AlertStoreError,
)
from ..app_server import AppServerError
from ..registry import RegistryError
from ..service import (
    ServiceError,
)
from ..storage import StateError, StateStore
from .alerts import (
    _show_alerts,
)
from .common import (
    _configure_logging,
    _configure_output_encoding,
)
from .daemon import (
    _monitor,
)
from .disk import (
    _show_disk,
)
from .parser import (
    _localize_parser,
    build_parser,
)
from .quota import (
    _show_quota,
)
from .service import (
    _manage_service,
)
from .sessions import (
    _show_sessions,
)
from .status import (
    _show_status,
)
from .traffic import (
    _show_traffic,
)
from .usage import (
    _show_usage_search,
)
from .update import (
    _notify_update,
    _show_update,
)


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


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """按子命令分派；未知命令交给 argparse 报错。"""

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
    if args.command == "update":
        return _show_update(args)
    if args.command == "service":
        return _manage_service(args)
    if args.command == "daemon":
        return _monitor(args).run()
    parser.error(f"未知命令: {args.command}")
    return 2


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

    # 中文注释：先用缓存提示一次（daemon 之类的长驻命令也能立刻看到），
    # 命令跑完后再允许补一次联网检查；两步都只在新版本未提醒过时输出。
    _notify_update(args, allow_network=False)
    try:
        result = _dispatch(args, parser)
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
    _notify_update(args, allow_network=True)
    return result
