"""App Server 额度快照解析测试。"""

from __future__ import annotations

import unittest

from a_token_monitor.quota import (
    QUOTA_DISPLAY_PERIODS,
    QuotaWindow,
    merge_sparse_update,
    parse_rate_limits_result,
    quota_period,
    quota_period_label,
    quota_window_duration,
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
