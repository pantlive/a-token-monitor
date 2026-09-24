"""跨平台进程后端测试：Linux /proc 路径与 macOS ps + lsof 路径。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _platform_support import requires_symlinks
from a_token_monitor import process_backend
from a_token_monitor.agents import scan_running_agents

_PS_OUTPUT = """\
    1     0 Mon Jan  1 00:00:00 2026 /sbin/launchd
  501     1 Mon Jan  1 00:00:05 2026 /Applications/Grok.app/grok --resume abc
  502   501 Mon Jan  1 00:00:06 2026 /opt/homebrew/bin/node /opt/dsh/bin/dsh web
  503     1 Mon Jan  1 00:00:07 2026 /usr/bin/vim notes.md
"""


def _lsof_output(files: dict[int, list[str]], cwds: dict[int, str]) -> str:
    """拼出 lsof -Ffn 风格的输出。"""

    lines: list[str] = []
    for pid in sorted(set(files) | set(cwds)):
        lines.append(f"p{pid}")
        if pid in cwds:
            lines.extend(["fcwd", f"n{cwds[pid]}"])
        for index, path in enumerate(files.get(pid, ()), start=3):
            lines.extend([f"f{index}", f"n{path}"])
    return "\n".join(lines) + "\n"


class BackendSelectionTests(unittest.TestCase):
    """验证后端选择与 netlink 可用性判断。"""

    def test_explicit_proc_root_always_uses_proc_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            self.assertEqual(
                process_backend.select_backend(Path(temporary_directory) / "proc"),
                "proc",
            )

    def test_macos_backend_when_proc_is_missing(self) -> None:
        missing = Path("/nonexistent-proc")
        with (
            mock.patch.object(process_backend, "PROC_ROOT", missing),
            mock.patch.object(sys, "platform", "darwin"),
        ):
            self.assertEqual(process_backend.select_backend(), "macos")
            self.assertEqual(process_backend.backend_name(), "macos")
        with (
            mock.patch.object(process_backend, "PROC_ROOT", missing),
            mock.patch.object(sys, "platform", "linux"),
        ):
            # 其它平台没有 /proc 时保持 proc 语义，结果为空而不是报错
            self.assertEqual(process_backend.select_backend(), "proc")

    def test_netlink_reason_explains_macos(self) -> None:
        with mock.patch.object(process_backend, "socket", object()):
            self.assertFalse(process_backend.netlink_available())
            with mock.patch.object(sys, "platform", "darwin"):
                reason = process_backend.netlink_reason()
            self.assertIsNotNone(reason)
            self.assertIn("netlink", reason or "")
        self.assertIsNone(process_backend.netlink_reason())


class MacosProcessTests(unittest.TestCase):
    """验证 macOS 的 ps + lsof 解析。"""

    def _patch_run(self, lsof: str):
        """把 ps / lsof 子进程换成固定输出并统计调用次数。"""

        calls: list[tuple[str, ...]] = []

        def fake_run(command, timeout):  # noqa: ANN001, ARG001
            calls.append(tuple(command))
            if command[0] == "ps":
                return _PS_OUTPUT
            if "-i" in command:
                return "p501\nf10\nn10.0.0.9:52344->93.184.216.34:443\n"
            return lsof

        return calls, mock.patch.object(process_backend, "_run", side_effect=fake_run)

    def test_scan_processes_parses_ps_tree(self) -> None:
        calls, patcher = self._patch_run("")
        missing = Path("/nonexistent-proc")
        with (
            patcher,
            mock.patch.object(process_backend, "PROC_ROOT", missing),
            mock.patch.object(sys, "platform", "darwin"),
        ):
            process_backend.reset_cache()
            processes = process_backend.scan_processes()

        self.assertEqual(sorted(processes), [1, 501, 502, 503])
        self.assertEqual(processes[502].ppid, 501)
        self.assertEqual(processes[502].command[0], "/opt/homebrew/bin/node")
        self.assertEqual(processes[502].comm, "node")
        self.assertTrue(processes[501].start_token.startswith("macos:"))
        self.assertTrue(any(call[0] == "ps" for call in calls))

    def test_scan_agents_uses_lsof_for_cwd_and_open_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            transcript = root / "chat_history.jsonl"
            transcript.write_text("", encoding="utf-8")
            calls, patcher = self._patch_run(
                _lsof_output({501: [str(transcript)]}, {501: str(root)})
            )
            missing = Path("/nonexistent-proc")
            with (
                patcher,
                mock.patch.object(process_backend, "PROC_ROOT", missing),
                mock.patch.object(sys, "platform", "darwin"),
            ):
                process_backend.reset_cache()
                agents = process_backend.scan_macos_agents()

            found = {agent.pid: agent for agent in agents}

        self.assertIn(501, found)
        self.assertNotIn(503, found)  # vim 不是 agent
        grok = found[501]
        self.assertEqual(grok.product, "grok")
        self.assertEqual(grok.cwd, root)
        self.assertEqual(grok.open_paths, (transcript,))
        self.assertEqual(grok.command[0], "/Applications/Grok.app/grok")
        lsof_calls = [call for call in calls if call[0] == "lsof"]
        self.assertTrue(lsof_calls)
        self.assertIn("-a", lsof_calls[0])

    def test_scan_agents_filters_products_and_ignores_pids(self) -> None:
        _, patcher = self._patch_run("")
        missing = Path("/nonexistent-proc")
        with (
            patcher,
            mock.patch.object(process_backend, "PROC_ROOT", missing),
            mock.patch.object(sys, "platform", "darwin"),
        ):
            process_backend.reset_cache()
            only_dsh = process_backend.scan_macos_agents(products=("dsh",))
            without_grok = process_backend.scan_macos_agents(ignore_pids=(501,))

        self.assertEqual([agent.pid for agent in only_dsh], [502])
        self.assertNotIn(501, [agent.pid for agent in without_grok])

    def test_lsof_results_are_cached_within_ttl(self) -> None:
        calls, patcher = self._patch_run("")
        missing = Path("/nonexistent-proc")
        with (
            patcher,
            mock.patch.object(process_backend, "PROC_ROOT", missing),
            mock.patch.object(sys, "platform", "darwin"),
        ):
            process_backend.reset_cache()
            process_backend.scan_macos_agents()
            first = len(calls)
            process_backend.scan_macos_agents()
            second = len(calls)
            process_backend.reset_cache()
            process_backend.scan_macos_agents()
            third = len(calls)

        self.assertEqual(first, second)  # 命中缓存，不再 fork
        self.assertGreater(third, second)  # 清缓存后重新查询

    def test_parse_lsof_skips_devices_and_sockets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            real = root / "session.jsonl"
            real.write_text("", encoding="utf-8")
            details = process_backend._parse_lsof(
                "\n".join(
                    [
                        "p42",
                        "fcwd",
                        f"n{root}",
                        "f3",
                        f"n{real}",
                        "f4",
                        "n/dev/null",
                        "f5",
                        "nTCP 10.0.0.9:52344->93.184.216.34:443",
                    ]
                )
            )

        self.assertEqual(details[42]["cwd"], root)
        self.assertEqual(details[42]["files"], [real])

    def test_scan_macos_connections_parses_endpoints(self) -> None:
        def fake_run(command, timeout):  # noqa: ANN001, ARG001
            return "\n".join(
                [
                    "p501",
                    "f10",
                    "n10.0.0.9:52344->93.184.216.34:443",
                    "f11",
                    "n127.0.0.1:8080->127.0.0.1:3080",
                    "f12",
                    "n*:3080",
                ]
            )

        with mock.patch.object(process_backend, "_run", side_effect=fake_run):
            process_backend.reset_cache()
            connections = process_backend.scan_macos_connections((501,))

        parsed = connections[501]
        self.assertEqual(parsed[0].remote, "93.184.216.34:443")
        self.assertEqual(parsed[0].local_port, 52344)
        self.assertFalse(parsed[0].loopback)
        self.assertTrue(parsed[1].loopback)
        self.assertTrue(parsed[2].listening)
        self.assertIsNone(parsed[2].remote)


class ProcBackendTests(unittest.TestCase):
    """验证 Linux /proc 后端仍按原语义工作。"""

    def _write_proc(self, proc_root: Path, pid: int, session: Path) -> None:
        directory = proc_root / str(pid)
        (directory / "fd").mkdir(parents=True)
        (directory / "cmdline").write_bytes(b"codex\0exec\0")
        (directory / "comm").write_text("codex\n", encoding="utf-8")
        (directory / "stat").write_text(
            f"{pid} (codex) " + " ".join(["S", "1"] + ["0"] * 17 + ["77"]),
            encoding="utf-8",
        )
        (directory / "cwd").symlink_to(session.parent)
        (directory / "fd" / "3").symlink_to(session)

    def test_agents_scan_dispatches_to_macos_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            transcript = root / "chat_history.jsonl"
            transcript.write_text("", encoding="utf-8")
            calls: list[tuple[str, ...]] = []

            def fake_run(command, timeout):  # noqa: ANN001, ARG001
                calls.append(tuple(command))
                if command[0] == "ps":
                    return _PS_OUTPUT
                return _lsof_output({501: [str(transcript)]}, {501: str(root)})

            with (
                mock.patch.object(process_backend, "_run", side_effect=fake_run),
                mock.patch.object(
                    process_backend,
                    "PROC_ROOT",
                    Path("/nonexistent-proc"),
                ),
                mock.patch.object(sys, "platform", "darwin"),
            ):
                process_backend.reset_cache()
                agents = scan_running_agents(products=("grok",))

        self.assertEqual([agent.pid for agent in agents], [501])
        self.assertEqual(agents[0].open_paths, (transcript,))

    @requires_symlinks
    def test_scan_processes_reads_proc_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session = root / "sessions" / "rollout-1.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text("", encoding="utf-8")
            proc_root = root / "proc"
            self._write_proc(proc_root, 4242, session)

            processes = process_backend.scan_processes(proc_root)
            process_backend.reset_cache()
            agents = scan_running_agents(proc_root=proc_root, products=("codex",))

        self.assertEqual(processes[4242].ppid, 1)
        self.assertEqual(processes[4242].command, ("codex", "exec"))
        self.assertEqual(processes[4242].start_token, "77")
        self.assertEqual(processes[4242].cwd, session.parent)
        self.assertEqual([agent.pid for agent in agents], [4242])
        self.assertEqual(agents[0].open_paths, (session,))

    def test_missing_proc_root_returns_empty(self) -> None:
        missing = Path("/nonexistent-proc")
        process_backend.reset_cache()
        self.assertEqual(process_backend.scan_processes(missing), {})
        self.assertEqual(process_backend.scan_agents(missing), ())


if __name__ == "__main__":
    unittest.main()
