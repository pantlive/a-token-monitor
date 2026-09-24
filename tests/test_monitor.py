"""多会话监控主流程测试，不调用真实 Codex 或真实额度后端。"""

from __future__ import annotations

import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from a_token_monitor.app_server import AppServerError
from a_token_monitor.discovery import JsonlSessionReader, ProcessObservation
from a_token_monitor.monitor import (
    AppServerThread,
    MonitorConfig,
    MultiSessionMonitor,
)
from a_token_monitor.multi_models import (
    DetectionConfidence,
    SessionStatus,
    TrackedSession,
)
from a_token_monitor.quota import QuotaSnapshot, QuotaWindow
from a_token_monitor.registry import MultiSessionRegistry


class _FakeAppServer:
    """提供只读额度和空会话列表的假 App Server。"""

    def __init__(self, snapshot: QuotaSnapshot) -> None:
        self.snapshot = snapshot
        self.read_count = 0

    def start(self) -> None:
        """模拟启动。"""

    def close(self) -> None:
        """模拟关闭。"""

    def drain_notifications(self) -> int:
        """没有待处理通知。"""

        return 0

    def read_rate_limits(self, now: float | None = None) -> QuotaSnapshot:
        """返回固定额度快照。"""

        self.read_count += 1
        return self.snapshot

    def list_threads(self) -> list[dict[str, object]]:
        """返回空的 App Server 活动列表。"""

        return []


class _FailingAppServer(_FakeAppServer):
    """模拟额度后端不可达的 App Server。"""

    def read_rate_limits(self, now: float | None = None) -> QuotaSnapshot:
        """抛出连接错误。"""

        raise AppServerError("模拟网络不可达")


class _FakeScanner:
    """可切换进程快照的扫描器。"""

    def __init__(self, observations: tuple[ProcessObservation, ...]) -> None:
        self.observations = observations

    def scan(self) -> tuple[ProcessObservation, ...]:
        """返回当前假进程快照。"""

        return self.observations


