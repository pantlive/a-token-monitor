"""update 子命令：检查新版本、（可选）执行升级，以及一次性命令的自动提醒。

自动提醒的原则是「不拖慢命令」：命令结束前只读状态目录里的缓存，缓存过期且
stderr 是终端时才补一次同步抓取；非交互环境（脚本、CI、服务）从不联网，
真正的抓取交给 ``update`` 命令、daemon 后台线程或 ``POST /api/update``。
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from ..local_time import to_local
from ..updates import (
    DEFAULT_TIMEOUT,
    NOTICE_TIMEOUT,
    UpdateChecker,
    checks_disabled,
)
from .common import _dump_json

# 中文注释：来源与安装方式的展示名；都是产品名，两种语言下都不用翻译。
_SOURCE_LABELS = {
    "github-release": "GitHub Release",
    "github-tag": "GitHub tags",
    "pypi": "PyPI",
}


def _checker(
    args: argparse.Namespace,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    enabled: bool = True,
) -> UpdateChecker:
    """按命令行参数构造检查器；状态目录决定缓存落盘位置。"""

    return UpdateChecker(
        state_dir=getattr(args, "state_dir", None),
        timeout=timeout,
        enabled=enabled,
    )


def _format_moment(value: object) -> str:
    """把 Unix 时间格式化为本地时间；没有值时返回未知。"""

    if value is None:
        return "未知"
    try:
        return to_local(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "未知"


def _print_snapshot(snapshot: dict, *, notes: bool = False) -> None:
    """打印人类可读的检查结果。"""

    current = snapshot.get("current_version") or "未知"
    latest = snapshot.get("latest_version") or current
    print(f"当前版本: {current}")
    print(f"最新版本: {latest}")
    error = snapshot.get("last_error")
    if error:
        # 中文注释：这一次检查失败（离线、限流）时如实说明；如果之前成功过，
        # 下面的最新版本来自上次成功的结果，仍然有效。
        print(f"检查更新: 失败（{error}）")
    if snapshot.get("update_available"):
        print("状态: 发现新版本")
    elif snapshot.get("checked_at") or not error:
        print("状态: 已是最新版本")
    source = snapshot.get("release_source")
    if snapshot.get("update_available") or snapshot.get("checked_at"):
        print(
            "发布来源: "
            + (_SOURCE_LABELS.get(str(source), str(source)) if source else "未知")
        )
    if snapshot.get("published_at"):
        print(f"发布时间: {_format_moment(snapshot['published_at'])}")
    url = snapshot.get("release_url")
    if url:
        print(f"发布说明: {url}")
    if snapshot.get("checked_at"):
        print(f"上次检查: {_format_moment(snapshot['checked_at'])}")
    if snapshot.get("update_available"):
        upgrade = snapshot.get("upgrade") or {}
        print(f"升级方式: {upgrade.get('label') or '未知'}")
        print(f"升级命令: {upgrade.get('command') or ''}")
        print(f"升级后重启: {upgrade.get('restart_command') or ''}")
        if notes and snapshot.get("notes"):
            print("")
            print(str(snapshot["notes"]))


def _show_update(args: argparse.Namespace) -> int:
    """``update`` 子命令：显式检查一次，必要时执行升级。"""

    # 中文注释：显式命令不受「自动检查」开关影响，否则用户无法手动排查；
    # --cached 只读缓存，用于脚本或离线环境。
    checker = _checker(args, timeout=getattr(args, "timeout", DEFAULT_TIMEOUT))
    snapshot = (
        checker.snapshot()
        if getattr(args, "cached", False)
        else checker.refresh(force=True)
    )
    if args.json:
        print(_dump_json(snapshot))
    else:
        _print_snapshot(snapshot, notes=bool(getattr(args, "notes", False)))
    if not getattr(args, "upgrade", False):
        return 0
    return _run_upgrade(snapshot, assume_yes=bool(getattr(args, "yes", False)))


def _run_upgrade(snapshot: dict, *, assume_yes: bool) -> int:
    """执行升级命令；非交互环境必须显式 ``--yes``。"""

    upgrade = snapshot.get("upgrade") or {}
    command = str(upgrade.get("command") or "")
    restart = str(upgrade.get("restart_command") or "")
    if not command:
        print("升级失败: 没有可用的升级命令")
        return 2
    if not assume_yes:
        if not sys.stdin.isatty():
            print("非交互环境：请加 --yes 确认执行升级命令。")
            return 2
        print(f"即将执行: {command}")
        # 中文注释：提示语自己 print（走翻译后的 stdout），不用 input 的 prompt——
        # 真实终端里 input 的提示直接写到 fd，绕过 CLI 的翻译层。
        print("确认升级？[y/N] ", end="", flush=True)
        answer = input().strip().lower()
        if answer not in {"y", "yes"}:
            print("已取消升级。")
            return 0
    print(f"执行: {command}")
    try:
        code = subprocess.run(command, shell=True).returncode
    except OSError as error:
        print(f"升级失败: {error}")
        return 2
    if code != 0:
        print(f"升级失败: 升级命令退出码 {code}")
        return 2
    print(f"升级完成，请重启监控服务后生效: {restart}")
    return 0


def _notify_update(args: argparse.Namespace, *, allow_network: bool) -> None:
    """命令执行前后打印一次更新提醒；只对有新版本且未提醒过的情况输出。"""

    if getattr(args, "command", "") == "update":
        return
    if checks_disabled() or getattr(args, "no_update_check", False):
        return
    checker = _checker(args, timeout=NOTICE_TIMEOUT)
    # 中文注释：非交互环境（脚本、CI、后台服务）不做同步抓取，避免拖慢调用。
    if allow_network and checker.should_refresh() and sys.stderr.isatty():
        checker.refresh()
    pending = checker.pending_notice()
    if pending is None:
        return
    latest = str(pending.get("latest_version") or "")
    current = str(pending.get("current_version") or "")
    print(
        f"发现新版本 v{latest}（当前 v{current}）。"
        "运行 a-token-monitor update --upgrade 升级，"
        "或用 --no-update-check 关闭提醒。",
        file=sys.stderr,
    )
    checker.mark_notified(latest)
