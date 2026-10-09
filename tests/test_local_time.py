"""本地时区入口：注入时区与夏令时切换日的日界线。"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from a_token_monitor import local_time
from a_token_monitor.local_time import (
    local_date_end,
    local_date_start,
    local_day_key,
    local_day_start,
    local_naive_to_timestamp,
    set_local_timezone,
    to_local,
)

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python 3.10 以上都有 zoneinfo
    ZoneInfo = None  # type: ignore[assignment]


def _los_angeles():
    """美西时区；Windows 没装 tzdata 时拿不到 IANA 时区，用例跳过。"""

    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo("America/Los_Angeles")
    except ZoneInfoNotFoundError:
        return None


class LocalTimeTests(unittest.TestCase):
    def setUp(self) -> None:
        previous = local_time._override
        self.addCleanup(set_local_timezone, previous)

    def test_injected_timezone_drives_day_boundaries(self) -> None:
        set_local_timezone(timezone(timedelta(hours=8)))
        moment = datetime(2026, 8, 26, 17, 0, tzinfo=timezone.utc).timestamp()

        self.assertEqual(local_day_key(moment), "2026-08-27")
        self.assertEqual(
            local_day_start(moment),
            datetime(2026, 8, 26, 16, 0, tzinfo=timezone.utc).timestamp(),
        )
        self.assertEqual(to_local(moment).utcoffset(), timedelta(hours=8))

        set_local_timezone(timezone(timedelta(hours=-7)))
        self.assertEqual(local_day_key(moment), "2026-08-26")

    def test_naive_wall_clock_is_read_in_local_timezone(self) -> None:
        set_local_timezone(timezone(timedelta(hours=8)))

        self.assertEqual(
            local_naive_to_timestamp(datetime(2026, 8, 27, 1, 0)),
            datetime(2026, 8, 26, 17, 0, tzinfo=timezone.utc).timestamp(),
        )

    def test_dst_day_boundaries_follow_the_zone_not_the_current_offset(self) -> None:
        zone = _los_angeles()
        if zone is None:
            self.skipTest("系统缺少 IANA 时区数据")
        set_local_timezone(zone)
        # 中文注释：2026-03-08 美西进入夏令时，当天只有 23 小时；若沿用当前时刻的
        # UTC 偏移去 replace(hour=0)，零点会偏一小时。
        spring = date(2026, 3, 8)
        self.assertEqual(local_date_end(spring) + 1e-6 - local_date_start(spring), 23 * 3600)
        evening = datetime(2026, 3, 8, 20, 0, tzinfo=zone).timestamp()
        self.assertEqual(
            local_day_start(evening),
            datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc).timestamp(),
        )
        self.assertEqual(
            local_day_start(evening, days_ago=1),
            datetime(2026, 3, 7, 8, 0, tzinfo=timezone.utc).timestamp(),
        )

    def test_none_restores_system_timezone(self) -> None:
        set_local_timezone(None)
        moment = 1_788_000_000.0

        self.assertEqual(to_local(moment), datetime.fromtimestamp(moment).astimezone())


if __name__ == "__main__":
    unittest.main()
