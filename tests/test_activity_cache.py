"""活动会话索引缓存的行为测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from a_token_monitor.activity_cache import ActivityIndexCache


class ActivityIndexCacheTests(unittest.TestCase):
    def test_reuses_index_until_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "sessions"
            root.mkdir()
            cache: ActivityIndexCache[dict[str, str]] = ActivityIndexCache(30)
            calls: list[int] = []

            def loader() -> dict[str, str]:
                calls.append(1)
                return {"call": str(len(calls))}

            first = cache.get(root, loader)
            second = cache.get(root, loader)
            self.assertEqual(first, second)
            self.assertEqual(calls, [1])

            cache.clear()
            third = cache.get(root, loader)
            self.assertEqual(third, {"call": "2"})
            self.assertEqual(calls, [1, 1])

    def test_directory_identity_change_invalidates_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "sessions"
            root.mkdir()
            cache: ActivityIndexCache[dict[str, str]] = ActivityIndexCache(30)
            calls: list[int] = []

            def loader() -> dict[str, str]:
                calls.append(1)
                return {"call": str(len(calls))}

            cache.get(root, loader)
            # 会话根目录身份变化后会重新扫描，避免目录被替换后命中旧索引。
            # Windows 创建子目录不一定更新父目录 mtime（3.10 上因此命中旧索引），
            # 这里把 mtime 往前拨，锁定的是缓存行为而不是文件系统副作用。
            (root / "new-session").mkdir()
            stamped = root.stat()
            os.utime(
                root,
                ns=(stamped.st_atime_ns, stamped.st_mtime_ns + 1_000_000_000),
            )
            cache.get(root, loader)
            self.assertEqual(calls, [1, 1])

    def test_missing_directory_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "missing"
            cache: ActivityIndexCache[dict[str, str]] = ActivityIndexCache(30)
            calls: list[int] = []

            def loader() -> dict[str, str]:
                calls.append(1)
                return {}

            cache.get(root, loader)
            cache.get(root, loader)
            self.assertEqual(calls, [1, 1])

    def test_expires_after_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "sessions"
            root.mkdir()
            cache: ActivityIndexCache[dict[str, str]] = ActivityIndexCache(30)
            calls: list[int] = []

            def loader() -> dict[str, str]:
                calls.append(1)
                return {}

            with mock.patch(
                "a_token_monitor.activity_cache.time.monotonic",
                side_effect=[100.0, 131.0, 131.0],
            ):
                cache.get(root, loader)
                cache.get(root, loader)
            self.assertEqual(calls, [1, 1])


if __name__ == "__main__":
    unittest.main()
