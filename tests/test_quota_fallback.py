"""本地 JSONL 额度兜底测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from token_monitor.discovery import JsonlSessionReader
from token_monitor.quota_fallback import (
    JsonlQuotaFallbackReader,
    read_jsonl_quota,
    recent_session_paths,
)


class QuotaFallbackTests(unittest.TestCase):
    """验证不依赖模型 turn 也能读取最近 token_count 额度。"""

    def test_reads_latest_window_from_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "session.jsonl"
            path.write_text(
                '{"timestamp":"2026-08-26T08:00:00Z","type":"token_count",'
                '"rate_limits":{"primary":{"used_percent":41,'
                '"window_minutes":300,"resets_at":200},'
                '"secondary":{"used_percent":8,"window_minutes":10080,'
                '"resets_at":500},"plan_type":"plus"}}\n'
                '{"timestamp":"2026-08-26T08:01:00Z","type":"token_count",'
                '"rate_limits":{"primary":{"used_percent":42,'
                '"window_minutes":300,"resets_at":220}}}\n',
                encoding="utf-8",
            )

            snapshot = read_jsonl_quota([path], now=1000)

        self.assertEqual(snapshot.source, "session-jsonl-fallback")
        self.assertEqual(snapshot.plan_type, "plus")
        self.assertEqual(snapshot.window("codex", "primary").used_percent, 42)
        self.assertEqual(snapshot.window("codex", "secondary").used_percent, 8)
        self.assertEqual(snapshot.window("codex", "primary").resets_at, 220)

    def test_reuses_unchanged_tail_and_avoids_recursive_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "known.jsonl"
            path.write_text(
                '{"timestamp":100,"type":"token_count",'
                '"rate_limits":{"primary":{"used_percent":55,'
                '"window_minutes":300,"resets_at":200}}}\n',
                encoding="utf-8",
            )
            reader = JsonlSessionReader()
            fallback_reader = JsonlQuotaFallbackReader(reader=reader)

            with patch.object(
                Path,
                "rglob",
                side_effect=AssertionError("不应递归扫描历史目录"),
            ):
                paths = recent_session_paths(root, known_paths=(path,))
            with patch.object(reader, "read", wraps=reader.read) as read_mock:
                first = fallback_reader.read(paths, now=110)
                second = fallback_reader.read(paths, now=120)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(read_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
