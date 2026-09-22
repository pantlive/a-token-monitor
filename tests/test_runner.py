"""Codex 运行器单元测试，不调用真实 Codex 或消耗额度。"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from token_monitor.runner import CodexRunner, RunnerConfig
from token_monitor.storage import StateStore


class FakeProcess:
    """用于模拟 subprocess.Popen 的最小进程对象。"""

    def __init__(self, lines: list[str], returncode: int, pid: int) -> None:
        self.stdout = iter(lines)
        self.returncode = returncode
        self.pid = pid

    def wait(self, timeout: float | None = None) -> int:
        """返回预设退出码。"""

        return self.returncode

    def poll(self) -> int:
        """返回预设退出码，表示模拟进程已经结束。"""

        return self.returncode

    def terminate(self) -> None:
        """模拟优雅终止。"""

    def kill(self) -> None:
        """模拟强制终止。"""


class RunnerTests(unittest.TestCase):
    """验证初始任务和额度中断后的 session 续跑。"""

    def test_successful_task_is_persisted_as_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = StateStore(Path(temporary_directory) / "state")
            process = FakeProcess(
                ['{"type":"thread.started","thread_id":"session-1"}\n'],
                returncode=0,
                pid=101,
            )
            runner = CodexRunner(store, RunnerConfig(reset_grace=0))

            with patch(
                "token_monitor.runner.subprocess.Popen",
                return_value=process,
            ) as popen:
                with contextlib.redirect_stdout(io.StringIO()):
                    result = runner.start(
                        cwd=Path(temporary_directory),
                        codex_path="codex",
                        prompt="完成任务",
                        continuation_prompt="继续任务",
                        codex_options=["--sandbox", "workspace-write"],
                    )

            state = store.load()
            self.assertEqual(result, 0)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.status.value, "completed")
            self.assertEqual(state.session_id, "session-1")
            self.assertEqual(
                popen.call_args.args[0][:4],
                [
                    "codex",
                    "exec",
                    "--json",
                    "--sandbox",
                ],
            )

    def test_quota_failure_resumes_same_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = StateStore(Path(temporary_directory) / "state")
            first_process = FakeProcess(
                [
                    '{"type":"thread.started","thread_id":"session-2"}\n',
                    '{"type":"turn.failed",'
                    '"rate_limit_reached_type":"primary",'
                    '"error":{"message":"usage limit reached",'
                    '"rate_limits":{"primary":{"resets_at":0}}}}\n',
                ],
                returncode=1,
                pid=102,
            )
            second_process = FakeProcess(
                ['{"type":"turn.completed"}\n'],
                returncode=0,
                pid=103,
            )
            runner = CodexRunner(
                store,
                RunnerConfig(
                    poll_interval=0.001,
                    reset_grace=0,
                    unknown_reset_wait=1,
                ),
            )

            with patch(
                "token_monitor.runner.subprocess.Popen",
                side_effect=[first_process, second_process],
            ) as popen:
                with contextlib.redirect_stdout(io.StringIO()):
                    result = runner.start(
                        cwd=Path(temporary_directory),
                        codex_path="codex",
                        prompt="完成长任务",
                        continuation_prompt="继续长任务",
                        codex_options=["--sandbox", "workspace-write"],
                    )

            state = store.load()
            self.assertEqual(result, 0)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.status.value, "completed")
            self.assertEqual(state.session_id, "session-2")
            self.assertEqual(state.retry_count, 1)
            self.assertEqual(popen.call_count, 2)
            resume_command = popen.call_args_list[1].args[0]
            self.assertEqual(
                resume_command[:4],
                ["codex", "exec", "--sandbox", "workspace-write"],
            )
            self.assertEqual(resume_command[4:6], ["resume", "--json"])
            self.assertIn("session-2", resume_command)
            self.assertIn("继续长任务", resume_command)

    def test_non_quota_failure_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = StateStore(Path(temporary_directory) / "state")
            process = FakeProcess(
                [
                    '{"type":"thread.started","thread_id":"session-3"}\n',
                    '{"type":"turn.failed","error":{"message":"network timeout"}}\n',
                ],
                returncode=1,
                pid=104,
            )
            runner = CodexRunner(store, RunnerConfig(reset_grace=0))

            with patch(
                "token_monitor.runner.subprocess.Popen",
                return_value=process,
            ) as popen:
                with contextlib.redirect_stdout(io.StringIO()):
                    result = runner.start(
                        cwd=Path(temporary_directory),
                        codex_path="codex",
                        prompt="普通任务",
                        continuation_prompt="继续普通任务",
                        codex_options=[],
                    )

            state = store.load()
            self.assertEqual(result, 1)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.status.value, "failed")
            self.assertEqual(popen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
