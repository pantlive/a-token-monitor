"""Windows 进程后端测试。

ctypes 原语（Toolhelp32 / Restart Manager / GetExtendedTcpTable）在 Linux 上无法执行，
这里用假数据替换那几个 ``_windows_*`` 原语，验证解析、组装与分配逻辑；同时覆盖
``os.kill(pid, 0)`` 这个 Windows 陷阱的替代实现。
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from token_monitor import process_backend
from token_monitor.agents import RunningAgent, scan_running_agents
from token_monitor.process_backend import ObservedConnection, ObservedProcess


def _process_row(
    pid: int,
    *,
    ppid: int = 1,
    image: str,
    command: tuple[str, ...] | None = None,
    created: str = "windows:133000000000000000",
) -> dict[str, object]:
    """构造一行 Toolhelp32 观测结果。"""

    return {
        "pid": pid,
        "ppid": ppid,
        "exe": image.replace("\\", "/").rsplit("/", 1)[-1],
        "image": image,
        "created": created,
        "command": command or (),
    }


class WindowsBackendSelectionTests(unittest.TestCase):
    """验证 Windows 后端的选择条件。"""

    def test_windows_backend_when_proc_missing(self) -> None:
        with (
            mock.patch.object(
                process_backend,
                "PROC_ROOT",
                Path("/nonexistent-proc"),
            ),
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.object(process_backend, "netlink_available", return_value=False),
        ):
            self.assertEqual(process_backend.select_backend(), "windows")
            self.assertIn("Windows", process_backend.netlink_reason() or "")

    def test_scan_process_connections_dispatches_by_backend(self) -> None:
        with (
            mock.patch.object(
                process_backend,
                "PROC_ROOT",
                Path("/nonexistent-proc"),
            ),
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.object(
                process_backend,
                "scan_windows_connections",
                return_value={7: (ObservedConnection(80, "1.1.1.1", 443),)},
            ) as windows,
        ):
            process_backend.reset_cache()
            connections = process_backend.scan_process_connections((7,))

        self.assertIn(7, connections)
        windows.assert_called_once()


class WindowsProcessParsingTests(unittest.TestCase):
    """验证解析与产品识别，不触碰真实 Windows API。"""

    def test_parse_processes_uses_image_and_ppid(self) -> None:
        rows = (
            _process_row(
                100,
                image="C:\\Users\\dev\\AppData\\Roaming\\npm\\codex.exe",
                command=("C:\\Users\\dev\\AppData\\Roaming\\npm\\codex.exe", "exec"),
            ),
            _process_row(200, ppid=100, image="C:\\Windows\\System32\\notepad.exe"),
            {"pid": "bad", "ppid": 1},
        )

        processes = process_backend._parse_windows_processes(rows)

        self.assertEqual(sorted(processes), [100, 200])
        self.assertEqual(processes[100].ppid, 1)
        self.assertEqual(processes[100].comm, "codex.exe")
        self.assertEqual(processes[100].start_token, "windows:133000000000000000")
        # 没有命令行时退回镜像路径，便于识别产品
        self.assertEqual(processes[200].command, ("C:\\Windows\\System32\\notepad.exe",))
        self.assertIsNone(processes[100].cwd)

    def test_scan_agents_matches_products(self) -> None:
        rows = (
            _process_row(100, image="C:\\npm\\codex.exe"),
            _process_row(200, image="C:\\npm\\claude.exe"),
        )
        with (
            mock.patch.object(
                process_backend,
                "_windows_process_rows",
                return_value=rows,
            ),
            mock.patch.object(process_backend, "_windows_session_owners", return_value={}),
        ):
            process_backend.reset_cache()
            agents = process_backend.scan_windows_agents(products=("claude",))

        self.assertEqual([agent.pid for agent in agents], [200])
        self.assertEqual(agents[0].product, "claude")

    def test_session_owners_are_assigned_to_matching_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sessions = root / "sessions"
            sessions.mkdir()
            transcript = sessions / "session-1.jsonl"
            transcript.write_text("", encoding="utf-8")
            rows = (
                _process_row(100, image="C:\\npm\\codex.exe"),
                _process_row(999, image="C:\\npm\\codex.exe"),
            )
            with (
                mock.patch.object(
                    process_backend,
                    "_windows_process_rows",
                    return_value=rows,
                ),
                mock.patch.object(
                    process_backend,
                    "_windows_session_owners",
                    return_value={transcript: (100, 555)},
                ),
            ):
                process_backend.reset_cache()
                agents = process_backend.scan_windows_agents(
                    products=("codex",),
                    session_roots=(sessions,),
                )

        found = {agent.pid: agent for agent in agents}
        self.assertEqual(found[100].open_paths, (transcript,))
        # 未通过产品过滤的 PID（555）不会被带进来
        self.assertEqual(found[999].open_paths, ())

    def test_agents_ignore_pids_and_session_roots_are_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sessions = root / "sessions"
            sessions.mkdir()
            transcript = sessions / "state.json"
            transcript.write_text("{}", encoding="utf-8")
            rows = (_process_row(100, image="C:\\npm\\codex.exe"),)
            with (
                mock.patch.object(
                    process_backend,
                    "_windows_process_rows",
                    return_value=rows,
                ),
                mock.patch.object(
                    process_backend,
                    "_windows_session_owners",
                    return_value={transcript: (100,)},
                ) as owners,
                mock.patch.object(
                    process_backend,
                    "PROC_ROOT",
                    Path("/nonexistent-proc"),
                ),
                mock.patch.object(sys, "platform", "win32"),
            ):
                process_backend.reset_cache()
                agents = scan_running_agents(
                    products=("codex",),
                    session_roots=(sessions,),
                )

        self.assertEqual([agent.pid for agent in agents], [100])
        self.assertEqual(agents[0].open_paths, (transcript,))
        owners.assert_called_once()

    def test_recent_session_files_filters_by_age_and_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fresh = root / "fresh.jsonl"
            fresh.write_text("", encoding="utf-8")
            old = root / "old.jsonl"
            old.write_text("", encoding="utf-8")
            other = root / "notes.txt"
            other.write_text("", encoding="utf-8")
            past = time.time() - 7200
            import os

            os.utime(old, (past, past))
            os.utime(other, (past, past))

            files = process_backend._recent_session_files(
                (root,),
                time.time(),
                window_seconds=3600,
            )

        self.assertEqual(files, (fresh,))

    def test_file_owners_skips_missing_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "a.jsonl"
            second = root / "b.jsonl"
            first.write_text("", encoding="utf-8")
            second.write_text("", encoding="utf-8")

            def owner_pids(path: str) -> tuple[int, ...]:
                if path.endswith("a.jsonl"):
                    return (10, 11)
                raise OSError("拒绝访问")

            with mock.patch.object(
                process_backend,
                "_windows_file_owner_pids",
                side_effect=owner_pids,
            ):
                owners = process_backend._windows_file_owners((first, second))

        self.assertEqual(owners, {first: (10, 11)})


class WindowsDiscoveryTests(unittest.TestCase):
    """验证 Codex 的 ProcessScanner 在 Windows 上也走平台后端。"""

    def test_process_scanner_uses_restart_manager_owners(self) -> None:
        from token_monitor.discovery import ProcessScanner

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session = root / "sessions" / "rollout-2026.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text("", encoding="utf-8")
            rows = (
                _process_row(
                    100,
                    image="C:\\npm\\codex.exe",
                    command=("C:\\npm\\codex.exe", "exec"),
                ),
            )
            with (
                mock.patch.object(
                    process_backend,
                    "PROC_ROOT",
                    Path("/nonexistent-proc"),
                ),
                mock.patch.object(sys, "platform", "win32"),
                mock.patch.object(
                    process_backend,
                    "_windows_process_rows",
                    return_value=rows,
                ),
                mock.patch.object(
                    process_backend,
                    "_windows_file_owner_pids",
                    return_value=(100,),
                ),
            ):
                process_backend.reset_cache()
                observations = ProcessScanner(
                    session_root=root / "sessions",
                    proc_root=None,
                ).scan()

        self.assertEqual([item.pid for item in observations], [100])
        self.assertEqual(observations[0].open_jsonl_paths, (session,))
        self.assertEqual(observations[0].command, ("C:\\npm\\codex.exe", "exec"))


class WindowsConnectionParsingTests(unittest.TestCase):
    """验证 TCP 表行的分组与监听判断。"""

    def test_parse_connections_groups_by_pid(self) -> None:
        rows = (
            {
                "state": 5,
                "local_ip": "10.0.0.9",
                "local_port": 52344,
                "remote_ip": "93.184.216.34",
                "remote_port": 443,
                "pid": 100,
            },
            {
                "state": 2,
                "local_ip": "0.0.0.0",
                "local_port": 3080,
                "remote_ip": "0.0.0.0",
                "remote_port": 0,
                "pid": 100,
            },
            {
                "state": 5,
                "local_ip": "10.0.0.9",
                "local_port": 1,
                "remote_ip": "1.1.1.1",
                "remote_port": 53,
                "pid": 999,
            },
        )

        grouped = process_backend._parse_windows_connections(rows, (100,))

        self.assertEqual(list(grouped), [100])
        established, listening = grouped[100]
        self.assertEqual(established.remote, "93.184.216.34:443")
        self.assertFalse(established.loopback)
        self.assertTrue(listening.listening)
        self.assertIsNone(listening.remote)

    def test_scan_windows_connections_is_cached(self) -> None:
        rows = (
            {
                "state": 5,
                "local_ip": "10.0.0.9",
                "local_port": 5,
                "remote_ip": "127.0.0.1",
                "remote_port": 8765,
                "pid": 100,
            },
        )
        with mock.patch.object(
            process_backend,
            "_windows_tcp_rows",
            return_value=rows,
        ) as tcp_rows:
            process_backend.reset_cache()
            first = process_backend.scan_windows_connections((100,))
            second = process_backend.scan_windows_connections((100,))

        self.assertEqual(first, second)
        tcp_rows.assert_called_once()
        self.assertTrue(first[100][0].loopback)


class ProcessAliveTests(unittest.TestCase):
    """验证存活探测不会在 Windows 上误杀进程。"""

    def test_posix_path_uses_zero_signal(self) -> None:
        self.assertTrue(process_backend.process_alive(1))
        self.assertFalse(process_backend.process_alive(0))
        self.assertFalse(process_backend.process_alive(None))

    @staticmethod
    def _kernel32_returning(code: int) -> mock.Mock:
        """构造一个会写入退出码的 kernel32 假对象。"""

        import ctypes

        kernel32 = mock.Mock()
        kernel32.OpenProcess.return_value = 1234

        def get_exit_code(handle: object, pointer: object) -> bool:
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ulong))[0] = code
            return True

        kernel32.GetExitCodeProcess.side_effect = get_exit_code
        return kernel32

    def test_windows_path_never_calls_os_kill(self) -> None:
        kernel32 = self._kernel32_returning(259)
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.object(process_backend, "_kernel32", return_value=kernel32),
            mock.patch.object(process_backend.os, "kill") as kill,
        ):
            alive = process_backend.process_alive(4242)

        self.assertTrue(alive)
        kill.assert_not_called()

    def test_windows_reports_exited_process(self) -> None:
        kernel32 = self._kernel32_returning(0)
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.object(process_backend, "_kernel32", return_value=kernel32),
        ):
            alive = process_backend.process_alive(4242)

        self.assertFalse(alive)

    def test_windows_denied_access_is_alive(self) -> None:
        kernel32 = mock.Mock()
        kernel32.OpenProcess.return_value = 0
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.object(process_backend, "_kernel32", return_value=kernel32),
            mock.patch.object(process_backend, "_ERROR_ACCESS_DENIED", 0),
        ):
            alive = process_backend.process_alive(4)

        self.assertTrue(alive)


class WindowsAgentModelTests(unittest.TestCase):
    """确认返回结构与其它平台一致（统一 RunningAgent）。"""

    def test_running_agent_shape(self) -> None:
        agent = RunningAgent(
            pid=1,
            product="codex",
            start_token="windows:1",
            cwd=None,
            command=("codex.exe",),
            open_paths=(),
        )
        self.assertTrue(agent.is_process_backed is False if hasattr(agent, "is_process_backed") else True)
        self.assertEqual(agent.product, "codex")
        self.assertIsInstance(process_backend.ObservedProcess(1, 0, (), "", "", None), ObservedProcess)


if __name__ == "__main__":
    unittest.main()
