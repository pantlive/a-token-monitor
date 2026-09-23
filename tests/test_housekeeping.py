"""agent 目录占用统计与会话归档/清理测试。"""

from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from token_monitor.housekeeping import (
    AuditTarget,
    CleanupCriteria,
    DiskThresholds,
    HousekeepingError,
    HousekeepingMonitor,
    empty_housekeeping_report,
)


_MIB = 1024 * 1024


def _old_session(
    home: Path,
    day: str,
    session_id: str,
    size: int,
    days_old: float,
) -> Path:
    """写入一个形状正确的旧 Codex session 文件。"""

    path = (
        home
        / "sessions"
        / day[:4]
        / day[4:6]
        / day[6:]
        / f"rollout-{day[:4]}-{day[4:6]}-{day[6:]}T01-00-00-{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = time.time() - days_old * 86_400
    os.utime(path, (stamp, stamp))
    return path


def _monitor(
    root: Path,
    home: Path,
    *,
    active: set[str] | None = None,
    thresholds: DiskThresholds | None = None,
) -> HousekeepingMonitor:
    """构造一个以临时目录为目标的管家。"""

    return HousekeepingMonitor(
        targets=(
            AuditTarget(
                label="Codex (codex)",
                product="codex",
                path=home,
                sessions_root=home / "sessions",
            ),
        ),
        thresholds=thresholds or DiskThresholds.from_gb(
            single_warn_gb=1.0,
            total_warn_gb=2.0,
        ),
        archive_dir=root / "archives",
        active_paths=(lambda: set(active or set())),
    )


class DiskScanTests(unittest.TestCase):
    """验证目录占用统计和磁盘提醒。"""

    def test_scan_reports_sizes_sessions_and_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            _old_session(home, "20260601", "aaaa1111-1111-4111-8111-111111111111", 4096, 100)
            _old_session(home, "20260920", "bbbb2222-2222-4222-8222-222222222222", 2048, 1)
            (home / "packages").mkdir()
            (home / "packages" / "cli.tar.gz").write_bytes(b"p" * (2 * _MIB))

            monitor = _monitor(
                root,
                home,
                thresholds=DiskThresholds.from_gb(
                    single_warn_gb=0.001,
                    total_warn_gb=0.0001,
                ),
            )
            report = monitor.scan(now=time.time())

        entry = report["directories"][0]
        self.assertEqual(entry["session_files"], 2)
        self.assertEqual(entry["session_bytes"], 4096 + 2048)
        self.assertGreaterEqual(entry["bytes"], 2 * _MIB)
        self.assertEqual(entry["top_children"][0]["name"], "packages")
        messages = [item["message"] for item in report["reminders"]]
        self.assertTrue(any("Codex (codex)" in message for message in messages))
        self.assertTrue(any("合计" in message for message in messages))
        self.assertTrue(report["preview"]["count"] >= 1)

    def test_scan_without_threshold_breach_has_no_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            _old_session(home, "20260601", "cccc3333-3333-4333-8333-333333333333", 512, 100)

            report = _monitor(root, home).scan(now=time.time())

        self.assertEqual(report["reminders"], [])

    def test_target_created_after_construction_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            monitor = HousekeepingMonitor(
                targets=(
                    AuditTarget(
                        "Codex (codex)",
                        "codex",
                        home,
                        sessions_root=home / "sessions",
                    ),
                ),
                archive_dir=root / "archives",
            )
            self.assertEqual([target.label for target in monitor.targets], [])
            home.mkdir()
            (home / "sessions").mkdir()

            report = monitor.scan(now=time.time())

        self.assertEqual(len(report["directories"]), 1)
        self.assertEqual(report["directories"][0]["label"], "Codex (codex)")

    def test_missing_target_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            monitor = HousekeepingMonitor(
                targets=(
                    AuditTarget("Codex", "codex", root / "missing"),
                ),
                archive_dir=root / "archives",
            )

            report = monitor.scan(now=time.time())

        self.assertEqual(report["directories"], [])
        self.assertEqual(report["totals"]["bytes"], 0)

    def test_empty_report_is_dashboard_safe(self) -> None:
        report = empty_housekeeping_report()

        self.assertIsNone(report["observed_at"])
        self.assertEqual(report["reminders"], [])
        self.assertEqual(report["preview"]["count"], 0)

    def test_update_targets_swaps_targets_and_invalidates_caches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home_a = root / ".codex"
            home_b = root / ".kimi"
            home_a.mkdir()
            home_b.mkdir()
            monitor = _monitor(root, home_a)
            report_a = monitor.refresh(now=1_000.0)
            monitor.sessions()

            monitor.update_targets(
                (AuditTarget("Kimi Code", "kimi", home_b),)
            )
            labels = [target.label for target in monitor.targets]
            empty_after_swap = monitor.latest()
            report_b = monitor.refresh(now=1_001.0)

        self.assertEqual(report_a["directories"][0]["label"], "Codex (codex)")
        self.assertEqual(labels, ["Kimi Code"])
        # 中文注释：换目标后旧报告和会话缓存必须失效，latest 退回空报告。
        self.assertEqual(empty_after_swap["directories"], [])
        self.assertEqual(
            [entry["label"] for entry in report_b["directories"]],
            ["Kimi Code"],
        )


class CleanupPreviewTests(unittest.TestCase):
    """验证归档/清理预览的筛选和安全保护。"""

    def test_preview_skips_active_recent_and_small_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            old = _old_session(
                home,
                "20260601",
                "dddd4444-4444-4444-8444-444444444444",
                4096,
                100,
            )
            active = _old_session(
                home,
                "20260602",
                "eeee5555-5555-4555-8555-555555555555",
                4096,
                100,
            )
            _old_session(
                home,
                "20260921",
                "ffff6666-6666-4666-8666-666666666666",
                4096,
                1,
            )
            small = _old_session(
                home,
                "20260603",
                "aaaa7777-7777-4777-8777-777777777777",
                128,
                100,
            )
            monitor = _monitor(root, home, active={str(active)})

            plan = monitor.plan(CleanupCriteria(older_than_days=30), now=time.time())
            small_plan = monitor.plan(
                CleanupCriteria(older_than_days=30, min_bytes=1024),
                now=time.time(),
            )

        self.assertEqual(
            {str(item.path) for item in plan.files},
            {str(old), str(small)},
        )
        self.assertEqual(plan.skipped_active, 1)
        self.assertEqual(plan.skipped_recent, 1)
        self.assertEqual([str(item.path) for item in small_plan.files], [str(old)])
        self.assertEqual(small_plan.skipped_small, 1)

    def test_preview_payload_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            for index in range(3):
                _old_session(
                    home,
                    f"2026060{index + 1}",
                    f"{index:08d}-1111-4111-8111-111111111111",
                    2048,
                    100,
                )
            monitor = _monitor(root, home)

            payload = monitor.preview(
                CleanupCriteria(older_than_days=30),
                now=time.time(),
            )

        self.assertEqual(payload["count"], 3)
        self.assertEqual(len(payload["files"]), 3)
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["bytes"], 3 * 2048)

    def test_invalid_criteria_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CleanupCriteria(older_than_days=0)
        with self.assertRaises(ValueError):
            CleanupCriteria(older_than_days=4000)
        with self.assertRaises(ValueError):
            CleanupCriteria(min_bytes=-1)
        with self.assertRaises(ValueError):
            DiskThresholds(single_warn_bytes=0)
        with self.assertRaises(ValueError):
            DiskThresholds(total_warn_bytes=0)


