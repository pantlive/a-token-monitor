"""/proc 进程发现和 JSONL 增量读取测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _platform_support import requires_symlinks
from a_token_monitor.discovery import JsonlSessionReader, ProcessScanner


class DiscoveryTests(unittest.TestCase):
    """验证只识别打开的 session JSONL，并正确保留不完整尾行。"""

    @requires_symlinks
    def test_scans_process_fd_and_reads_session_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_root = root / "sessions"
            session_root.mkdir()
            jsonl_file = session_root / "rollout-test.jsonl"
            jsonl_file.write_text(
                '{"type":"session_meta","payload":{'
                '"session_id":"session-1","id":"thread-1",'
                '"cwd":"/workspace","source":"cli"}}\n'
                '{"type":"event_msg","payload":{"type":"task_started"}}\n',
                encoding="utf-8",
            )
            proc_root = root / "proc"
            process_root = proc_root / "123"
            (process_root / "fd").mkdir(parents=True)
            (process_root / "cwd").symlink_to(root)
            (process_root / "fd" / "3").symlink_to(jsonl_file)
            (process_root / "cmdline").write_bytes(b"codex\0exec\0--json\0")
            stat_fields = ["S"] + ["0"] * 18 + ["987654"]
            (process_root / "stat").write_text(
                "123 (codex) " + " ".join(stat_fields),
                encoding="utf-8",
            )

            processes = ProcessScanner(
                session_root=session_root,
                proc_root=proc_root,
            ).scan()
            metadata = JsonlSessionReader().read_metadata(jsonl_file)

        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].pid, 123)
        self.assertEqual(processes[0].start_token, "987654")
        self.assertEqual(processes[0].open_jsonl_paths, (jsonl_file.resolve(),))
        self.assertEqual(metadata.thread_id, "thread-1")
        self.assertEqual(metadata.session_id, "session-1")

    def test_does_not_consume_partial_last_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            jsonl_file = Path(temporary_directory) / "session.jsonl"
            jsonl_file.write_text(
                '{"type":"thread.started","thread_id":"session-2"}\n'
                '{"type":"turn.started"}',
                encoding="utf-8",
            )
            reader = JsonlSessionReader()
            first = reader.read(jsonl_file)
            with jsonl_file.open("a", encoding="utf-8") as handle:
                handle.write("\n")
            second = reader.read(jsonl_file, offset=first.next_offset)

        self.assertEqual(
            first.next_offset,
            len('{"type":"thread.started","thread_id":"session-2"}\n'.encode()),
        )
        self.assertEqual(len(first.events), 1)
        self.assertEqual(len(second.events), 1)
        self.assertEqual(second.events[0].event_type, "turn.started")

    def test_metadata_read_has_a_total_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            jsonl_file = Path(temporary_directory) / "session.jsonl"
            # 中文注释：元数据在 256 KiB 之后时，不能因为“最多 32 行”而继续读冷数据。
            prefix = (b"x" * 10_000 + b"\n") * 31
            metadata = (
                b'{"type":"session_meta","payload":{"session_id":"late"}}\n'
            )
            jsonl_file.write_bytes(prefix + metadata)

            result = JsonlSessionReader().read_metadata(jsonl_file)

        self.assertIsNone(result)

    def test_discards_initial_mid_line_without_false_quota_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            jsonl_file = Path(temporary_directory) / "session.jsonl"
            first_line = (
                '{"type":"event_msg","payload":{"type":"agent_message",'
                '"text":"rate limit is ordinary discussion text"}}\n'
            )
            jsonl_file.write_text(
                first_line + '{"type":"task_started"}\n',
                encoding="utf-8",
            )
            reader = JsonlSessionReader()
            tail = reader.read(jsonl_file, offset=7)

        self.assertEqual(len(tail.quota_events), 0)
        self.assertEqual(tail.events[-1].event_type, "task_started")

    def test_ignores_approval_words_inside_message_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            jsonl_file = Path(temporary_directory) / "session.jsonl"
            jsonl_file.write_text(
                '{"type":"response_item","payload":{"type":"agent_message",'
                '"text":"approval_request is a concept in the instructions"}}\n',
                encoding="utf-8",
            )

            tail = JsonlSessionReader().read(jsonl_file)

        self.assertFalse(tail.approval_waiting)


if __name__ == "__main__":
    unittest.main()
