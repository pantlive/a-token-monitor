"""daemon 子命令：组装常驻多账号监控器。"""

from __future__ import annotations

import argparse

from ..accounts import build_account_specs
from ..multi_account import MultiAccountMonitor
from ..monitor import MonitorConfig
from ..providers import home_keys
from ..scan_dirs import (
    ScanDirsController,
)
from .common import (
    _cli_scan_homes,
    _daemon_homes,
)


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
        alert_context_content=getattr(args, "alert_context_content", False),
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
        homes={
            key: _daemon_homes(effective_dirs.state(key)) for key in home_keys()
        },
        scan_dirs_controller=controller,
    )