class _FakeResumeProcess:
    """模拟成功完成一次 session resume 的子进程。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.pid = 901
        self.returncode = 0
        self.stdout = iter(
            [
                '{"type":"thread.started","thread_id":"session-1"}\n',
                '{"type":"turn.completed"}\n',
            ]
        )

    def poll(self) -> int:
        """返回成功退出码。"""

        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """返回成功退出码。"""

        return self.returncode

    def terminate(self) -> None:
        """模拟终止。"""


def _write_session(path: Path, session_id: str, event: str = "task_started") -> None:
    """写入最小可解析的 Codex session JSONL。"""

    path.write_text(
        '{"type":"session_meta","payload":{'
        f'"session_id":"{session_id}","id":"{session_id}",'
        '"cwd":"/tmp","source":"cli"}}\n'
        f'{{"type":"{event}"}}\n',
        encoding="utf-8",
    )


class MonitorTests(unittest.TestCase):
    """验证多个活动文件和额度失败后的同 session 续跑。"""

    @staticmethod
    def _available_quota() -> QuotaSnapshot:
        """返回未耗尽的 Plus 额度快照。"""

        return QuotaSnapshot(
            observed_at=100,
            plan_type="plus",
            windows=(
                QuotaWindow(
                    limit_id="codex",
                    name="primary",
                    used_percent=20,
                    window_minutes=300,
                    resets_at=200,
                ),
            ),
        )

    def test_parses_camel_case_app_server_waiting_status(self) -> None:
        summary = MultiSessionMonitor._parse_app_thread(
            {
                "id": "thread-approval",
                "status": {"type": "waitingOnApproval"},
                "sourceKind": "cli",
            }
        )

        self.assertTrue(summary.active)
        self.assertTrue(summary.waiting_for_approval)

    def test_monitors_all_open_jsonl_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_root = root / "sessions"
            session_root.mkdir()
            first_path = session_root / "one.jsonl"
            second_path = session_root / "two.jsonl"
            _write_session(first_path, "session-1")
            _write_session(second_path, "session-2")
            scanner = _FakeScanner(
                (
                    ProcessObservation(11, "start-11", root, ("codex",), (first_path,)),
                    ProcessObservation(
                        12,
                        "start-12",
                        root,
                        ("codex",),
                        (second_path,),
                    ),
                )
            )
            registry = MultiSessionRegistry(root / "state")
            monitor = MultiSessionMonitor(
                registry=registry,
                config=MonitorConfig(auto_resume=False),
                session_root=session_root,
                app_server=_FakeAppServer(self._available_quota()),  # type: ignore[arg-type]
                process_scanner=scanner,  # type: ignore[arg-type]
            )

            monitor.scan_processes(now=100)
            sessions = registry.list_sessions(active_only=True)

        self.assertEqual(
            {session.session_id for session in sessions},
            {
                "session-1",
                "session-2",
            },
        )
        self.assertEqual({session.pids for session in sessions}, {(11,), (12,)})

    def test_active_scan_skips_legacy_zero_offset_on_large_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_root = root / "sessions"
            session_root.mkdir()
            jsonl_path = session_root / "large.jsonl"
            jsonl_path.write_bytes(
                b'{"type":"session_meta","payload":{"session_id":"large"}}\n'
                + b'{"type":"response_item","payload":{"text":"'
                + (b"x" * (MultiSessionMonitor._ACTIVE_CATCHUP_SKIP_BYTES + 1))
                + b'"}}\n'
                + b'{"type":"turn.started"}\n'
            )
            scanner = _FakeScanner(
                (
                    ProcessObservation(
                        11,
                        "start-11",
                        root,
                        ("codex",),
                        (jsonl_path,),
                    ),
                )
            )
            registry = MultiSessionRegistry(root / "state")
            registry.upsert_session(
                TrackedSession(
                    thread_id="large",
                    session_id="large",
                    jsonl_path=str(jsonl_path),
                    cwd=str(root),
                    source="cli",
                    status=SessionStatus.RUNNING,
                    confidence=DetectionConfidence.PERSISTED,
                    first_seen_at=100,
                    last_seen_at=100,
                    last_offset=0,
                )
            )
            monitor = MultiSessionMonitor(
                registry=registry,
                config=MonitorConfig(auto_resume=False),
                session_root=session_root,
                app_server=_FakeAppServer(self._available_quota()),  # type: ignore[arg-type]
                process_scanner=scanner,  # type: ignore[arg-type]
            )

            monitor.scan_processes(now=100)
            session = registry.get_session("large")
            file_size = jsonl_path.stat().st_size

        self.assertIsNotNone(session)
        assert session is not None
        self.assertGreaterEqual(
            session.last_offset,
            file_size - MultiSessionMonitor._ACTIVE_SESSION_READ_BYTES,
        )
        self.assertLessEqual(session.last_offset, file_size)

    def test_app_server_discovery_starts_large_file_from_the_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            jsonl_path = root / "large.jsonl"
            jsonl_path.write_bytes(b"x" * (JsonlSessionReader._DEFAULT_INITIAL_BYTES * 2))
            registry = MultiSessionRegistry(root / "state")
            monitor = MultiSessionMonitor(
                registry=registry,
                config=MonitorConfig(auto_resume=False),
                session_root=root,
                app_server=_FakeAppServer(self._available_quota()),  # type: ignore[arg-type]
                process_scanner=_FakeScanner(()),  # type: ignore[arg-type]
            )

            monitor._upsert_app_thread(
                AppServerThread(
                    thread_id="large-thread",
                    session_id="large",
                    status="running",
                    active=True,
                    waiting_for_approval=False,
                    jsonl_path=jsonl_path,
                ),
                now=100,
            )
            session = registry.get_session("large-thread")

        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(
            session.last_offset,
            JsonlSessionReader._DEFAULT_INITIAL_BYTES,
        )

    def test_quota_failure_does_not_resume_after_process_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_root = root / "sessions"
            session_root.mkdir()
            jsonl_path = session_root / "one.jsonl"
            _write_session(jsonl_path, "session-1", event="turn.failed")
            with jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    '{"type":"turn.failed","rate_limit_reached_type":"primary",'
                    '"error":{"message":"usage limit reached",'
                    '"resets_at":100}}\n'
                )
            scanner = _FakeScanner(
                (ProcessObservation(11, "start-11", root, ("codex",), (jsonl_path,)),)
            )
            app_server = _FakeAppServer(self._available_quota())
            registry = MultiSessionRegistry(root / "state")
            monitor = MultiSessionMonitor(
                registry=registry,
                config=MonitorConfig(
                    auto_resume=True,
                    reset_grace=0,
                    unknown_reset_wait=10,
                    codex_home=root / ".codex-work",
                    account_name="codex-work",
                    account_id="account-work",
                ),
                session_root=session_root,
                app_server=app_server,  # type: ignore[arg-type]
                process_scanner=scanner,  # type: ignore[arg-type]
            )
            monitor.scan_processes(now=100)
            blocked = registry.get_session("session-1")
            self.assertEqual(blocked.status, SessionStatus.LIMIT_BLOCKED)
            scanner.observations = ()
            monitor.scan_processes(now=101)
            monitor.finalize_sessions(now=101)
            finished = registry.get_session("session-1")
            started = monitor.resume_due(now=101)
            result = registry.get_session("session-1")
            monitor.close()

        self.assertEqual(finished.status, SessionStatus.LIMIT_BLOCKED)
        self.assertFalse(finished.auto_resume)
        self.assertIsNone(finished.next_attempt_at)
        self.assertIn("进程已退出，不自动恢复", finished.last_error)
        self.assertEqual(started, 0)
        self.assertEqual(result.status, SessionStatus.LIMIT_BLOCKED)
        self.assertEqual(result.resume_attempts, 0)

    def test_quota_failure_queues_while_process_still_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_root = root / "sessions"
            session_root.mkdir()
            jsonl_path = session_root / "one.jsonl"
            _write_session(jsonl_path, "session-1", event="turn.failed")
            with jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    '{"type":"turn.failed","rate_limit_reached_type":"primary",'
                    '"error":{"message":"usage limit reached",'
                    '"resets_at":150}}\n'
                )
            scanner = _FakeScanner(
                (ProcessObservation(11, "start-11", root, ("codex",), (jsonl_path,)),)
            )
            registry = MultiSessionRegistry(root / "state")
            monitor = MultiSessionMonitor(
                registry=registry,
                config=MonitorConfig(
                    auto_resume=True,
                    reset_grace=0,
                    account_id="account-work",
                ),
                session_root=session_root,
                app_server=_FakeAppServer(self._available_quota()),  # type: ignore[arg-type]
                process_scanner=scanner,  # type: ignore[arg-type]
            )
            monitor.scan_processes(now=100)
            monitor.finalize_sessions(now=100)
            queued = registry.get_session("session-1")
            monitor.close()

        self.assertIsNotNone(queued)
        assert queued is not None
        self.assertEqual(queued.status, SessionStatus.QUEUED)
        self.assertTrue(queued.auto_resume)
        self.assertEqual(queued.next_attempt_at, 150)
        self.assertEqual(queued.pids, (11,))

    def test_cancelled_resume_is_not_queued_again_while_process_is_alive(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "session.jsonl"
            _write_session(path, "session-cancelled", event="turn.failed")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    '{"type":"turn.failed","rate_limit_reached_type":"primary",'
                    '"error":{"message":"usage limit reached",'
                    '"resets_at":200}}\n'
                )
            scanner = _FakeScanner(
                (ProcessObservation(11, "start-11", root, ("codex",), (path,)),)
            )
            registry = MultiSessionRegistry(root / "state")
            registry.upsert_session(
                TrackedSession(
                    thread_id="session-cancelled",
                    session_id="session-cancelled",
                    jsonl_path=str(path),
                    cwd=str(root),
                    source="cli",
                    status=SessionStatus.LIMIT_BLOCKED,
                    confidence=DetectionConfidence.OPEN_FILE,
                    first_seen_at=90,
                    last_seen_at=100,
                    pids=(11,),
                    auto_resume=False,
                    quota_blocked_at=100,
                    last_resume_result="cancelled",
                    account_id="account-work",
                    metadata={
                        "quota_blocked": "1",
                        "auto_resume_disabled_reason": "user_cancelled",
                    },
                )
            )
            monitor = MultiSessionMonitor(
                registry=registry,
                config=MonitorConfig(
                    auto_resume=True,
                    account_id="account-work",
                ),
                session_root=root,
                app_server=_FakeAppServer(self._available_quota()),  # type: ignore[arg-type]
                process_scanner=scanner,  # type: ignore[arg-type]
            )

            monitor.scan_processes(now=109)
            monitor.finalize_sessions(now=110)
            cancelled = registry.get_session("session-cancelled")
            monitor.close()

        self.assertIsNotNone(cancelled)
        assert cancelled is not None
        self.assertEqual(cancelled.status, SessionStatus.LIMIT_BLOCKED)
        self.assertFalse(cancelled.auto_resume)
        self.assertIsNone(cancelled.next_attempt_at)
        self.assertEqual(cancelled.last_resume_result, "cancelled")

    def test_resume_due_stops_live_process_then_starts_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_root = root / "sessions"
            session_root.mkdir()
            jsonl_path = session_root / "one.jsonl"
            _write_session(jsonl_path, "session-1", event="turn.failed")
            with jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    '{"type":"turn.failed","rate_limit_reached_type":"primary",'
                    '"error":{"message":"usage limit reached",'
                    '"resets_at":100}}\n'
                )
            scanner = _FakeScanner(
                (ProcessObservation(11, "start-11", root, ("codex",), (jsonl_path,)),)
            )
            registry = MultiSessionRegistry(root / "state")
            monitor = MultiSessionMonitor(
                registry=registry,
                config=MonitorConfig(
                    auto_resume=True,
                    reset_grace=0,
                    account_id="account-work",
                    continuation_prompt="continue",
                ),
                session_root=session_root,
                app_server=_FakeAppServer(self._available_quota()),  # type: ignore[arg-type]
                process_scanner=scanner,  # type: ignore[arg-type]
            )
            alive = {11}
            signaled: list[tuple[int, int]] = []

            def signal_pid(pid: int, sig: int) -> None:
                signaled.append((pid, sig))
                alive.discard(pid)
                scanner.observations = tuple(
                    item for item in scanner.observations if item.pid != pid
                )

            monitor._signal_pid = signal_pid  # type: ignore[method-assign]
            monitor._pid_is_alive = lambda pid: pid in alive  # type: ignore[method-assign]
            monitor._PROCESS_STOP_TIMEOUT = 0.01
            monitor._PROCESS_STOP_POLL = 0.001
            monitor.scan_processes(now=100)
            monitor.finalize_sessions(now=100)
            with patch(
                "a_token_monitor.monitor.subprocess.Popen",
                _FakeResumeProcess,
            ):
                started = monitor.resume_due(now=100)
                worker = monitor._resume_workers.get("session-1")
                if worker is not None:
                    worker.thread.join(timeout=2)
            result = registry.get_session("session-1")
            monitor.close()

        self.assertEqual(started, 1)
        self.assertIn((11, signal.SIGTERM), signaled)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, SessionStatus.COMPLETED)
        self.assertEqual(result.last_resume_result, "success")

    def test_uses_local_jsonl_quota_when_app_server_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_root = root / "sessions"
            session_root.mkdir()
            path = session_root / "one.jsonl"
            path.write_text(
                '{"timestamp":100,"type":"token_count",'
                '"rate_limits":{"primary":{"used_percent":55,'
                '"window_minutes":300,"resets_at":200}}}\n',
                encoding="utf-8",
            )
            scanner = _FakeScanner(
                (ProcessObservation(11, "start-11", root, ("codex",), (path,)),)
            )
            registry = MultiSessionRegistry(root / "state")
            monitor = MultiSessionMonitor(
                registry=registry,
                config=MonitorConfig(auto_resume=False),
                session_root=session_root,
                app_server=_FailingAppServer(self._available_quota()),  # type: ignore[arg-type]
                process_scanner=scanner,  # type: ignore[arg-type]
            )

            snapshot = monitor.refresh_quota(force=True, now=110)

        self.assertEqual(snapshot.source, "session-jsonl-fallback")
        self.assertEqual(snapshot.window("codex", "primary").used_percent, 55)

    def test_reschedules_queue_while_account_window_is_still_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = MultiSessionRegistry(root / "state")
            registry.upsert_session(
                TrackedSession(
                    thread_id="thread-1",
                    session_id="session-1",
                    jsonl_path=None,
                    cwd=str(root),
                    source="cli",
                    status=SessionStatus.QUEUED,
                    confidence=DetectionConfidence.PERSISTED,
                    first_seen_at=1,
                    last_seen_at=1,
                    quota_reset_at=200,
                    next_attempt_at=100,
                )
            )
            blocked = QuotaSnapshot(
                observed_at=100,
                windows=(
                    QuotaWindow(
                        limit_id="codex",
                        name="primary",
                        used_percent=100,
                        window_minutes=300,
                        resets_at=200,
                        reached_type="primary",
                    ),
                ),
            )
            monitor = MultiSessionMonitor(
                registry=registry,
                config=MonitorConfig(reset_grace=30),
                app_server=_FakeAppServer(blocked),  # type: ignore[arg-type]
            )

            started = monitor.resume_due(now=100)
            deferred = registry.get_session("thread-1")

        self.assertEqual(started, 0)
        self.assertEqual(deferred.status, SessionStatus.LIMIT_BLOCKED)
        self.assertFalse(deferred.auto_resume)
        self.assertIsNone(deferred.next_attempt_at)

    def test_does_not_resume_queue_owned_by_another_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = MultiSessionRegistry(root / "state")
            registry.upsert_session(
                TrackedSession(
                    thread_id="thread-other-account",
                    session_id="session-other-account",
                    jsonl_path=None,
                    cwd=str(root),
                    source="cli",
                    status=SessionStatus.QUEUED,
                    confidence=DetectionConfidence.PERSISTED,
                    first_seen_at=1,
                    last_seen_at=1,
                    next_attempt_at=100,
                    account_id="account-personal",
                )
            )
            app_server = _FakeAppServer(self._available_quota())
            monitor = MultiSessionMonitor(
                registry=registry,
                config=MonitorConfig(
                    auto_resume=True,
                    account_name="codex-work",
                    account_id="account-work",
                ),
                app_server=app_server,  # type: ignore[arg-type]
            )

            started = monitor.resume_due(now=100)
            preserved = registry.get_session("thread-other-account")
            monitor.close()

        self.assertEqual(started, 0)
        self.assertEqual(preserved.status, SessionStatus.QUEUED)
        self.assertEqual(preserved.next_attempt_at, 100)
        self.assertEqual(app_server.read_count, 0)

    def test_does_not_resume_queue_without_verified_account_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = MultiSessionRegistry(root / "state")
            registry.upsert_session(
                TrackedSession(
                    thread_id="thread-unknown-account",
                    session_id="session-unknown-account",
                    jsonl_path=None,
                    cwd=str(root),
                    source="cli",
                    status=SessionStatus.QUEUED,
                    confidence=DetectionConfidence.PERSISTED,
                    first_seen_at=1,
                    last_seen_at=1,
                    next_attempt_at=100,
                )
            )
            app_server = _FakeAppServer(self._available_quota())
            monitor = MultiSessionMonitor(
                registry=registry,
                config=MonitorConfig(
                    auto_resume=True,
                    account_id="account-work",
                ),
                app_server=app_server,  # type: ignore[arg-type]
            )

            started = monitor.resume_due(now=100)
            preserved = registry.get_session("thread-unknown-account")
            monitor.close()

        self.assertEqual(started, 0)
        self.assertIsNone(preserved.account_id)
        self.assertEqual(preserved.status, SessionStatus.LIMIT_BLOCKED)
        self.assertFalse(preserved.auto_resume)
        self.assertEqual(app_server.read_count, 0)

    def test_does_not_rewrite_other_account_queue_during_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = MultiSessionRegistry(root / "state")
            registry.upsert_session(
                TrackedSession(
                    thread_id="thread-other-account-finalize",
                    session_id="session-other-account-finalize",
                    jsonl_path=None,
                    cwd=str(root),
                    source="cli",
                    status=SessionStatus.LIMIT_BLOCKED,
                    confidence=DetectionConfidence.PERSISTED,
                    first_seen_at=1,
                    last_seen_at=1,
                    quota_reset_at=200,
                    next_attempt_at=100,
                    account_id="account-personal",
                    metadata={"quota_blocked": "1"},
                )
            )
            monitor = MultiSessionMonitor(
                registry=registry,
                config=MonitorConfig(account_id="account-work"),
                app_server=_FakeAppServer(self._available_quota()),  # type: ignore[arg-type]
            )

            monitor.finalize_sessions(now=100)
            preserved = registry.get_session("thread-other-account-finalize")
            monitor.close()

        self.assertEqual(preserved.status, SessionStatus.LIMIT_BLOCKED)
        self.assertEqual(preserved.next_attempt_at, 100)

    def test_unidentified_dead_quota_session_is_not_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = MultiSessionRegistry(root / "state")
            registry.upsert_session(
                TrackedSession(
                    thread_id="thread-unidentified-dead",
                    session_id="session-unidentified-dead",
                    jsonl_path=None,
                    cwd=str(root),
                    source="cli",
                    status=SessionStatus.LIMIT_BLOCKED,
                    confidence=DetectionConfidence.PERSISTED,
                    first_seen_at=1,
                    last_seen_at=1,
                    auto_resume=True,
                    metadata={"quota_blocked": "1"},
                )
            )
            monitor = MultiSessionMonitor(
                registry=registry,
                config=MonitorConfig(account_id="account-work"),
                app_server=_FakeAppServer(self._available_quota()),  # type: ignore[arg-type]
                process_scanner=_FakeScanner(()),  # type: ignore[arg-type]
            )

            monitor.finalize_sessions(now=100)
            dropped = registry.get_session("thread-unidentified-dead")
            monitor.close()

        self.assertIsNotNone(dropped)
        assert dropped is not None
        self.assertEqual(dropped.status, SessionStatus.LIMIT_BLOCKED)
        self.assertFalse(dropped.auto_resume)
        self.assertIn("进程已退出，不自动恢复", dropped.last_error or "")

    def test_does_not_force_quota_query_without_due_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app_server = _FakeAppServer(self._available_quota())
            monitor = MultiSessionMonitor(
                registry=MultiSessionRegistry(root / "state"),
                config=MonitorConfig(auto_resume=True),
                app_server=app_server,  # type: ignore[arg-type]
            )

            started = monitor.resume_due(now=100)
            monitor.close()

        self.assertEqual(started, 0)
        self.assertEqual(app_server.read_count, 0)

    def test_refreshes_quota_only_after_configured_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app_server = _FakeAppServer(self._available_quota())
            monitor = MultiSessionMonitor(
                registry=MultiSessionRegistry(root / "state"),
                config=MonitorConfig(quota_interval=300),
                app_server=app_server,  # type: ignore[arg-type]
            )

            monitor.refresh_quota(force=True, now=100)
            monitor.refresh_quota(now=101)
            monitor.refresh_quota(now=401)
            monitor.close()

        self.assertEqual(app_server.read_count, 2)

    def test_does_not_resume_from_stale_quota_after_query_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_root = root / "sessions"
            session_root.mkdir()
            registry = MultiSessionRegistry(root / "state")
            registry.save_quota(self._available_quota())
            registry.upsert_session(
                TrackedSession(
                    thread_id="thread-stale",
                    session_id="session-stale",
                    jsonl_path=None,
                    cwd=str(root),
                    source="cli",
                    status=SessionStatus.QUEUED,
                    confidence=DetectionConfidence.PERSISTED,
                    first_seen_at=1,
                    last_seen_at=1,
                    next_attempt_at=100,
                )
            )
            monitor = MultiSessionMonitor(
                registry=registry,
                config=MonitorConfig(unknown_reset_wait=20),
                session_root=session_root,
                app_server=_FailingAppServer(self._available_quota()),  # type: ignore[arg-type]
            )

            started = monitor.resume_due(now=100)
            queued = registry.get_session("thread-stale")
            monitor.close()

        self.assertEqual(started, 0)
        self.assertEqual(queued.status, SessionStatus.LIMIT_BLOCKED)
        self.assertFalse(queued.auto_resume)
        self.assertIsNone(queued.next_attempt_at)

    def test_task_complete_usage_limit_is_not_resumed_without_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_root = root / "sessions"
            session_root.mkdir()
            jsonl_path = session_root / "limited.jsonl"
            jsonl_path.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "error": {
                                "message": "You've hit your usage limit.",
                                "codex_error_info": "usage_limit_exceeded",
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            registry = MultiSessionRegistry(root / "state")
            registry.upsert_session(
                TrackedSession(
                    thread_id="limited",
                    session_id="limited",
                    jsonl_path=str(jsonl_path),
                    cwd=str(root),
                    source="cli",
                    status=SessionStatus.RUNNING,
                    confidence=DetectionConfidence.PERSISTED,
                    first_seen_at=100,
                    last_seen_at=100,
                    last_offset=jsonl_path.stat().st_size,
                    last_event_type="task_complete",
                    auto_resume=False,
                    terminal=True,
                    account_id="account-work",
                )
            )
            monitor = MultiSessionMonitor(
                registry=registry,
                config=MonitorConfig(
                    auto_resume=True,
                    reset_grace=0,
                    account_id="account-work",
                ),
                session_root=session_root,
                app_server=_FakeAppServer(self._available_quota()),  # type: ignore[arg-type]
                process_scanner=_FakeScanner(()),  # type: ignore[arg-type]
            )

            monitor._recheck_completed_quota(now=101)
            monitor.finalize_sessions(now=101)
            session = registry.get_session("limited")

        self.assertIsNotNone(session)
        assert session is not None
        self.assertFalse(session.auto_resume)
        self.assertEqual(session.metadata.get("quota_blocked"), "1")
        self.assertEqual(session.status, SessionStatus.LIMIT_BLOCKED)
        self.assertIn("进程已退出，不自动恢复", session.last_error or "")

    def test_daemon_lifecycle_can_start_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_root = root / "sessions"
            session_root.mkdir()
            app_server = _FakeAppServer(self._available_quota())
            monitor = MultiSessionMonitor(
                registry=MultiSessionRegistry(root / "state"),
                config=MonitorConfig(
                    auto_resume=False,
                    dashboard=True,
                    dashboard_port=0,
                ),
                session_root=session_root,
                app_server=app_server,  # type: ignore[arg-type]
                process_scanner=_FakeScanner(()),  # type: ignore[arg-type]
            )
            monitor._stop_event.set()

            result = monitor.run()

        self.assertEqual(result, 0)
        self.assertEqual(app_server.read_count, 1)


class QuotaHealthTests(unittest.TestCase):
    """验证 refresh_quota 的成功/失败埋点和 quota_health 的形状。"""

    def test_success_marks_last_success_and_clears_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_root = root / "sessions"
            session_root.mkdir()
            monitor = MultiSessionMonitor(
                registry=MultiSessionRegistry(root / "state"),
                config=MonitorConfig(auto_resume=False),
                session_root=session_root,
                app_server=_FakeAppServer(MonitorTests._available_quota()),  # type: ignore[arg-type]
                process_scanner=_FakeScanner(()),  # type: ignore[arg-type]
            )

            monitor.refresh_quota(force=True, now=110)
            health = monitor.quota_health()

        self.assertEqual(monitor._last_quota_success_at, 110)
        self.assertIsNone(monitor._last_quota_error)
        self.assertEqual(health["last_success_at"], 110)
        self.assertIsNone(health["last_error"])
        self.assertTrue(health["is_current"])
        # 未 start 时 App Server 进程为空属于 starting,不算可用也不算故障。
        self.assertFalse(health["app_server_available"])

    def test_failure_without_fallback_records_sanitized_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_root = root / "sessions"
            session_root.mkdir()
            monitor = MultiSessionMonitor(
                registry=MultiSessionRegistry(root / "state"),
                config=MonitorConfig(auto_resume=False),
                session_root=session_root,
                app_server=_FailingAppServer(MonitorTests._available_quota()),  # type: ignore[arg-type]
                process_scanner=_FakeScanner(()),  # type: ignore[arg-type]
            )

            monitor.refresh_quota(force=True, now=110)
            health = monitor.quota_health()

        self.assertIsNone(monitor._last_quota_success_at)
        self.assertEqual(monitor._last_quota_error, "模拟网络不可达")
        self.assertIsNone(health["last_success_at"])
        self.assertEqual(health["last_error"], "模拟网络不可达")
        self.assertFalse(health["is_current"])

    def test_fallback_success_updates_success_time_but_keeps_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_root = root / "sessions"
            session_root.mkdir()
            path = session_root / "one.jsonl"
            path.write_text(
                '{"timestamp":100,"type":"token_count",'
                '"rate_limits":{"primary":{"used_percent":55,'
                '"window_minutes":300,"resets_at":200}}}\n',
                encoding="utf-8",
            )
            scanner = _FakeScanner(
                (ProcessObservation(11, "start-11", root, ("codex",), (path,)),)
            )
            monitor = MultiSessionMonitor(
                registry=MultiSessionRegistry(root / "state"),
                config=MonitorConfig(auto_resume=False),
                session_root=session_root,
                app_server=_FailingAppServer(MonitorTests._available_quota()),  # type: ignore[arg-type]
                process_scanner=scanner,  # type: ignore[arg-type]
            )

            monitor.refresh_quota(force=True, now=110)
            health = monitor.quota_health()

        # fallback 成功算一次成功(数据仍新鲜),但保留 App Server 的降级原因。
        self.assertEqual(monitor._last_quota_success_at, 110)
        self.assertEqual(monitor._last_quota_error, "模拟网络不可达")
        self.assertTrue(health["is_current"])

    def test_quota_health_shape_and_started_app_server(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_root = root / "sessions"
            session_root.mkdir()
            monitor = MultiSessionMonitor(
                registry=MultiSessionRegistry(root / "state"),
                config=MonitorConfig(auto_resume=False),
                session_root=session_root,
                app_server=_FakeAppServer(MonitorTests._available_quota()),  # type: ignore[arg-type]
                process_scanner=_FakeScanner(()),  # type: ignore[arg-type]
            )

            health = monitor.quota_health()
            # 模拟已启动且进程存活的 App Server。
            monitor._started = True
            monitor.app_server.process = _LiveProcess()  # type: ignore[attr-defined]
            available = monitor.quota_health()["app_server_available"]

        self.assertEqual(
            set(health),
            {"last_success_at", "last_error", "is_current", "app_server_available"},
        )
        self.assertIsNone(health["last_success_at"])
        self.assertIsNone(health["last_error"])
        self.assertFalse(health["is_current"])
        self.assertFalse(health["app_server_available"])
        self.assertTrue(available)


class _LiveProcess:
    """模拟仍在运行的 App Server 子进程。"""

    def poll(self) -> None:
        """返回 None 表示进程仍存活。"""

        return None


class MonitorConfigRetentionTests(unittest.TestCase):
    """验证用量/会话历史保留天数的默认值和范围校验。"""

    def test_defaults_match_retention_module(self) -> None:
        config = MonitorConfig()

        self.assertEqual(config.usage_retention_days, 90.0)
        self.assertEqual(config.session_retention_days, 30.0)

    def test_retention_days_must_be_positive_and_bounded(self) -> None:
        for field_name in ("usage_retention_days", "session_retention_days"):
            for invalid in (0.0, -1.0, 3650.1):
                with self.assertRaises(ValueError, msg=f"{field_name}={invalid}"):
                    MonitorConfig(**{field_name: invalid})
            # 边界值 3650 合法。
            MonitorConfig(**{field_name: 3650.0})


if __name__ == "__main__":
    unittest.main()
