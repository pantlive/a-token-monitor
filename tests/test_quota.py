"""App Server 额度快照解析测试。"""

from __future__ import annotations

import unittest

from token_monitor.quota import (
    merge_sparse_update,
    parse_rate_limits_result,
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
