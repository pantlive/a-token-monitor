"""App Server 额度快照解析测试。"""

from __future__ import annotations

import unittest

from a_token_monitor.local_time import local_day_key, local_day_start
from a_token_monitor.quota import (
    QUOTA_DISPLAY_PERIODS,
    QuotaDayBaseline,
    QuotaProjection,
    QuotaWindow,
    merge_sparse_update,
    parse_rate_limits_result,
    project_quota_exhaustion,
    quota_period,
    quota_period_label,
    quota_window_duration,
    resolve_quota_day_baseline,
)


class QuotaParserTests(unittest.TestCase):
    """验证多额度桶、精确 reset 时间和稀疏通知合并。"""

    def test_parses_multiple_limit_buckets_without_duplicate_compat_view(self) -> None:
        snapshot = parse_rate_limits_result(
            {
                "planType": "plus",
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {
                        "usedPercent": 64,
                        "windowDurationMins": 300,
                        "resetsAt": 1_787_743_877,
                    },
                },
                "rateLimitsByLimitId": {
                    "codex": {
                        "primary": {
                            "usedPercent": 64,
                            "windowDurationMins": 300,
                            "resetsAt": 1_787_743_877,
                        },
                        "secondary": {
                            "usedPercent": 20,
                            "windowDurationMins": 10080,
                            "resetsAt": 1_788_312_607,
                        },
                    },
                    "other": {
                        "primary": {
                            "usedPercent": 5,
                            "windowDurationMins": 300,
                            "resetsAt": "2026-08-26T08:00:00Z",
                        }
                    },
                },
            },
            observed_at=1_787_727_000,
        )

        self.assertEqual(snapshot.plan_type, "plus")
        self.assertEqual(len(snapshot.windows), 3)
        self.assertEqual(snapshot.window("codex", "primary").resets_at, 1_787_743_877)
        self.assertEqual(snapshot.window("codex", "secondary").window_minutes, 10080)
        self.assertFalse(snapshot.window("codex", "primary").is_exhausted)

    def test_merges_sparse_notification_without_erasing_secondary_window(self) -> None:
        previous = parse_rate_limits_result(
            {
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {
                        "usedPercent": 99,
                        "windowDurationMins": 300,
                        "resetsAt": 200,
                    },
                    "secondary": {
                        "usedPercent": 40,
                        "windowDurationMins": 10080,
                        "resetsAt": 500,
                    },
                }
            },
            observed_at=100,
        )
        updated = merge_sparse_update(
            previous,
            {
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {
                        "usedPercent": 100,
                        "windowDurationMins": 300,
                        "resetsAt": 220,
                    },
                    "rateLimitReachedType": "primary",
                }
            },
            observed_at=120,
        )

        self.assertEqual(updated.window("codex", "primary").used_percent, 100)
        self.assertEqual(updated.window("codex", "primary").resets_at, 220)
        self.assertEqual(updated.window("codex", "secondary").used_percent, 40)
        self.assertEqual(updated.latest_exhausted_reset_at, 220)

    def test_reached_type_only_exhausts_the_targeted_window(self) -> None:
        snapshot = parse_rate_limits_result(
            {
                "rateLimits": {
                    "limitId": "codex",
                    "rateLimitReachedType": "primary",
                    "primary": {
                        "usedPercent": 100,
                        "windowDurationMins": 300,
                        "resetsAt": 200,
                    },
                    "secondary": {
                        "usedPercent": 20,
                        "windowDurationMins": 10080,
                        "resetsAt": 500,
                    },
                }
            },
            observed_at=100,
        )

        self.assertTrue(snapshot.window("codex", "primary").is_exhausted)
        self.assertFalse(snapshot.window("codex", "secondary").is_exhausted)
        self.assertEqual(snapshot.latest_exhausted_reset_at, 200)

    def test_accepts_unix_millisecond_reset_timestamp(self) -> None:
        snapshot = parse_rate_limits_result(
            {
                "rateLimits": {
                    "primary": {
                        "usedPercent": 10,
                        "windowDurationMins": 300,
                        "resetsAt": 1_787_743_877_000,
                    }
                }
            },
            observed_at=1_787_727_000,
        )

        self.assertEqual(snapshot.window("codex", "primary").resets_at, 1_787_743_877)


if __name__ == "__main__":
    unittest.main()


