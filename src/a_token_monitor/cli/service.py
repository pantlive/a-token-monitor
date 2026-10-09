"""service 子命令：安装和管理后台服务（systemd / launchd / Windows 计划任务）。"""

from __future__ import annotations

import argparse
import sys

from ..providers import home_providers
from ..service import (
    ServiceConfig,
    create_service_manager,
    ServiceError,
    resolve_executable,
    run_saved_service,
)
from .common import (
    _accounts,
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
        provider_homes={
            spec.key: tuple(
                path.expanduser().resolve()
                for path in (getattr(args, spec.homes_field, None) or ())
            )
            for spec in home_providers()
        },
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
