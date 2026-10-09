"""本地时区的唯一入口：时间戳与本地日历之间的换算都经过这里。

默认跟随系统时区（与 ``datetime.astimezone()`` 一致）。测试可以用
``set_local_timezone`` 注入固定时区：``time.tzset`` 只在 POSIX 上可用，
Windows 上改不了进程时区，注入是让「今天」「按天汇总」等结果在任何开发机上
都可复现的唯一可靠方式。

本地零点一律从日期重新换算成时间戳，而不是对当前时刻 ``replace(hour=0)``：
后者沿用当前的 UTC 偏移，夏令时切换当天会把日界线算偏一小时。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo

_override: tzinfo | None = None


def set_local_timezone(zone: tzinfo | None) -> None:
    """固定本地时区；传 ``None`` 恢复跟随系统时区。"""

    global _override
    _override = zone


def to_local(timestamp: float) -> datetime:
    """把 Unix 时间戳转换为带时区的本地时间。"""

    if _override is None:
        return datetime.fromtimestamp(float(timestamp)).astimezone()
    return datetime.fromtimestamp(float(timestamp), tz=_override)


def local_naive_to_timestamp(naive: datetime) -> float:
    """把不带时区、按本地时间书写的时刻（日志里的墙上时间）转换为时间戳。"""

    if _override is None:
        return naive.astimezone().timestamp()
    return naive.replace(tzinfo=_override).timestamp()


def local_date_start(day: date) -> float:
    """本地某一天 0 点的时间戳。"""

    return local_naive_to_timestamp(datetime.combine(day, time.min))


def local_date_end(day: date) -> float:
    """本地某一天最后一刻的时间戳（次日 0 点前 1 微秒）。"""

    return local_date_start(day + timedelta(days=1)) - 1e-6


def local_day_start(timestamp: float, days_ago: int = 0) -> float:
    """``timestamp`` 所在本地日（再往前 ``days_ago`` 天）的 0 点时间戳。"""

    return local_date_start(to_local(timestamp).date() - timedelta(days=days_ago))


def local_day_key(timestamp: float) -> str:
    """把时间戳格式化为本地自然日 ``YYYY-MM-DD``。"""

    return to_local(timestamp).strftime("%Y-%m-%d")