class QuotaPeriodTests(unittest.TestCase):
    """验证不同订阅的额度窗口被归到统一周期（面板固定行的基础）。"""

    # 上游真实取值：provider / 窗口名 / 时长 / 期望周期
    CASES = (
        ("codex", "primary", 300.0, "five_hours"),
        ("codex", "secondary", 10_080.0, "week"),
        ("grok", "weekly", 10_080.0, "week"),
        ("kimi", "limit_5h", 300.0, "five_hours"),
        ("kimi", "limit_month_total", None, "month"),
        ("kimi", "limit_month_code", None, "month"),
        ("command-code", "5-hour", 300.0, "five_hours"),
        ("command-code", "Weekly", 10_080.0, "week"),
        ("command-code", "monthly", None, "month"),
        # 时长优先于名字：名字叫 daily 但实际是 5 小时窗口的按时长算。
        ("x", "daily", 300.0, "five_hours"),
        ("x", "daily", 1440.0, "day"),
        ("x", "whatever", 43_200.0, "month"),
        # 名字兜底与判不出来的情况
        ("x", "mystery", None, "other"),
    )

    def test_classifies_every_real_window(self) -> None:
        for limit_id, name, minutes, expected in self.CASES:
            window = QuotaWindow(
                limit_id=limit_id,
                name=name,
                used_percent=1.0,
                window_minutes=minutes,
                resets_at=None,
            )
            with self.subTest(limit_id=limit_id, name=name, minutes=minutes):
                self.assertEqual(quota_period(window), expected)

    def test_fixed_rows_are_the_three_canonical_periods(self) -> None:
        self.assertEqual(QUOTA_DISPLAY_PERIODS, ("five_hours", "week", "month"))
        self.assertEqual(quota_period_label("five_hours"), "5 小时")
        self.assertEqual(quota_period_label("week"), "周")
        self.assertEqual(quota_period_label("month"), "月")

    def test_duration_text_never_shows_a_naked_dash(self) -> None:
        """上游没给时长（Kimi / Command Code 的月窗口）也要给出人话。"""

        with_minutes = QuotaWindow("codex", "primary", 2.0, 300.0, None)
        month_without_minutes = QuotaWindow("kimi", "limit_month_total", 17.9, None, None)
        unknown = QuotaWindow("x", "mystery", 1.0, None, None)

        self.assertEqual(quota_window_duration(with_minutes), "5 小时")
        self.assertEqual(quota_window_duration(month_without_minutes), "1 个月")
        self.assertEqual(quota_window_duration(unknown), "周期未知")


class QuotaProjectionTests(unittest.TestCase):
    """验证按本周期平均速度预测额度耗尽时间。

    固定用 5 小时窗口（18000 秒）、重置时刻 20000、观察时刻 11000：
    窗口从 2000 开始，已经过 9000 秒，数据足够做线性预测。
    """

    OBSERVED_AT = 11_000.0
    RESETS_AT = 20_000.0

    def window(
        self,
        used_percent: float | None = 50.0,
        window_minutes: float | None = 300.0,
        resets_at: float | None = 20_000.0,
    ) -> QuotaWindow:
        return QuotaWindow(
            limit_id="codex",
            name="primary",
            used_percent=used_percent,
            window_minutes=window_minutes,
            resets_at=resets_at,
        )

    def test_predicts_exhaustion_before_reset(self) -> None:
        # 已用 60%，按当前速度还能用 6000 秒；重置在 9000 秒后，所以会先耗尽。
        projection = project_quota_exhaustion(
            self.window(used_percent=60.0),
            observed_at=self.OBSERVED_AT,
        )

        self.assertIsInstance(projection, QuotaProjection)
        self.assertEqual(projection.status, "before_reset")
        self.assertEqual(projection.projected_exhausted_at, 17_000.0)
        self.assertEqual(projection.remaining_seconds, 6_000.0)

    def test_predicts_quota_lasts_until_reset(self) -> None:
        # 已用 40%，按当前速度还能用 13500 秒，超过距离重置的 9000 秒。
        projection = project_quota_exhaustion(
            self.window(used_percent=40.0),
            observed_at=self.OBSERVED_AT,
        )

        self.assertEqual(projection.status, "after_reset")
        self.assertEqual(projection.projected_exhausted_at, 24_500.0)
        self.assertEqual(projection.remaining_seconds, 13_500.0)

    def test_exhaustion_exactly_at_reset_counts_as_after_reset(self) -> None:
        # 已用 50%、时间过半时，预计耗尽时刻正好等于重置时刻，算能撑到重置。
        projection = project_quota_exhaustion(
            self.window(used_percent=50.0),
            observed_at=self.OBSERVED_AT,
        )

        self.assertEqual(projection.status, "after_reset")
        self.assertEqual(projection.projected_exhausted_at, self.RESETS_AT)

    def test_exhausted_window_has_no_forecast(self) -> None:
        for window in (
            self.window(used_percent=100.0),
            self.window(used_percent=120.0),
            QuotaWindow(
                limit_id="codex",
                name="primary",
                used_percent=80.0,
                window_minutes=300.0,
                resets_at=self.RESETS_AT,
                reached_type="primary",
            ),
        ):
            with self.subTest(window=window):
                projection = project_quota_exhaustion(
                    window,
                    observed_at=self.OBSERVED_AT,
                )
                self.assertEqual(projection.status, "exhausted")
                self.assertEqual(
                    projection.projected_exhausted_at,
                    self.OBSERVED_AT,
                )
                self.assertEqual(projection.remaining_seconds, 0.0)

    def test_missing_or_invalid_fields_are_unknown(self) -> None:
        cases = {
            "缺已用百分比": self.window(used_percent=None),
            "缺重置时间": self.window(resets_at=None),
            "缺窗口时长": self.window(window_minutes=None),
            "窗口时长为零": self.window(window_minutes=0.0),
            "已用百分比为零": self.window(used_percent=0.0),
            "已用百分比为负": self.window(used_percent=-1.0),
            "重置时间已过": self.window(resets_at=self.OBSERVED_AT),
        }
        for name, window in cases.items():
            with self.subTest(name=name):
                projection = project_quota_exhaustion(
                    window,
                    observed_at=self.OBSERVED_AT,
                )
                self.assertEqual(projection.status, "unknown")
                self.assertIsNone(projection.projected_exhausted_at)
                self.assertIsNone(projection.remaining_seconds)

    def test_window_started_less_than_ten_minutes_ago_is_unknown(self) -> None:
        # 窗口从 2000 开始，观察时刻 2500 时只过了 500 秒，速率噪声太大。
        projection = project_quota_exhaustion(
            self.window(used_percent=10.0),
            observed_at=2_500.0,
        )

        self.assertEqual(projection.status, "unknown")
        self.assertIsNone(projection.projected_exhausted_at)

    def test_uses_observed_at_instead_of_the_system_clock(self) -> None:
        # 同一份窗口数据换一个观察时刻，预测会随之变化，说明没有偷偷读系统时钟。
        early = project_quota_exhaustion(
            self.window(used_percent=60.0),
            observed_at=self.OBSERVED_AT,
        )
        late = project_quota_exhaustion(
            self.window(used_percent=60.0),
            observed_at=12_000.0,
        )

        self.assertEqual(early.projected_exhausted_at, 17_000.0)
        self.assertIsNotNone(late.projected_exhausted_at)
        self.assertGreater(
            late.projected_exhausted_at,
            early.projected_exhausted_at,
        )


