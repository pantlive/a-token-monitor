"""Codex 账户额度快照和阻塞窗口判定。

额度数据来自 Codex App Server 的 ``account/rateLimits/read`` 响应。
本模块只做纯数据解析和判定，不发起网络请求，也不启动模型任务。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


def _number(value: Any) -> float | None:
    """把服务端数字字段转换为浮点数。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> float | None:
    """解析服务端返回的 Unix 秒或兼容的 ISO 时间。"""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        return timestamp / 1000 if timestamp > 10_000_000_000 else timestamp
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            timestamp = float(stripped)
            return timestamp / 1000 if timestamp > 10_000_000_000 else timestamp
        except ValueError:
            try:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    """只接受 JSON 对象，避免解析时把异常值当成窗口。"""

    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class QuotaWindow:
    """一个账户额度窗口，例如 primary 或 secondary。"""

    limit_id: str
    name: str
    used_percent: float | None
    window_minutes: float | None
    resets_at: float | None
    reached_type: str | None = None

    @property
    def is_exhausted(self) -> bool:
        """判断窗口是否已经被服务端标记为不可用。"""

        if self.reached_type not in {None, "", "none", "null", "false"}:
            return True
        return self.used_percent is not None and self.used_percent >= 100.0


# 中文注释：额度周期统一口径。上游的名字五花八门（primary / secondary / weekly /
# limit_month_total / 5-hour / monthly），面板要用同一套行渲染不同订阅，所以统一按
# 「窗口时长优先、名字兜底」归类成有限几种周期。
QUOTA_PERIOD_ORDER = ("five_hours", "day", "week", "month", "other")
QUOTA_PERIOD_LABELS = {
    "five_hours": "5 小时",
    "day": "日",
    "week": "周",
    "month": "月",
    "other": "其它",
}
# 面板固定展示的行与顺序：5 小时 → 周 → 月；缺的周期显示「不适用」。
QUOTA_DISPLAY_PERIODS = ("five_hours", "week", "month")
_QUOTA_PERIOD_BY_MINUTES = (
    (6 * 60.0, "five_hours"),
    (36 * 60.0, "day"),
    (10 * 1440.0, "week"),
)
_QUOTA_PERIOD_NAME_HINTS = (
    ("5-hour", "five_hours"),
    ("5_hour", "five_hours"),
    ("5h", "five_hours"),
    ("hour", "five_hours"),
    ("daily", "day"),
    ("day", "day"),
    ("week", "week"),
    ("month", "month"),
)


def quota_period(window: QuotaWindow) -> str:
    """把一个额度窗口归到统一周期（``QUOTA_PERIOD_ORDER`` 之一）。

    时长可读时按分钟归类（≤6 小时算 5 小时窗口、≤36 小时算日、≤10 天算周、
    更长算月）；上游没给时长时退回窗口名字里的关键词（``monthly``、
    ``limit_month_total``、``Weekly``、``5-hour`` 等），都判不出来才落到 ``other``。
    """

    minutes = window.window_minutes
    if isinstance(minutes, (int, float)) and minutes > 0:
        for limit, period in _QUOTA_PERIOD_BY_MINUTES:
            if minutes <= limit:
                return period
        return "month"
    name = f"{window.name} {window.limit_id}".lower()
    for hint, period in _QUOTA_PERIOD_NAME_HINTS:
        if hint in name:
            return period
    return "other"


def quota_period_label(period: str) -> str:
    """返回周期的中文展示名。"""

    return QUOTA_PERIOD_LABELS.get(period, QUOTA_PERIOD_LABELS["other"])


_QUOTA_PERIOD_DURATION_TEXT = {
    "five_hours": "5 小时",
    "day": "1 天",
    "week": "7 天",
    "month": "1 个月",
    "other": "周期未知",
}


def quota_window_duration(window: QuotaWindow) -> str:
    """返回窗口时长的人话描述；上游没给时长时按周期给个默认说法。"""

    minutes = window.window_minutes
    if isinstance(minutes, (int, float)) and minutes > 0:
        if minutes < 60:
            return f"{minutes:g} 分钟"
        if minutes < 1440:
            return f"{minutes / 60:g} 小时"
        if minutes < 10 * 1440:
            return f"{minutes / 1440:g} 天"
        return f"{minutes / 43200:g} 个月"
    return _QUOTA_PERIOD_DURATION_TEXT.get(
        quota_period(window),
        _QUOTA_PERIOD_DURATION_TEXT["other"],
    )


