"""多会话 SQLite 注册表测试。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_reset_monitor.multi_models import (
    DetectionConfidence,
    SessionStatus,
    TrackedSession,
)
from codex_reset_monitor.quota import QuotaSnapshot, QuotaWindow
from codex_reset_monitor.registry import MultiSessionRegistry


class RegistryTests(unittest.TestCase):
    """验证会话、额度和原子队列领取。"""

    def test_round_trip_and_claim_due_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry = MultiSessionRegistry(Path(temporary_directory) / "state")
            session = TrackedSession(
                thread_id="thread-1",
                session_id="session-1",
                jsonl_path="/tmp/session.jsonl",
                cwd="/tmp",
                source="cli",
                status=SessionStatus.QUEUED,
                confidence=DetectionConfidence.PERSISTED,
                first_seen_at=10,
                last_seen_at=20,
                next_attempt_at=100,
                quota_reset_at=80,
                account_id="account-1",
                quota_blocked_at=70,
                last_resume_started_at=80,
                last_resume_finished_at=90,
                last_resume_result="success",
            )
            registry.upsert_session(session)
            registry.save_quota(
                QuotaSnapshot(
                    observed_at=20,
                    plan_type="plus",
                    windows=(
                        QuotaWindow(
                            limit_id="codex",
                            name="primary",
                            used_percent=100,
                            window_minutes=300,
                            resets_at=80,
                            reached_type="primary",
                        ),
                    ),
                )
            )

            restored = registry.get_session("thread-1")
            claimed = registry.claim_due_session(100)
            quota = registry.load_quota()
            second_claim = registry.claim_due_session(100)
            recoveries = registry.list_recovery_sessions()

        self.assertEqual(restored.session_id, "session-1")
        self.assertEqual(restored.account_id, "account-1")
        self.assertEqual(restored.quota_blocked_at, 70)
        self.assertEqual(restored.last_resume_result, "success")
        self.assertEqual(claimed.status, SessionStatus.RESUMING)
        self.assertEqual(claimed.resume_attempts, 1)
        self.assertEqual([item.thread_id for item in recoveries], ["thread-1"])
        self.assertEqual(quota.latest_exhausted_reset_at, 80)
        self.assertIsNone(second_claim)

    def test_cancels_waiting_resume_without_removing_history_or_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry = MultiSessionRegistry(Path(temporary_directory) / "state")
            registry.upsert_session(
                TrackedSession(
                    thread_id="thread-cancel",
                    session_id="session-cancel",
                    jsonl_path="/tmp/session.jsonl",
                    cwd="/tmp",
                    source="cli",
                    status=SessionStatus.QUEUED,
                    confidence=DetectionConfidence.OPEN_FILE,
                    first_seen_at=10,
                    last_seen_at=20,
                    pids=(123,),
                    next_attempt_at=200,
                    auto_resume=True,
                    quota_blocked_at=100,
                    last_resume_result="waiting_for_reset",
                    metadata={"quota_blocked": "1"},
                )
            )

            result = registry.cancel_queued_resume("thread-cancel", now=110)
            cancelled = registry.get_session("thread-cancel")
            claim = registry.claim_due_session(300)
            second_result = registry.cancel_queued_resume(
                "thread-cancel",
                now=120,
            )
            recoveries = registry.list_recovery_sessions()

        self.assertEqual(result, "cancelled")
        self.assertIsNotNone(cancelled)
        assert cancelled is not None
        self.assertEqual(cancelled.status, SessionStatus.LIMIT_BLOCKED)
        self.assertFalse(cancelled.auto_resume)
        self.assertIsNone(cancelled.next_attempt_at)
        self.assertEqual(cancelled.pids, (123,))
        self.assertEqual(cancelled.last_resume_result, "cancelled")
        self.assertEqual(
            cancelled.metadata["auto_resume_disabled_reason"],
            "user_cancelled",
        )
        self.assertIsNone(claim)
        self.assertEqual(second_result, "not_waiting")
        self.assertEqual([item.thread_id for item in recoveries], ["thread-cancel"])

    def test_cancel_marker_blocks_a_stale_queue_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry = MultiSessionRegistry(Path(temporary_directory) / "state")
            stale_session = TrackedSession(
                thread_id="thread-race",
                session_id="session-race",
                jsonl_path="/tmp/session.jsonl",
                cwd="/tmp",
                source="cli",
                status=SessionStatus.QUEUED,
                confidence=DetectionConfidence.OPEN_FILE,
                first_seen_at=10,
                last_seen_at=20,
                next_attempt_at=100,
                auto_resume=True,
                quota_blocked_at=80,
                last_resume_result="waiting_for_reset",
            )
            registry.upsert_session(stale_session)
            registry.cancel_queued_resume("thread-race", now=90)

            # 中文注释：模拟监控线程用点击前读到的旧对象覆盖 sessions 行。
            registry.upsert_session(stale_session)
            due = registry.has_due_session(200)
            claimed = registry.claim_due_session(200)
            cancellation_exists = registry.is_resume_cancelled("thread-race")

        self.assertTrue(cancellation_exists)
        self.assertFalse(due)
        self.assertIsNone(claimed)

    def test_marks_a_failed_resume_as_manually_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry = MultiSessionRegistry(Path(temporary_directory) / "state")
            registry.upsert_session(
                TrackedSession(
                    thread_id="thread-failed",
                    session_id="session-failed",
                    jsonl_path="/tmp/session.jsonl",
                    cwd="/tmp",
                    source="cli",
                    status=SessionStatus.FAILED,
                    confidence=DetectionConfidence.PERSISTED,
                    first_seen_at=10,
                    last_seen_at=20,
                    auto_resume=False,
                    quota_blocked_at=15,
                    last_error="resume 退出码为 -15",
                    last_resume_result="failed",
                )
            )

            result = registry.cancel_queued_resume("thread-failed", now=30)
            cancelled = registry.get_session("thread-failed")

        self.assertEqual(result, "cancelled")
        self.assertIsNotNone(cancelled)
        assert cancelled is not None
        self.assertEqual(cancelled.status, SessionStatus.FAILED)
        self.assertEqual(cancelled.last_resume_result, "cancelled")
        self.assertFalse(cancelled.auto_resume)
        self.assertIn("退出码为 -15", cancelled.last_error)

    def test_migrates_existing_database_before_saving_recovery_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory) / "state"
            state_dir.mkdir()
            database_file = state_dir / "monitor.sqlite3"
            connection = sqlite3.connect(database_file)
            try:
                connection.execute(
                    """
                    CREATE TABLE sessions (
                        thread_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        jsonl_path TEXT,
                        cwd TEXT,
                        source TEXT NOT NULL,
                        status TEXT NOT NULL,
                        confidence TEXT NOT NULL,
                        first_seen_at REAL NOT NULL,
                        last_seen_at REAL NOT NULL,
                        pids_json TEXT NOT NULL,
                        process_start_tokens_json TEXT NOT NULL,
                        last_offset INTEGER NOT NULL,
                        last_event_at REAL,
                        last_event_type TEXT,
                        last_error TEXT,
                        quota_reset_at REAL,
                        blocked_limit_ids_json TEXT NOT NULL,
                        next_attempt_at REAL,
                        auto_resume INTEGER NOT NULL,
                        resume_attempts INTEGER NOT NULL,
                        parent_thread_id TEXT,
                        root_thread_id TEXT,
                        last_exit_code INTEGER,
                        terminal INTEGER NOT NULL,
                        metadata_json TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()

            registry = MultiSessionRegistry(state_dir)
            registry.upsert_session(
                TrackedSession(
                    thread_id="thread-migrated",
                    session_id="session-migrated",
                    jsonl_path=None,
                    cwd=None,
                    source="cli",
                    status=SessionStatus.COMPLETED,
                    confidence=DetectionConfidence.PERSISTED,
                    first_seen_at=1,
                    last_seen_at=2,
                    terminal=True,
                    quota_blocked_at=1.5,
                    last_resume_started_at=2,
                    last_resume_finished_at=3,
                    last_resume_result="success",
                )
            )
            restored = registry.get_session("thread-migrated")

        self.assertEqual(restored.last_resume_result, "success")
        self.assertEqual(restored.quota_blocked_at, 1.5)

    def test_backfills_success_from_previous_resume_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory) / "state"
            registry = MultiSessionRegistry(state_dir)
            registry.upsert_session(
                TrackedSession(
                    thread_id="thread-previous",
                    session_id="session-previous",
                    jsonl_path=None,
                    cwd=None,
                    source="cli",
                    status=SessionStatus.COMPLETED,
                    confidence=DetectionConfidence.PERSISTED,
                    first_seen_at=1,
                    last_seen_at=3,
                    resume_attempts=1,
                    terminal=True,
                )
            )
            connection = sqlite3.connect(registry.database_file)
            try:
                connection.execute(
                    """
                    INSERT INTO resume_attempts (
                        thread_id, started_at, finished_at, returncode, error
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    ("thread-previous", 2, 3, 0, None),
                )
                connection.commit()
            finally:
                connection.close()

            reloaded = MultiSessionRegistry(state_dir)
            recoveries = reloaded.list_recovery_sessions()

        self.assertEqual(len(recoveries), 1)
        self.assertEqual(recoveries[0].last_resume_result, "success")
        self.assertEqual(recoveries[0].last_resume_started_at, 2)
        self.assertEqual(recoveries[0].last_resume_finished_at, 3)


if __name__ == "__main__":
    unittest.main()
