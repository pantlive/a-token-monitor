"""按本地日历日和滚动窗口切分统计时间段。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta



def _local_day(timestamp: float) -> str:
    """把时间戳格式化为本地自然日，与用量趋势的日期口径一致。"""

    return (
        datetime.fromtimestamp(float(timestamp))
        .astimezone()
        .strftime("%Y-%m-%d")
    )


def calendar_day_start(now: float | None = None) -> float:
    """本地时区「今天 0 点」的时间戳，供各处的「今天」筛选共用。"""

    return _period_start("calendar_day", time.time() if now is None else float(now))


def _period_start(period_kind: str, now: float) -> float:
    """计算本地时区的今天或滚动时间窗口起点。"""

    if period_kind == "calendar_day":
        local_now = datetime.fromtimestamp(now).astimezone()
        local_start = local_now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return local_start.timestamp()
    days = {
        "seven_days": 7,
        "thirty_days": 30,
        "three_hundred_sixty_five_days": 365,
    }
    try:
        duration = timedelta(days=days[period_kind])
    except KeyError as error:
        raise ValueError(f"未知用量时间窗口: {period_kind}") from error
    return now - duration.total_seconds()