class ArchiveAndCleanTests(unittest.TestCase):
    """验证压缩归档、直接清理和恢复。"""

    def test_archive_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            _old_session(
                home,
                "20260601",
                "aaaa8888-8888-4888-8888-888888888888",
                4096,
                100,
            )
            monitor = _monitor(root, home)

            with self.assertRaises(HousekeepingError):
                monitor.archive(CleanupCriteria(older_than_days=30))

    def test_archive_then_restore_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            first = _old_session(
                home,
                "20260601",
                "aaaa9999-9999-4999-8999-999999999999",
                4096,
                100,
            )
            second = _old_session(
                home,
                "20260602",
                "bbbb9999-9999-4999-8999-999999999999",
                2048,
                100,
            )
            monitor = _monitor(root, home)

            result = monitor.archive(
                CleanupCriteria(older_than_days=30),
                confirm=True,
            )
            archives = monitor.restores()
            remaining = monitor.sessions()
            manifest = json.loads(
                Path(result["manifest"]).read_text(encoding="utf-8")
            )
            restored = monitor.restore(
                Path(result["archive"]),
                destination=root / "restore",
            )
            restored_files = sorted(
                (root / "restore").glob("**/rollout-*.jsonl")
            )

        self.assertEqual(result["count"], 2)
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["failed"], [])
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(remaining, ())
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0]["count"], 2)
        self.assertEqual(manifest["count"], 2)
        self.assertEqual(len(manifest["archive_sha256"]), 64)
        self.assertEqual(restored["restored"], 2)
        self.assertEqual(len(restored_files), 2)
        self.assertEqual({item.name for item in restored_files}, {first.name, second.name})

    def test_archive_skips_active_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            active = _old_session(
                home,
                "20260601",
                "cccc9999-9999-4999-8999-999999999999",
                4096,
                100,
            )
            monitor = _monitor(root, home, active={str(active)})

            result = monitor.archive(
                CleanupCriteria(older_than_days=30),
                confirm=True,
            )
            still_there = active.exists()

        self.assertEqual(result["count"], 0)
        self.assertTrue(still_there)

    def test_clean_requires_confirmation_and_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            old = _old_session(
                home,
                "20260601",
                "dddd9999-9999-4999-8999-999999999999",
                4096,
                100,
            )
            monitor = _monitor(root, home)

            with self.assertRaises(HousekeepingError):
                monitor.clean(CleanupCriteria(older_than_days=30))
            result = monitor.clean(
                CleanupCriteria(older_than_days=30),
                confirm=True,
            )

        self.assertEqual(result["action"], "clean")
        self.assertEqual(result["deleted"], 1)
        self.assertFalse(old.exists())

    def test_restore_rejects_unsafe_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            _old_session(
                home,
                "20260601",
                "eeee9999-9999-4999-8999-999999999999",
                1024,
                100,
            )
            monitor = _monitor(root, home)
            malicious = root / "evil.tar.gz"
            payload = b"owned"
            with tarfile.open(malicious, "w:gz") as handle:
                info = tarfile.TarInfo("../escaped.jsonl")
                info.size = len(payload)
                handle.addfile(info, io.BytesIO(payload))

            with self.assertRaises(HousekeepingError):
                monitor.restore(malicious, destination=root / "restore")

        self.assertFalse((root / "escaped.jsonl").exists())

    def test_restore_missing_archive_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            monitor = _monitor(root, root / ".codex")

            with self.assertRaises(HousekeepingError):
                monitor.restore(root / "archives" / "missing.tar.gz")

    def test_refresh_is_cached_until_forced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            _old_session(
                home,
                "20260601",
                "ffff9999-9999-4999-8999-999999999999",
                1024,
                100,
            )
            monitor = _monitor(root, home)

            first = monitor.refresh(now=1_000.0)
            cached = monitor.refresh(now=1_010.0)
            forced = monitor.refresh(now=1_010.0, force=True)

        self.assertIs(first, cached)
        self.assertEqual(forced["observed_at"], 1_010.0)
        self.assertEqual(monitor.latest()["observed_at"], 1_010.0)