@dataclass(frozen=True)
class QuotaSnapshot:
    """一次完整的账户额度快照。"""

    observed_at: float
    windows: tuple[QuotaWindow, ...] = ()
    plan_type: str | None = None
    source: str = "app-server"
    raw_limit_ids: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def exhausted_windows(self) -> tuple[QuotaWindow, ...]:
        """返回当前快照中已经耗尽的窗口。"""

        return tuple(window for window in self.windows if window.is_exhausted)

    @property
    def latest_exhausted_reset_at(self) -> float | None:
        """返回所有耗尽窗口中最晚的重置时间。"""

        reset_times = [
            window.resets_at
            for window in self.exhausted_windows
            if window.resets_at is not None
        ]
        return max(reset_times) if reset_times else None

    def window(self, limit_id: str, name: str) -> QuotaWindow | None:
        """按额度桶和窗口名称查找窗口。"""

        for item in self.windows:
            if item.limit_id == limit_id and item.name == name:
                return item
        return None


def _parse_window(
    limit_id: str,
    name: str,
    value: Any,
    reached_type: str | None,
) -> QuotaWindow | None:
    """解析 primary/secondary 窗口。"""

    data = _mapping(value)
    if not data:
        return None
    used_percent = _number(data.get("usedPercent", data.get("used_percent")))
    window_minutes = _number(data.get("windowDurationMins", data.get("window_minutes")))
    resets_at = _timestamp(data.get("resetsAt", data.get("resets_at")))
    if used_percent is None and window_minutes is None and resets_at is None:
        return None
    return QuotaWindow(
        limit_id=limit_id,
        name=name,
        used_percent=used_percent,
        window_minutes=window_minutes,
        resets_at=resets_at,
        reached_type=reached_type,
    )


def _parse_limit_bucket(limit_id: str, value: Any) -> list[QuotaWindow]:
    """解析一个 rate-limits bucket。"""

    data = _mapping(value)
    reached_type = data.get("rateLimitReachedType")
    if not isinstance(reached_type, str):
        reached_type = None
    else:
        reached_type = reached_type.strip().lower() or None
    windows: list[QuotaWindow] = []
    for name in ("primary", "secondary"):
        window = _parse_window(
            limit_id=limit_id,
            name=name,
            value=data.get(name),
            # reached_type 指向具体窗口；不能把 primary 命中传播给
            # secondary，否则额度展示会错误等待更长的窗口。
            reached_type=reached_type if reached_type == name else None,
        )
        if window is not None:
            windows.append(window)
    return windows


def parse_rate_limits_result(
    result: Mapping[str, Any],
    observed_at: float,
    source: str = "app-server",
) -> QuotaSnapshot:
    """解析 ``account/rateLimits/read`` 的 result 对象。

    优先使用 ``rateLimitsByLimitId``，这样不会把向后兼容的单桶视图和
    多桶视图重复计入；旧版本只返回 ``rateLimits`` 时再使用单桶视图。
    """

    by_limit_id = result.get("rateLimitsByLimitId")
    windows: list[QuotaWindow] = []
    limit_ids: list[str] = []
    if isinstance(by_limit_id, Mapping) and by_limit_id:
        for raw_id, bucket in by_limit_id.items():
            limit_id = str(raw_id)
            limit_ids.append(limit_id)
            windows.extend(_parse_limit_bucket(limit_id, bucket))
    else:
        single_bucket = result.get("rateLimits")
        single_data = _mapping(single_bucket)
        limit_id_value = single_data.get("limitId", "codex")
        limit_id = str(limit_id_value or "codex")
        limit_ids.append(limit_id)
        windows.extend(_parse_limit_bucket(limit_id, single_data))

    plan_type = result.get("planType")
    if not isinstance(plan_type, str):
        plan_type = None
    return QuotaSnapshot(
        observed_at=observed_at,
        windows=tuple(windows),
        plan_type=plan_type,
        source=source,
        raw_limit_ids=tuple(limit_ids),
    )


def merge_sparse_update(
    previous: QuotaSnapshot | None,
    update_result: Mapping[str, Any],
    observed_at: float,
) -> QuotaSnapshot:
    """合并 ``account/rateLimits/updated`` 的稀疏更新。

    更新通知可能只带 primary，因此不能用空字段覆盖旧的 secondary。
    """

    if previous is None:
        return parse_rate_limits_result(
            update_result,
            observed_at=observed_at,
            source="app-server-notification",
        )

    update_snapshot = parse_rate_limits_result(
        update_result,
        observed_at=observed_at,
        source="app-server-notification",
    )
    merged: dict[tuple[str, str], QuotaWindow] = {
        (window.limit_id, window.name): window for window in previous.windows
    }
    for window in update_snapshot.windows:
        merged[(window.limit_id, window.name)] = window
    return QuotaSnapshot(
        observed_at=observed_at,
        windows=tuple(merged.values()),
        plan_type=update_snapshot.plan_type or previous.plan_type,
        source="app-server-notification",
        raw_limit_ids=tuple(
            dict.fromkeys(previous.raw_limit_ids + update_snapshot.raw_limit_ids)
        ),
        metadata=dict(previous.metadata),
    )
