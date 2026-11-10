"""验证各 provider 的每日基线持久化与隔离。"""

from __future__ import annotations

import sqlite3
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from a_token_monitor.local_time import local_day_key, local_day_start
from a_token_monitor.provider_quota_baseline import (
    ProviderQuotaBaselineError,
    ProviderQuotaBaselineStore,
)
from a_token_monitor.quota import QuotaDayBaseline, QuotaSnapshot, QuotaWindow
from _platform_support import requires_chmod


class ProviderQuotaBaselineTests(unittest.TestCase):
    """覆盖日切、周期重置、乱序快照和数据库保护。"""

    NOW = local_day_start(1_800_000_000.0) + 3600.0
    KEY = ("shared", "primary")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ProviderQuotaBaselineStore(self.root / "state")

    def snapshot(
        self, used: float | None = 10.0, observed_at: float = NOW
    ) -> QuotaSnapshot:
        """构建起点在昨天的窗口，排除今日重置的特殊规则。"""

        return QuotaSnapshot(
            observed_at=observed_at,
            windows=(QuotaWindow(*self.KEY, used, 300.0, self.NOW + 3600.0),),
        )

    def observe(self, snapshot: QuotaSnapshot) -> QuotaDayBaseline:
        """读取测试账号的主窗口基线。"""

        return self.store.observe("claude", "account-a", snapshot)[self.KEY]

    def test_first_observation_and_restart(self) -> None:
        baseline = self.observe(self.snapshot())
        self.assertEqual(baseline.used_percent, 10.0)
        self.assertEqual(baseline.day_key, local_day_key(self.NOW))
        reopened = ProviderQuotaBaselineStore(self.store.state_dir)
        self.assertEqual(
            reopened.observe("claude", "account-a", self.snapshot(22.0))[
                self.KEY
            ],
            baseline,
        )

    def test_growth_and_repeated_observations_do_not_write(self) -> None:
        baseline = self.observe(self.snapshot())
        # 中文注释：SQLite 的 data_version 可验证另一个连接没有提交实际写入。
        with sqlite3.connect(self.store.database_file) as connection:
            version = connection.execute("PRAGMA data_version").fetchone()
            for used in (10.0, 22.0, 30.0):
                self.assertEqual(
                    self.observe(self.snapshot(used, self.NOW + 600.0)),
                    baseline,
                )
            self.assertEqual(
                connection.execute("PRAGMA data_version").fetchone(), version
            )

    def test_next_local_day_rebuilds_baseline(self) -> None:
        self.observe(self.snapshot())
        tomorrow = local_day_start(self.NOW, days_ago=-1) + 3600.0
        baseline = self.observe(self.snapshot(22.0, tomorrow))
        self.assertEqual(baseline.used_percent, 22.0)
        self.assertEqual(baseline.day_key, local_day_key(tomorrow))

    def test_changed_reset_starts_at_zero(self) -> None:
        self.observe(self.snapshot())
        snapshot = self.snapshot(22.0, self.NOW + 60.0)
        snapshot = replace(
            snapshot,
            windows=(
                replace(snapshot.windows[0], resets_at=self.NOW + 1800.0),
            ),
        )
        self.assertEqual(self.observe(snapshot).used_percent, 0.0)

    def test_window_started_today_starts_at_zero(self) -> None:
        snapshot = self.snapshot()
        snapshot = replace(
            snapshot,
            windows=(
                replace(snapshot.windows[0], resets_at=self.NOW + 5 * 3600.0),
            ),
        )
        self.assertEqual(self.observe(snapshot).used_percent, 0.0)

    def test_decreased_usage_rebuilds_baseline(self) -> None:
        self.observe(self.snapshot())
        baseline = self.observe(self.snapshot(5.0, self.NOW + 60.0))
        self.assertEqual(baseline.used_percent, 5.0)

    def test_old_snapshot_does_not_downgrade_baseline(self) -> None:
        baseline = self.observe(self.snapshot())
        self.assertEqual(
            self.observe(self.snapshot(5.0, self.NOW - 60.0)), baseline
        )
        yesterday = local_day_start(self.NOW) - 60.0
        self.assertEqual(
            self.store.observe(
                "claude", "account-a", self.snapshot(5.0, yesterday)
            ),
            {},
        )
        self.assertEqual(self.observe(self.snapshot()), baseline)

    def test_products_accounts_and_windows_are_isolated(self) -> None:
        for product, account, used in (
            ("claude", "account-a", 10.0),
            ("grok", "account-a", 30.0),
            ("claude", "account-b", 50.0),
        ):
            with self.subTest(product=product, account=account):
                snapshot = self.snapshot(used)
                snapshot = replace(
                    snapshot,
                    windows=(
                        *snapshot.windows,
                        replace(
                            snapshot.windows[0],
                            name="secondary",
                            used_percent=2.0,
                        ),
                    ),
                )
                self.store.observe(product, account, snapshot)
                baselines = self.store.observe(
                    product, account, self.snapshot(60.0)
                )
                self.assertEqual(baselines[self.KEY].used_percent, used)
                self.assertEqual(
                    baselines[("shared", "secondary")].used_percent, 2.0
                )
        self.assertEqual(self.observe(self.snapshot()).used_percent, 10.0)

    def test_missing_usage_does_not_create_or_replace_baseline(self) -> None:
        self.assertEqual(
            self.store.observe("claude", "account-a", self.snapshot(None)), {}
        )
        baseline = self.observe(self.snapshot())
        self.assertEqual(self.observe(self.snapshot(None)), baseline)

    @requires_chmod
    def test_database_permissions(self) -> None:
        self.assertEqual(
            stat.S_IMODE(self.store.state_dir.stat().st_mode), 0o700
        )
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.store._protect_database_files()
            for suffix in ("", "-wal", "-shm"):
                path = self.store.database_file.with_name(
                    self.store.database_file.name + suffix
                )
                self.assertTrue(path.exists())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_initialization_error_is_wrapped(self) -> None:
        path = self.root / "file"
        path.touch()
        with self.assertRaises(ProviderQuotaBaselineError):
            ProviderQuotaBaselineStore(path)

    def test_sqlite_errors_are_wrapped(self) -> None:
        with (
            mock.patch(
                "a_token_monitor.provider_quota_baseline.sqlite3.connect",
                side_effect=sqlite3.OperationalError("模拟不可用"),
            ),
            self.assertRaises(ProviderQuotaBaselineError),
        ):
            self.observe(self.snapshot())
