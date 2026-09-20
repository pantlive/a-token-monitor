"""状态存储单元测试。"""

from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from codex_reset_monitor.models import JobState, JobStatus
from codex_reset_monitor.storage import StateError, StateStore


class StateStoreTests(unittest.TestCase):
    """验证状态和原始输出日志可以安全恢复。"""

    def test_round_trip_and_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory) / "monitor"
            store = StateStore(state_dir)
            log_file = store.create_log_file("job-1")
            state = JobState.create(
                cwd=str(Path.cwd()),
                codex_path="codex",
                prompt="完成任务",
                continuation_prompt="继续任务",
                codex_options=["--sandbox", "workspace-write"],
                log_file=str(log_file),
            )
            state.job_id = "job-1"
            store.save(state)
            store.append_log(state, '{"type":"thread.started"}\n')

            restored = store.load()

            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.job_id, "job-1")
            self.assertEqual(restored.status, JobStatus.RUNNING)
            self.assertEqual(restored.codex_options, ["--sandbox", "workspace-write"])
            self.assertEqual(
                log_file.read_text(encoding="utf-8"),
                '{"type":"thread.started"}\n',
            )
            self.assertEqual(
                stat.S_IMODE(store.state_file.stat().st_mode),
                0o600,
            )
            self.assertEqual(stat.S_IMODE(log_file.stat().st_mode), 0o600)

    def test_second_monitor_cannot_acquire_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory) / "monitor"
            first_store = StateStore(state_dir)
            second_store = StateStore(state_dir)

            with first_store.lock():
                with self.assertRaises(StateError):
                    with second_store.lock():
                        pass


if __name__ == "__main__":
    unittest.main()
