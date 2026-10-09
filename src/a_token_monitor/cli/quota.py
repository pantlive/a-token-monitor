"""quota 子命令：读取各账号当前额度。"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Sequence

from ..accounts import CodexAccount
from ..app_server import AppServerClient, AppServerConfig, AppServerError
from ..discovery import ProcessScanner
from ..providers import ProviderSpec, home_providers
from ..registry import MultiSessionRegistry, RegistryError
from ..quota import (
    QuotaSnapshot,
    QuotaWindow,
    quota_period,
    quota_period_label,
    quota_window_duration,
)
from ..quota_fallback import read_jsonl_quota, recent_session_paths
from .common import (
    _accounts,
    _dump_json,
    _effective_scan_dirs,
    _format_timestamp,
    _guard_provider,
    _quota_summary,
)


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

    for spec in home_providers():
        if spec.read_account is None or spec.read_quota is None:
            continue
        for home in effective_dirs.homes(spec.key):
            if not home.is_dir():
                continue
            try:
                _append_provider_quota(spec, home, results, errors)
            except Exception:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
                _guard_provider(spec.display_name, home)
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
    for summary in results:
        if summary.get("product") != "claude":
            continue
        if printed:
            sys.stdout.write("\n")
        sys.stdout.write(f"账号: {summary.get('account') or '未识别'}\n")
        sys.stdout.write(f"账号 ID: {summary.get('account_id') or '未识别'}\n")
        sys.stdout.write(f"CLAUDE_CONFIG_DIR: {summary.get('codex_home')}\n")
        sys.stdout.write(f"套餐: {summary.get('plan_type') or '未知'}\n")
        logged_in = "已登录" if summary.get("has_credentials") else "未找到登录凭据"
        sys.stdout.write(f"登录状态: {logged_in}\n")
        claude_windows = summary.get("windows")
        if isinstance(claude_windows, list) and claude_windows:
            sys.stdout.write(
                f"查询时间: {_format_timestamp(summary.get('observed_at'))}\n"
            )
            for window in claude_windows:
                if not isinstance(window, dict):
                    continue
                _write_quota_windows((window,))
        else:
            sys.stdout.write(
                "配额暂不可读（网络、限速或登录状态问题），可在 claude CLI 中"
                "用 /usage 查看；本地用量与成本统计不受影响\n"
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


def _append_provider_quota(
    spec: ProviderSpec,
    home: Path,
    results: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    """读取一个 provider 数据目录的账号与额度，追加到 quota 命令的输出。"""

    provider_account = spec.read_account(home)
    snapshot = spec.read_quota(home)
    identity: dict[str, Any] = {
        "account": provider_account.display_name,
        "account_id": provider_account.account_id,
        "profile_name": provider_account.profile_name,
        "codex_home": str(home),
        "product": spec.product_id,
    }
    if spec.login_field is not None:
        identity[spec.login_field] = getattr(provider_account, spec.login_field)
    if snapshot is None:
        if spec.missing_quota_error is not None:
            errors.append(
                {
                    "account": provider_account.display_name,
                    "error": spec.missing_quota_error,
                }
            )
        else:
            # 中文注释：读取失败或暂无数据时只输出账号与登录状态。
            results.append({**identity, "windows": []})
        return
    summary = _quota_summary(snapshot)
    summary.update(identity)
    results.append(summary)
