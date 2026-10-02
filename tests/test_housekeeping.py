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

from a_token_monitor.housekeeping import (
    AuditTarget,
    CleanupCriteria,
    DiskThresholds,
    HousekeepingError,
    HousekeepingMonitor,
    default_sessions_root,
    empty_housekeeping_report,
    scan_session_files,
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


def _age_file(path: Path, days_old: float) -> None:
    """把文件修改时间调到指定天数之前。"""

    stamp = time.time() - days_old * 86_400
    os.utime(path, (stamp, stamp))


def _target(product: str, home: Path) -> AuditTarget:
    """按产品默认会话布局构造一个审计目标。"""

    return AuditTarget(
        label=f"{product} (测试)",
        product=product,
        path=home,
        sessions_root=default_sessions_root(product, home),
    )


def _codex_session(
    home: Path,
    session_id: str,
    cwd: str | None,
    days_old: float,
) -> Path:
    """写入一个旧 Codex rollout 文件；cwd 为 None 时头部不含项目信息。"""

    path = (
        home
        / "sessions"
        / "2026"
        / "06"
        / "01"
        / f"rollout-2026-06-01T01-00-00-{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if cwd is None:
        path.write_bytes(b"x" * 1024)
    else:
        header = json.dumps(
            {"type": "session_meta", "payload": {"id": session_id, "cwd": cwd}}
        )
        path.write_text(header + "\n" + "x" * 1024, encoding="utf-8")
    _age_file(path, days_old)
    return path


def _rewrite_codex_head(
    path: Path,
    cwd: str,
    days_old: float,
    padding: int,
) -> None:
    """重写 rollout 文件的 cwd 头部并重置修改时间。"""

    header = json.dumps({"type": "session_meta", "payload": {"cwd": cwd}})
    path.write_text(header + "\n" + "x" * padding, encoding="utf-8")
    _age_file(path, days_old)


def _claude_session(
    home: Path,
    slug: str,
    session_id: str,
    cwd: str | None,
    days_old: float,
) -> Path:
    """写入一个 Claude 主会话文件；cwd 为 None 时头部不含项目信息。"""

    path = home / "projects" / slug / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if cwd is None:
        head = json.dumps({"type": "user", "message": "hi"})
    else:
        head = json.dumps({"type": "user", "cwd": cwd})
    path.write_text(head + "\n", encoding="utf-8")
    _age_file(path, days_old)
    return path


def _claude_subagent(main: Path, days_old: float) -> Path:
    """在主会话同名目录的 subagents 下写入一个不含 cwd 的子代理文件。"""

    path = main.with_suffix("") / "subagents" / "agent-w0.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "summary"}) + "\n", encoding="utf-8")
    _age_file(path, days_old)
    return path


def _kimi_session(
    home: Path,
    workdir: str,
    session: str,
    cwd: str | None,
    days_old: float,
) -> Path:
    """写入 Kimi 会话的 wire.jsonl；cwd 非 None 时补一份 state.json。"""

    session_dir = home / "sessions" / workdir / session
    wire = session_dir / "agents" / "agent-0" / "wire.jsonl"
    wire.parent.mkdir(parents=True, exist_ok=True)
    wire.write_bytes(b"x" * 1024)
    if cwd is not None:
        (session_dir / "state.json").write_text(
            json.dumps({"cwd": cwd}),
            encoding="utf-8",
        )
    _age_file(wire, days_old)
    return wire


def _grok_session(
    home: Path,
    encoded_project: str,
    session: str,
    days_old: float,
) -> Path:
    """写入 Grok 会话文件；项目路径编码在一级目录名里。"""

    path = home / "sessions" / encoded_project / session / "session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 1024)
    _age_file(path, days_old)
    return path


def _dsh_session(
    home: Path,
    session: str,
    cwd: str | None,
    days_old: float,
) -> Path:
    """写入 DSH 会话文件；cwd 非 None 时补一份 projcache 快照。"""

    path = home / "sessions" / "--aHR0cHM--" / session / "messages.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 1024)
    if cwd is not None:
        cache = (
            home
            / "storages"
            / "session_projcache"
            / "sessions"
            / f"{session}.json"
        )
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "record": {
                        "identity": {"cwd": cwd},
                        "rows": {
                            "tokenUsage": {
                                "val": {
                                    "totals": {
                                        "uncachedInputTokens": 12,
                                        "outputTokens": 3,
                                    }
                                }
                            }
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
    _age_file(path, days_old)
    return path


def _commandcode_session(
    home: Path,
    slug: str,
    session: str,
    cwd: str | None,
    days_old: float,
) -> tuple[Path, Path, Path]:
    """写入 Command Code 主会话及 meta/checkpoints 侧车文件。"""

    root = home / "projects" / slug
    root.mkdir(parents=True, exist_ok=True)
    main = root / f"{session}.jsonl"
    if cwd is None:
        header = json.dumps({"type": "message", "text": "hi"})
    else:
        header = json.dumps(
            {
                "type": "session",
                "id": session,
                "cwd": cwd,
                "timestamp": 1_750_000_000,
            }
        )
    main.write_text(header + "\n", encoding="utf-8")
    meta = root / f"{session}.meta.json"
    meta.write_text(json.dumps({"model": "test-model"}), encoding="utf-8")
    checkpoints = root / f"{session}.checkpoints.jsonl"
    checkpoints.write_text(json.dumps({"seq": 1}) + "\n", encoding="utf-8")
    for path in (main, meta, checkpoints):
        _age_file(path, days_old)
    return main, meta, checkpoints


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

    def test_preview_files_sorted_by_size_and_idleness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            _old_session(
                home,
                "20260601",
                "aaaa8888-1111-4111-8111-111111111111",
                128,
                100,
            )
            biggest = _old_session(
                home,
                "20260602",
                "bbbb8888-2222-4222-8222-222222222222",
                8 * 1024 * 1024,
                100,
            )
            middle = _old_session(
                home,
                "20260603",
                "cccc8888-3333-4333-8333-333333333333",
                2 * 1024 * 1024,
                100,
            )
            # 中文注释：又大又新的 8MiB/2 天，得分低于又小又久的 1MiB/100 天，
            # 证明排序看的是「体积 × 闲置时长」而不是纯体积。
            large_but_recent = _old_session(
                home,
                "20260928",
                "dddd8888-4444-4444-8444-444444444444",
                8 * 1024 * 1024,
                2,
            )
            small_but_idle = _old_session(
                home,
                "20260604",
                "eeee8888-5555-4555-8555-555555555555",
                1024 * 1024,
                100,
            )
            monitor = _monitor(root, home)

            preview = monitor.preview(CleanupCriteria(older_than_days=1), now=time.time())

        paths = [item["path"] for item in preview["files"]]
        self.assertEqual(paths[0], str(biggest))
        self.assertEqual(paths[1], str(middle))
        self.assertLess(paths.index(str(small_but_idle)), paths.index(str(large_but_recent)))

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


class AnyAgeCriteriaTests(unittest.TestCase):
    """验证整项目压缩的 any_age 模式：不看保留天数，但护栏不变。"""

    def test_any_age_ignores_retention_days_but_keeps_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            recent_finished = _old_session(
                home,
                "20260921",
                "aaaa2222-2222-4333-8444-555566667777",
                2048,
                days_old=1,
            )
            writing = _old_session(
                home,
                "20260601",
                "bbbb3333-2222-4333-8444-555566667777",
                2048,
                days_old=100,
            )
            os.utime(writing, None)  # 刚刚还在写
            active = _old_session(
                home,
                "20260602",
                "cccc3333-2222-4333-8444-555566667777",
                2048,
                days_old=100,
            )
            monitor = _monitor(root, home, active={str(active)})
            now = time.time()

            # 对照：默认 criteria 下一天前的会话不够老，会被跳过
            default_plan = monitor.plan(CleanupCriteria(), now=now)
            # any_age：不看保留天数，一天前的会话也可以处理
            any_age_plan = monitor.plan(CleanupCriteria(any_age=True), now=now)

        self.assertEqual(default_plan.count, 0)
        self.assertIn(str(recent_finished), [str(item.path) for item in any_age_plan.files])
        self.assertEqual(any_age_plan.skipped_recent, 1)  # 仍在写入的仍跳过
        self.assertEqual(any_age_plan.skipped_active, 1)  # 活动会话仍跳过

    def test_criteria_to_dict_includes_any_age(self) -> None:
        self.assertFalse(CleanupCriteria().to_dict()["any_age"])
        self.assertTrue(CleanupCriteria(any_age=True).to_dict()["any_age"])

    def test_restore_invalidates_sessions_cache(self) -> None:
        """恢复归档后，会话清单缓存必须作废，单会话操作立即可用。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            session = _old_session(
                home,
                "20260601",
                "eeee2222-2222-4333-8444-555566667777",
                2048,
                days_old=100,
            )
            monitor = _monitor(root, home)
            result = monitor.archive(
                CleanupCriteria(paths=(str(session),)),
                confirm=True,
            )
            monitor.restore(Path(result["archive"]))
            states = monitor.session_archive_state([str(session)], now=time.time())

        self.assertTrue(states[str(session)]["eligible"])

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


class ProviderProjectScanTests(unittest.TestCase):
    """验证各产品会话文件的项目归属与会话 ID 解析。"""

    def test_default_sessions_root_covers_all_products(self) -> None:
        home = Path("/nonexistent-home")

        self.assertEqual(default_sessions_root("codex", home), home / "sessions")
        self.assertEqual(default_sessions_root("claude", home), home / "projects")
        self.assertEqual(default_sessions_root("kimi", home), home / "sessions")
        self.assertEqual(default_sessions_root("grok", home), home / "sessions")
        self.assertEqual(default_sessions_root("dsh", home), home / "sessions")
        self.assertEqual(
            default_sessions_root("command-code", home),
            home / "projects",
        )
        self.assertIsNone(default_sessions_root("other-agent", home))

    def test_codex_scan_reads_project_from_rollout_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            meta = _codex_session(
                home,
                "aaaa1111-1111-4111-8111-111111111111",
                "/home/user/proj-a",
                100,
            )
            # 事件顶层直接带 cwd 的 rollout 头也能识别
            top = (
                home
                / "sessions"
                / "2026"
                / "06"
                / "02"
                / "rollout-2026-06-02T01-00-00-bbbb2222-2222-4222-8222-222222222222.jsonl"
            )
            top.parent.mkdir(parents=True, exist_ok=True)
            top.write_text(
                json.dumps({"cwd": "/home/user/proj-top"}) + "\n",
                encoding="utf-8",
            )
            _age_file(top, 100)
            headless = _codex_session(
                home,
                "cccc3333-3333-4333-8333-333333333333",
                None,
                100,
            )

            files = scan_session_files(_target("codex", home))

        by_path = {item.path: item for item in files}
        self.assertEqual(by_path[meta].project, "/home/user/proj-a")
        self.assertEqual(
            by_path[meta].session_id,
            "aaaa1111-1111-4111-8111-111111111111",
        )
        self.assertEqual(by_path[meta].to_dict()["project"], "/home/user/proj-a")
        self.assertEqual(by_path[top].project, "/home/user/proj-top")
        self.assertEqual(by_path[headless].project, "未知项目")

    def test_claude_scan_reads_cwd_and_subagent_falls_back_to_main(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".claude"
            session_id = "dddd4444-4444-4444-8444-444444444444"
            main = _claude_session(
                home,
                "-home-user-proj-b",
                session_id,
                "/home/user/proj-b",
                100,
            )
            subagent = _claude_subagent(main, 100)
            orphan = _claude_session(
                home,
                "-home-user-orphan",
                "eeee5555-5555-4555-8555-555555555555",
                None,
                100,
            )
            orphan_subagent = _claude_subagent(orphan, 100)

            files = scan_session_files(_target("claude", home))

        by_path = {item.path: item for item in files}
        self.assertEqual(by_path[main].project, "/home/user/proj-b")
        self.assertEqual(by_path[main].session_id, session_id)
        self.assertEqual(by_path[subagent].project, "/home/user/proj-b")
        self.assertEqual(by_path[subagent].session_id, session_id)
        self.assertEqual(by_path[orphan].project, "未知项目")
        self.assertEqual(by_path[orphan_subagent].project, "未知项目")

    def test_kimi_scan_reads_project_from_state_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".kimi"
            wire = _kimi_session(
                home,
                "wd-aaa",
                "session-aaa",
                "/home/user/proj-c",
                100,
            )
            orphan = _kimi_session(home, "wd-bbb", "session-bbb", None, 100)

            files = scan_session_files(_target("kimi", home))

        by_path = {item.path: item for item in files}
        self.assertEqual(by_path[wire].project, "/home/user/proj-c")
        self.assertEqual(by_path[wire].session_id, "session-aaa")
        self.assertEqual(by_path[orphan].project, "未知项目")

    def test_grok_scan_decodes_project_from_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".grok"
            path = _grok_session(
                home,
                "%2Fhome%2Fuser%2Fproj-d",
                "session-ccc",
                100,
            )
            invalid = _grok_session(home, "relative-name", "session-ddd", 100)

            files = scan_session_files(_target("grok", home))

        by_path = {item.path: item for item in files}
        self.assertEqual(by_path[path].project, "/home/user/proj-d")
        self.assertEqual(by_path[path].session_id, "session-ccc")
        self.assertEqual(by_path[invalid].project, "未知项目")

    def test_dsh_scan_reads_project_from_projcache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".dsh"
            path = _dsh_session(home, "session-eee", "/home/user/proj-e", 100)
            orphan = _dsh_session(home, "session-fff", None, 100)

            files = scan_session_files(_target("dsh", home))

        by_path = {item.path: item for item in files}
        self.assertEqual(by_path[path].project, "/home/user/proj-e")
        self.assertEqual(by_path[path].session_id, "session-eee")
        self.assertEqual(by_path[orphan].project, "未知项目")

    def test_commandcode_scan_reads_header_and_sidecar_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".commandcode"
            main, meta, checkpoints = _commandcode_session(
                home,
                "home-user-proj-f",
                "session-ggg",
                "/home/user/proj-f",
                100,
            )
            orphan_files = _commandcode_session(
                home,
                "home-user-orphan",
                "session-hhh",
                None,
                100,
            )

            files = scan_session_files(_target("command-code", home))

        by_path = {item.path: item for item in files}
        for path in (main, meta, checkpoints):
            self.assertEqual(by_path[path].project, "/home/user/proj-f")
            self.assertEqual(by_path[path].session_id, "session-ggg")
        for path in orphan_files:
            self.assertEqual(by_path[path].project, "未知项目")


class ProjectCriteriaFilterTests(unittest.TestCase):
    """验证 CleanupCriteria.projects 的项目筛选与校验。"""

    def test_projects_filter_selects_only_matching_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            wanted = _codex_session(
                home,
                "1111aaaa-1111-4111-8111-111111111111",
                "/home/user/proj-a",
                100,
            )
            _codex_session(
                home,
                "2222bbbb-2222-4222-8222-222222222222",
                "/home/user/proj-b",
                100,
            )
            monitor = _monitor(root, home)
            criteria = CleanupCriteria(
                older_than_days=30,
                projects=("/home/user/proj-a",),
            )

            plan = monitor.plan(criteria, now=time.time())
            preview = monitor.preview(criteria, now=time.time())

        self.assertEqual([str(item.path) for item in plan.files], [str(wanted)])
        self.assertEqual(plan.skipped_project, 1)
        self.assertEqual(plan.to_dict()["skipped_project"], 1)
        self.assertEqual(preview["skipped_project"], 1)
        self.assertEqual(preview["files"][0]["project"], "/home/user/proj-a")

    def test_empty_projects_selects_all_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            first = _codex_session(
                home,
                "3333cccc-3333-4333-8333-333333333333",
                "/home/user/proj-a",
                100,
            )
            second = _codex_session(
                home,
                "4444dddd-4444-4444-8444-444444444444",
                "/home/user/proj-b",
                100,
            )
            monitor = _monitor(root, home)

            plan = monitor.plan(
                CleanupCriteria(older_than_days=30, projects=()),
                now=time.time(),
            )

        self.assertEqual(
            {str(item.path) for item in plan.files},
            {str(first), str(second)},
        )
        self.assertEqual(plan.skipped_project, 0)

    def test_paths_take_precedence_over_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            _codex_session(
                home,
                "5555eeee-5555-4555-8555-555555555555",
                "/home/user/proj-a",
                100,
            )
            targeted = _codex_session(
                home,
                "6666ffff-6666-4666-8666-666666666666",
                "/home/user/proj-b",
                100,
            )
            monitor = _monitor(root, home)

            plan = monitor.plan(
                CleanupCriteria(
                    paths=(str(targeted),),
                    projects=("/home/user/proj-a",),
                ),
                now=time.time(),
            )

        self.assertEqual([str(item.path) for item in plan.files], [str(targeted)])
        self.assertEqual(plan.skipped_project, 0)

    def test_invalid_projects_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CleanupCriteria(projects=("",))
        with self.assertRaises(ValueError):
            CleanupCriteria(projects=("   ",))
        with self.assertRaises(ValueError):
            CleanupCriteria(projects=("x" * 1025,))
        criteria = CleanupCriteria(projects=("/" + "x" * 1023,))
        self.assertEqual(len(criteria.projects[0]), 1024)

    def test_projects_report_selected_counts_under_criteria(self) -> None:
        """项目聚合同时给出总数与当前条件下实际会处理的数量。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            old = _codex_session(
                home,
                "9999aaaa-9999-4999-8999-999999999999",
                "/home/user/proj-a",
                100,
            )
            # 同项目里一个过新（不满足保留天数）的文件
            _codex_session(
                home,
                "9999bbbb-9999-4999-8999-999999999998",
                "/home/user/proj-a",
                5,
            )
            _codex_session(
                home,
                "9999cccc-9999-4999-8999-999999999997",
                "/home/user/proj-b",
                100,
            )
            monitor = _monitor(root, home)
            expected_size = old.stat().st_size

            without_criteria = {
                entry["project"]: entry for entry in monitor.projects()
            }
            with_criteria = {
                entry["project"]: entry
                for entry in monitor.projects(CleanupCriteria(older_than_days=30))
            }

        self.assertNotIn("selected_files", without_criteria["/home/user/proj-a"])
        alpha = with_criteria["/home/user/proj-a"]
        self.assertEqual(alpha["files"], 2)
        self.assertEqual(alpha["selected_files"], 1)
        self.assertEqual(alpha["selected_bytes"], expected_size)
        beta = with_criteria["/home/user/proj-b"]
        self.assertEqual(beta["files"], 1)
        self.assertEqual(beta["selected_files"], 1)


class ActiveDirectoryProtectionTests(unittest.TestCase):
    """验证活动集合里的目录路径会保护其下的全部文件。"""

    def test_active_claude_session_directory_protects_subagents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".claude"
            main = _claude_session(
                home,
                "-home-user-proj-b",
                "7777aaaa-7777-4777-8777-777777777777",
                "/home/user/proj-b",
                100,
            )
            _claude_subagent(main, 100)
            # 活动集合里是 <uuid>.jsonl 去掉后缀后的会话目录
            monitor = HousekeepingMonitor(
                targets=(_target("claude", home),),
                archive_dir=root / "archives",
                active_paths=lambda: {str(main.with_suffix(""))},
            )

            plan = monitor.plan(CleanupCriteria(older_than_days=30), now=time.time())

        # 主会话不在目录之下仍可归档；subagents 下的文件被目录条目保护
        self.assertEqual([str(item.path) for item in plan.files], [str(main)])
        self.assertEqual(plan.skipped_active, 1)


class MultiProviderArchiveTests(unittest.TestCase):
    """验证跨产品归档/恢复往返与归档命名。"""

    def test_mixed_providers_archive_restore_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            codex_home = root / ".codex"
            claude_home = root / ".claude"
            codex = _codex_session(
                codex_home,
                "8888bbbb-8888-4888-8888-888888888888",
                "/home/user/proj-a",
                100,
            )
            claude = _claude_session(
                claude_home,
                "-home-user-proj-b",
                "9999cccc-9999-4999-8999-999999999999",
                "/home/user/proj-b",
                100,
            )
            monitor = HousekeepingMonitor(
                targets=(
                    _target("codex", codex_home),
                    _target("claude", claude_home),
                ),
                archive_dir=root / "archives",
            )

            result = monitor.archive(
                CleanupCriteria(older_than_days=30),
                confirm=True,
            )
            manifest = json.loads(
                Path(result["manifest"]).read_text(encoding="utf-8")
            )
            restored = monitor.restore(
                Path(result["archive"]),
                destination=root / "restore",
            )
            restored_files = sorted((root / "restore").glob("**/*.jsonl"))

        self.assertEqual(result["count"], 2)
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["failed"], [])
        self.assertFalse(codex.exists())
        self.assertFalse(claude.exists())
        projects = {entry["path"]: entry["project"] for entry in manifest["files"]}
        self.assertEqual(projects[str(codex)], "/home/user/proj-a")
        self.assertEqual(projects[str(claude)], "/home/user/proj-b")
        self.assertEqual(restored["restored"], 2)
        self.assertEqual(
            {item.name for item in restored_files},
            {codex.name, claude.name},
        )

    def test_single_project_archive_name_contains_project_slug(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            _codex_session(
                home,
                "aaaadddd-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "/home/user/My Proj",
                100,
            )
            _codex_session(
                home,
                "bbbbeeee-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "/home/user/other",
                100,
            )
            monitor = _monitor(root, home)

            named = monitor.archive(
                CleanupCriteria(
                    older_than_days=30,
                    projects=("/home/user/My Proj",),
                ),
                confirm=True,
                now=1_800_000_000.0,
            )
            plain = monitor.archive(
                CleanupCriteria(
                    older_than_days=30,
                    projects=("/home/user/My Proj", "/home/user/other"),
                ),
                confirm=True,
                now=1_800_000_100.0,
            )

        self.assertEqual(named["count"], 1)
        self.assertRegex(
            Path(named["archive"]).name,
            r"^sessions-My-Proj-\d{8}-\d{6}\.tar\.gz$",
        )
        self.assertEqual(plain["count"], 1)
        self.assertRegex(
            Path(plain["archive"]).name,
            r"^sessions-\d{8}-\d{6}\.tar\.gz$",
        )

    def test_restores_lists_legacy_codex_prefixed_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            monitor = _monitor(root, home)
            archive_dir = root / "archives"
            archive_dir.mkdir(parents=True)
            legacy = archive_dir / "codex-sessions-20990101-000000.tar.gz"
            payload = b"legacy"
            with tarfile.open(legacy, "w:gz") as handle:
                info = tarfile.TarInfo("legacy.jsonl")
                info.size = len(payload)
                handle.addfile(info, io.BytesIO(payload))
            legacy_size = legacy.stat().st_size

            items = monitor.restores()

        self.assertEqual(
            [Path(item["archive"]).name for item in items],
            ["codex-sessions-20990101-000000.tar.gz"],
        )
        self.assertIsNone(items[0]["manifest"])
        self.assertEqual(items[0]["bytes"], legacy_size)


class ProjectCacheTests(unittest.TestCase):
    """验证 monitor 内部的项目缓存按体积和修改时间失效。"""

    def test_project_cache_invalidates_on_size_and_mtime_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            path = _codex_session(
                home,
                "ccccffff-cccc-4ccc-8ccc-cccccccccccc",
                "/home/user/proj-a",
                100,
            )
            monitor = _monitor(root, home)

            first = {item.path: item for item in monitor.sessions()}[path].project

            # 文件不可读但 size/mtime 未变时仍由缓存给出项目（证明缓存生效）
            path.chmod(0o000)
            cached = {item.path: item for item in monitor.sessions(refresh=True)}[
                path
            ].project
            path.chmod(0o644)

            # 体积变化直接让缓存失效，scan() 重新解析出 proj-b
            _rewrite_codex_head(path, "/home/user/proj-b", 99, padding=64)
            report = monitor.scan(now=time.time())
            second = report["preview"]["files"][0]["project"]

            # 体积不变（proj-b/proj-c 等长）、mtime 变化时缓存同样失效
            _rewrite_codex_head(path, "/home/user/proj-c", 98, padding=64)
            third = {item.path: item for item in monitor.sessions(refresh=True)}[
                path
            ].project

        self.assertEqual(first, "/home/user/proj-a")
        self.assertEqual(cached, "/home/user/proj-a")
        self.assertEqual(second, "/home/user/proj-b")
        self.assertEqual(third, "/home/user/proj-c")


if __name__ == "__main__":
    unittest.main()