class SingleSessionArchiveTests(unittest.TestCase):
    """验证按会话路径单独归档。"""

    def test_criteria_paths_ignore_age_but_keep_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            just_finished = _old_session(
                home,
                "20260921",
                "aaaa1111-2222-4333-8444-555566667777",
                2048,
                days_old=1,
            )
            writing = _old_session(
                home,
                "20260601",
                "bbbb1111-2222-4333-8444-555566667777",
                2048,
                days_old=100,
            )
            os.utime(writing, None)  # 刚刚还在写
            active = _old_session(
                home,
                "20260602",
                "bbbb2222-2222-4333-8444-555566667777",
                2048,
                days_old=100,
            )
            monitor = _monitor(root, home, active={str(active)})

            # 指定路径时忽略保留天数：一天前的会话可以直接归档
            plan = monitor.plan(
                CleanupCriteria(paths=(str(just_finished),)),
                now=time.time(),
            )
            # 仍在写入和仍在运行的会话即使被指定也要跳过
            writing_plan = monitor.plan(
                CleanupCriteria(paths=(str(writing),)),
                now=time.time(),
            )
            active_plan = monitor.plan(
                CleanupCriteria(paths=(str(active),)),
                now=time.time(),
            )

        self.assertEqual([str(item.path) for item in plan.files], [str(just_finished)])
        self.assertEqual(writing_plan.count, 0)
        self.assertEqual(writing_plan.skipped_recent, 1)
        self.assertEqual(active_plan.count, 0)
        self.assertEqual(active_plan.skipped_active, 1)

    def test_criteria_paths_select_only_requested_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            wanted = _old_session(
                home,
                "20260601",
                "cccc1111-2222-4333-8444-555566667777",
                2048,
                days_old=100,
            )
            other = _old_session(
                home,
                "20260602",
                "dddd1111-2222-4333-8444-555566667777",
                2048,
                days_old=100,
            )
            monitor = _monitor(root, home)

            plan = monitor.plan(
                CleanupCriteria(paths=(str(wanted),)),
                now=time.time(),
            )
            result = monitor.archive(
                CleanupCriteria(paths=(str(wanted),)),
                confirm=True,
            )
            wanted_exists = wanted.exists()
            other_exists = other.exists()

        self.assertEqual([str(item.path) for item in plan.files], [str(wanted)])
        self.assertEqual(result["count"], 1)
        self.assertFalse(wanted_exists)
        self.assertTrue(other_exists)

    def test_unmatched_paths_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            _old_session(
                home,
                "20260601",
                "eeee1111-2222-4333-8444-555566667777",
                2048,
                days_old=100,
            )
            monitor = _monitor(root, home)
            criteria = CleanupCriteria(paths=(str(root / "missing.jsonl"),))

            preview = monitor.preview(criteria, now=time.time())

        self.assertEqual(preview["count"], 0)
        self.assertEqual(len(preview["unmatched"]), 1)

    def test_session_archive_state_explains_why_not_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            ready = _old_session(
                home,
                "20260601",
                "ffff1111-2222-4333-8444-555566667777",
                2048,
                days_old=100,
            )
            active = _old_session(
                home,
                "20260602",
                "abcd1111-2222-4333-8444-555566667777",
                2048,
                days_old=100,
            )
            fresh = _old_session(
                home,
                "20260921",
                "dcba1111-2222-4333-8444-555566667777",
                2048,
                days_old=0,
            )
            outside = root / "elsewhere.jsonl"
            outside.write_bytes(b"x" * 128)
            monitor = _monitor(root, home, active={str(active)})

            states = monitor.session_archive_state(
                [str(ready), str(active), str(fresh), str(outside)],
                now=time.time(),
            )

        self.assertTrue(states[str(ready)]["eligible"])
        self.assertEqual(states[str(active)]["reason"], "会话仍在运行")
        self.assertIn("仍在写入", states[str(fresh)]["reason"])
        self.assertIn("可归档", states[str(outside)]["reason"])