class QuotaDayBaselineTests(unittest.TestCase):
    """验证今日额度基线的七种分支（纯函数，不访问数据库）。"""

    OBSERVED_AT = local_day_start(1_800_000_000.0) + 3600.0

    def window(
        self,
        used_percent: float | None = 22.0,
        resets_at: float | None = None,
    ) -> QuotaWindow:
        return QuotaWindow(
            limit_id="codex",
            name="primary",
            used_percent=used_percent,
            window_minutes=300.0,
            resets_at=self.OBSERVED_AT + 3600.0 if resets_at is None else resets_at,
        )

    def baseline(self, used_percent: float = 10.0, **overrides: object) -> QuotaDayBaseline:
        values: dict[str, object] = {
            "day_key": local_day_key(self.OBSERVED_AT),
            "used_percent": used_percent,
            "resets_at": self.OBSERVED_AT + 3600.0,
            "observed_at": self.OBSERVED_AT - 600.0,
        }
        values.update(overrides)
        return QuotaDayBaseline(**values)  # type: ignore[arg-type]

    def test_missing_used_percent_has_no_baseline(self) -> None:
        self.assertIsNone(
            resolve_quota_day_baseline(
                self.window(used_percent=None),
                self.OBSERVED_AT,
                None,
            )
        )

    def test_without_previous_uses_current_used_percent(self) -> None:
        self.assertEqual(
            resolve_quota_day_baseline(self.window(), self.OBSERVED_AT, None),
            22.0,
        )

    def test_keeps_previous_baseline_within_the_same_day(self) -> None:
        self.assertEqual(
            resolve_quota_day_baseline(
                self.window(),
                self.OBSERVED_AT,
                self.baseline(10.0),
            ),
            10.0,
        )

    def test_new_local_day_starts_from_current_used_percent(self) -> None:
        self.assertEqual(
            resolve_quota_day_baseline(
                self.window(),
                self.OBSERVED_AT,
                self.baseline(
                    10.0,
                    day_key=local_day_key(self.OBSERVED_AT - 86400.0),
                ),
            ),
            22.0,
        )

    def test_window_started_today_starts_from_zero(self) -> None:
        # 窗口起点正好是本地零点：今天之前不可能有消耗，基线必须是 0。
        resets_at = local_day_start(self.OBSERVED_AT) + 300.0 * 60.0
        self.assertEqual(
            resolve_quota_day_baseline(
                self.window(resets_at=resets_at),
                self.OBSERVED_AT,
                self.baseline(10.0, resets_at=resets_at),
            ),
            0.0,
        )

    def test_reset_window_starts_from_zero(self) -> None:
        self.assertEqual(
            resolve_quota_day_baseline(
                self.window(resets_at=self.OBSERVED_AT + 2000.0),
                self.OBSERVED_AT,
                self.baseline(10.0, resets_at=self.OBSERVED_AT + 1000.0),
            ),
            0.0,
        )

    def test_falling_used_percent_moves_the_baseline_down(self) -> None:
        # 上游回退时不能沿用旧基线，否则会算出虚假的一大段今日消耗。
        self.assertEqual(
            resolve_quota_day_baseline(
                self.window(used_percent=20.0),
                self.OBSERVED_AT,
                self.baseline(30.0),
            ),
            20.0,
        )
