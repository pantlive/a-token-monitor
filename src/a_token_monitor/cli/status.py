"""status 子命令：查看历史状态，不调用 Codex。"""

from __future__ import annotations

import sys

from ..models import JobState
from ..quota import (
    QuotaWindow,
    quota_period,
    quota_period_label,
    quota_window_duration,
)
from ..storage import StateStore
from .common import (
    _dump_json,
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
