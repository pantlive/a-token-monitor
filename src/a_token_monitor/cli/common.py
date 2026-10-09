"""各子命令共用的工具：账号与扫描目录解析、输出格式化、日志与终端编码设置。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Sequence

from ..local_time import to_local
from ..accounts import CodexAccount, build_account_specs
from ..i18n import (
    active_language,
    localize_payload,
)
from ..providers import PROVIDER_SPECS
from ..quota import (
    QuotaSnapshot,
)
from ..scan_dirs import (
    EffectiveScanDirs,
    ProviderDirsState,
    ScanDirsConfig,
    ScanDirsError,
    load_effective_scan_dirs,
    resolve_effective,
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
        spec.key: getattr(args, spec.homes_field, None)
        for spec in PROVIDER_SPECS.values()
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


def _format_timestamp(value: float | None) -> str:
    """把 Unix 时间转换为带时区的本地显示。"""

    if value is None:
        return "未知"
    return to_local(value).isoformat(timespec="seconds")


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


def _guard_provider(label: str, home: Path) -> None:
    """记录单个 provider 目录读取失败，继续处理其他 provider。"""

    logging.getLogger(__name__).exception(
        "%s 目录读取失败，已跳过该目录（其他 provider 不受影响）: %s",
        label,
        home,
    )


def _format_alert_time(timestamp: float) -> str:
    """把 Unix 时间戳格式化成本地时间。"""

    return to_local(float(timestamp)).strftime("%Y-%m-%d %H:%M:%S")


def _format_count(value: object) -> str:
    """把 token 数量格式化为带千位分隔的整数。"""

    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return str(value)


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