class HousekeepingTaskTests(unittest.TestCase):
    """验证后台归档/清理任务和进度上报。"""

    def test_background_archive_reports_progress_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            for index in range(4):
                _old_session(
                    home,
                    f"2026060{index + 1}",
                    f"{index:08d}-1111-4111-8111-111111111111",
                    4096,
                    days_old=100,
                )
            monitor = _monitor(root, home)

            task = monitor.start_task(
                "archive",
                CleanupCriteria(older_than_days=30),
            )
            self.assertEqual(task["state"], "running")
            self.assertEqual(task["action"], "archive")

            deadline = time.time() + 30
            latest = task
            while time.time() < deadline:
                current = monitor.task(task["id"])
                if current is None or current["state"] != "running":
                    latest = current
                    break
                latest = current
                time.sleep(0.05)

            remaining = monitor.sessions()
            archives = monitor.restores()
            tasks = monitor.tasks()

        self.assertIsNotNone(latest)
        self.assertEqual(latest["state"], "done")
        self.assertEqual(latest["result"]["count"], 4)
        self.assertEqual(latest["result"]["deleted"], 4)
        self.assertEqual(latest["progress"]["phase"], "done")
        self.assertEqual(remaining, ())
        self.assertEqual(len(archives), 1)
        self.assertEqual(len(tasks), 1)

    def test_background_clean_finishes_and_unknown_action_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            _old_session(
                home,
                "20260601",
                "bbbb0000-0000-4000-8000-000000000000",
                1024,
                days_old=100,
            )
            monitor = _monitor(root, home)

            with self.assertRaises(HousekeepingError):
                monitor.start_task("restore", CleanupCriteria())
            task = monitor.start_task("clean", CleanupCriteria(older_than_days=30))
            deadline = time.time() + 30
            current = monitor.task(task["id"])
            while (
                time.time() < deadline
                and current is not None
                and current["state"] == "running"
            ):
                time.sleep(0.05)
                current = monitor.task(task["id"])
            missing = monitor.task("missing")

        self.assertIsNotNone(current)
        self.assertEqual(current["state"], "done")
        self.assertEqual(current["result"]["deleted"], 1)
        self.assertIsNone(missing)

    def test_progress_callback_is_optional_and_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            _old_session(
                home,
                "20260601",
                "aaaa0000-0000-4000-8000-000000000000",
                2048,
                days_old=100,
            )
            monitor = _monitor(root, home)
            events: list[dict[str, object]] = []

            def broken(progress: dict[str, object]) -> None:
                events.append(dict(progress))
                raise RuntimeError("进度回调坏了")

            result = monitor.archive(
                CleanupCriteria(older_than_days=30),
                confirm=True,
                progress=broken,
            )

        self.assertEqual(result["deleted"], 1)
        self.assertTrue(events)
        self.assertEqual({event["phase"] for event in events}, {"compress", "verify", "delete"})


if __name__ == "__main__":
    unittest.main()
