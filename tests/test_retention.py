"""历史数据保留期配置与清理管理器的测试。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from a_token_monitor.alerts import TrafficAlertStore
from a_token_monitor.multi_models import DetectionConfidence, SessionStatus, TrackedSession
from a_token_monitor.registry import MultiSessionRegistry
from a_token_monitor.retention import (
    DEFAULT_SESSION_RETENTION_DAYS,
    DEFAULT_USAGE_RETENTION_DAYS,
    HistoryDataManager,
    RetentionController,
    RetentionError,
    RetentionSettings,
    db_file_bytes,
)
from a_token_monitor.traffic import TrafficAlert
from a_token_monitor.usage import _UsageIndexStore


NOW = 1_800_000_000.0
OLD = NOW - 200 * 86400.0
RECENT = NOW - 5 * 86400.0


def _session(
    thread_id: str,
    status: SessionStatus,
    last_seen: float,
    *,
    quota_blocked_at: float | None = None,
    resume_attempts: int = 0,
) -> TrackedSession:
    return TrackedSession(
        thread_id=thread_id,
        session_id=f"session-{thread_id}",
        jsonl_path=f"/tmp/{thread_id}.jsonl",
        cwd="/tmp",
        source="cli",
        status=status,
        confidence=DetectionConfidence.PERSISTED,
        first_seen_at=last_seen - 100,
        last_seen_at=last_seen,
        quota_blocked_at=quota_blocked_at,
        resume_attempts=resume_attempts,
    )


def _alert(observed_at: float, message: str) -> TrafficAlert:
    return TrafficAlert(
        level="warn",
        product="codex",
        pid=1234,
        kind="burst",
        bytes=1024,
        window_seconds=1.0,
        message=message,
        observed_at=observed_at,
    )


def _insert_usage_rows(db_path: Path, timestamps: list[float]) -> None:
    """直接写入 usage_delta 行,并保证 usage_file_state 有一行检查点。"""

    with sqlite3.connect(db_path) as connection:
        for index, timestamp in enumerate(timestamps):
            connection.execute(
                "INSERT INTO usage_delta(path, kind, timestamp, model, usage_json,"
                " billing_usage_json, project) VALUES (?, 'total', ?, 'm', '{}', '{}', 'p')",
                (f"/tmp/file-{index}.jsonl", timestamp),
            )
        connection.execute(
            "INSERT INTO usage_file_state(path, inode, mtime_ns, file_size,"
            " next_offset, complete, state_json) VALUES ('/tmp/file-0.jsonl', 1, 1, 1, 1, 0, '{}')"
        )


class RetentionSettingsTests(unittest.TestCase):
    """保留期覆盖配置的持久化与严格校验。"""

    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "settings.json"
            settings = RetentionSettings(overrides={"usage_days": 45.0})

            settings.save(path)
            loaded = RetentionSettings.load(path)

            self.assertEqual(dict(loaded.overrides), {"usage_days": 45.0})

    def test_missing_file_means_no_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            loaded = RetentionSettings.load(Path(temporary_directory) / "x.json")
            self.assertEqual(dict(loaded.overrides), {})

    def test_bad_schema_and_values_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "settings.json"
            for payload in (
                {"schema_version": 99, "overrides": {}},
                {"schema_version": 1, "overrides": {"unknown": 1}},
                {"schema_version": 1, "overrides": {"usage_days": "x"}},
                {"schema_version": 1, "overrides": {"usage_days": 0}},
                {"schema_version": 1, "overrides": {"usage_days": 99999}},
            ):
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(RetentionError):
                    RetentionSettings.load(path)

    def test_override_helpers(self) -> None:
        settings = RetentionSettings()
        settings = settings.with_override("usage_days", 10.0)
        settings = settings.with_override("session_days", 20.0)
        self.assertEqual(settings.overrides["usage_days"], 10.0)
        settings = settings.without_override("usage_days")
        self.assertNotIn("usage_days", settings.overrides)
        with self.assertRaises(RetentionError):
            settings.with_override("unknown", 1.0)


class RetentionControllerTests(unittest.TestCase):
    """Web 覆盖优先于命令行取值,修改热生效。"""

    def test_web_override_beats_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            applied: list[dict[str, float]] = []
            controller = RetentionController(
                Path(temporary_directory),
                {"usage_days": 90.0, "session_days": 30.0},
                reload_callback=applied.append,
            )

            snapshot = controller.apply("set", usage_days=45.0)

            retention = snapshot["retention"]
            self.assertEqual(retention["usage_days"]["value"], 45.0)
            self.assertEqual(retention["usage_days"]["source"], "web")
            self.assertEqual(retention["session_days"]["source"], "cli")
            self.assertEqual(
                applied, [{"usage_days": 45.0, "session_days": 30.0}]
            )
            self.assertEqual(
                controller.effective(), {"usage_days": 45.0, "session_days": 30.0}
            )

    def test_reset_restores_cli_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            controller = RetentionController(
                Path(temporary_directory),
                {"usage_days": 90.0, "session_days": 30.0},
            )
            controller.apply("set", usage_days=45.0, session_days=10.0)

            snapshot = controller.apply("reset")

            retention = snapshot["retention"]
            self.assertEqual(retention["usage_days"]["value"], 90.0)
            self.assertEqual(retention["usage_days"]["source"], "cli")
            self.assertEqual(retention["session_days"]["value"], 30.0)
            loaded = RetentionSettings.load(controller.config_path)
            self.assertEqual(dict(loaded.overrides), {})

    def test_invalid_values_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            controller = RetentionController(
                Path(temporary_directory),
                {"usage_days": 90.0, "session_days": 30.0},
            )
            with self.assertRaises(RetentionError):
                controller.apply("set", usage_days=0)
            with self.assertRaises(RetentionError):
                controller.apply("rename")
            with self.assertRaises(RetentionError):
                RetentionController(
                    Path(temporary_directory), {"usage_days": -1.0, "session_days": 30.0}
                )


class DbFileBytesTests(unittest.TestCase):
    """sqlite 文件大小统计含 wal 兄弟文件。"""

    def test_includes_wal_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "x.sqlite3"
            path.write_bytes(b"0" * 100)
            Path(f"{path}-wal").write_bytes(b"0" * 50)
            self.assertEqual(db_file_bytes(path), 150)


class HistoryDataManagerTests(unittest.TestCase):
    """预览与安全清理:不动活动会话、恢复历史和增量检查点。"""

    def _manager(
        self, root: Path
    ) -> tuple[HistoryDataManager, MultiSessionRegistry, TrafficAlertStore, Path]:
        state_dir = root / "state"
        state_dir.mkdir()
        registry = MultiSessionRegistry(state_dir / "accounts" / "codex")
        usage_path = state_dir / "usage-index.sqlite3"
        store = _UsageIndexStore(usage_path)
        store.close()
        _insert_usage_rows(usage_path, [OLD, OLD, RECENT])
        alert_store = TrafficAlertStore(state_dir, retention_days=30.0)
        manager = HistoryDataManager(
            state_dir,
            registries=lambda: {"codex": registry},
            alert_store=alert_store,
            usage_days=90.0,
            session_days=30.0,
            alert_days=30.0,
            usage_store_path=usage_path,
        )
        return manager, registry, alert_store, usage_path

    def test_preview_counts_and_estimates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager, registry, alert_store, _ = self._manager(
                Path(temporary_directory)
            )
            registry.upsert_session(_session("old-done", SessionStatus.COMPLETED, OLD))
            registry.upsert_session(_session("running", SessionStatus.RUNNING, OLD))
            alert_store.record([_alert(OLD, "旧告警")], now=OLD)
            alert_store.record([_alert(RECENT, "新告警")], now=RECENT)

            preview = manager.preview(now=NOW)

            kinds = {item["kind"]: item for item in preview["kinds"]}
            self.assertEqual(kinds["usage"]["rows_to_delete"], 2)
            self.assertEqual(kinds["usage"]["total_rows"], 3)
            self.assertEqual(kinds["sessions"]["rows_to_delete"], 1)
            self.assertEqual(kinds["sessions"]["total_rows"], 2)
            self.assertEqual(kinds["alerts"]["rows_to_delete"], 1)
            self.assertIn("estimated_free_bytes", preview)
            labels = [entry["label"] for entry in preview["dbs"]]
            self.assertIn("用量索引", labels)
            self.assertIn("会话历史(codex)", labels)
            self.assertIn("告警历史", labels)

    def test_cleanup_preserves_checkpoints_active_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager, registry, alert_store, usage_path = self._manager(
                Path(temporary_directory)
            )
            registry.upsert_session(_session("old-done", SessionStatus.COMPLETED, OLD))
            registry.upsert_session(_session("running", SessionStatus.RUNNING, OLD))
            registry.upsert_session(
                _session("recovery", SessionStatus.FAILED, OLD, quota_blocked_at=OLD)
            )
            alert_store.record([_alert(OLD, "旧告警")], now=OLD)
            before_checkpoints = self._file_state_rows(usage_path)

            result = manager.cleanup(now=NOW)

            self.assertEqual(result["deleted"]["usage"], 2)
            self.assertEqual(result["deleted"]["sessions"], 1)
            self.assertEqual(result["deleted"]["alerts"], 1)
            self.assertEqual(result["errors"], [])
            self.assertIsNotNone(manager.last_cleanup)
            # 中文注释:增量检查点一行不动。
            self.assertEqual(self._file_state_rows(usage_path), before_checkpoints)
            # 中文注释:活动会话与有恢复历史的会话保留。
            self.assertIsNotNone(registry.get_session("running"))
            self.assertIsNotNone(registry.get_session("recovery"))
            self.assertIsNone(registry.get_session("old-done"))
            self.assertEqual(alert_store.count_all(), 0)

    def test_update_retention_changes_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager, _, _, _ = self._manager(Path(temporary_directory))

            manager.update_retention(usage_days=400.0)
            preview = manager.preview(now=NOW)
            kinds = {item["kind"]: item for item in preview["kinds"]}
            self.assertEqual(kinds["usage"]["rows_to_delete"], 0)
            self.assertEqual(manager.retention_days["usage_days"], 400.0)

            with self.assertRaises(RetentionError):
                manager.update_retention(usage_days=0)

    def test_cleanup_failure_keeps_partial_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager, registry, _, _ = self._manager(root)
            registry.upsert_session(_session("old-done", SessionStatus.COMPLETED, OLD))

            def broken_delete(cutoff: float) -> int:
                raise sqlite3.OperationalError("database is locked")

            registry.delete_finished_sessions_before = broken_delete  # type: ignore[method-assign]

            with self.assertRaises(RetentionError):
                manager.cleanup(now=NOW)

            last = manager.last_cleanup
            self.assertIsNotNone(last)
            self.assertEqual(last["deleted"]["usage"], 2)
            self.assertTrue(last["errors"])
            self.assertTrue(any("会话历史" in item for item in last["errors"]))

    def test_cleanup_without_alert_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_dir = root / "state"
            state_dir.mkdir()
            usage_path = state_dir / "usage-index.sqlite3"
            store = _UsageIndexStore(usage_path)
            store.close()
            _insert_usage_rows(usage_path, [OLD])
            manager = HistoryDataManager(
                state_dir,
                registries=dict,
                alert_store=None,
                usage_days=DEFAULT_USAGE_RETENTION_DAYS,
                session_days=DEFAULT_SESSION_RETENTION_DAYS,
                alert_days=30.0,
                usage_store_path=usage_path,
            )

            result = manager.cleanup(now=NOW)

            self.assertEqual(result["deleted"]["usage"], 1)
            self.assertNotIn("alerts", result["deleted"])

    @staticmethod
    def _file_state_rows(usage_path: Path) -> int:
        with sqlite3.connect(usage_path) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM usage_file_state"
            ).fetchone()
        return int(row[0])


if __name__ == "__main__":
    unittest.main()
