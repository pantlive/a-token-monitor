"""活动会话探测用的短 TTL 索引缓存。

Kimi / Grok 等 provider 在每次 Dashboard 刷新时都会重建「会话 ID → 元数据」
索引，即使进程表里没有活动会话。这里按会话根目录的目录身份（inode / mtime）
和 TTL 缓存索引：同一刷新周期内只扫描一次磁盘；目录被删除后在相同路径重建
时，身份变化会让旧索引自然失效。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Generic, TypeVar

T = TypeVar("T")

# 中文注释：Dashboard 每 5 秒刷新一次，30 秒足够跨多次刷新，又不会让新会话
# 长时间不可见。
DEFAULT_ACTIVITY_INDEX_TTL_SECONDS = 30.0


class ActivityIndexCache(Generic[T]):
    """线程安全地缓存某个会话根目录的索引对象。"""

    def __init__(self, ttl_seconds: float = DEFAULT_ACTIVITY_INDEX_TTL_SECONDS) -> None:
        self._ttl_seconds = max(0.0, float(ttl_seconds))
        self._lock = threading.Lock()
        # 中文注释：键是会话根目录，值是 (目录身份, 过期时间, 索引)。
        self._entries: dict[str, tuple[tuple[int, int, int], float, T]] = {}

    def get(self, sessions_root: Path, loader: Callable[[], T]) -> T:
        """返回缓存索引；目录不存在时直接加载且不缓存（避免新建目录后命中空索引）。"""

        try:
            stat_result = sessions_root.stat()
        except OSError:
            return loader()
        identity = (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_mtime_ns,
        )
        key = str(sessions_root)
        with self._lock:
            cached = self._entries.get(key)
            if (
                cached is not None
                and cached[0] == identity
                and time.monotonic() < cached[1]
            ):
                return cached[2]
        value = loader()
        with self._lock:
            # 中文注释：TTL 从加载完成时开始算，慢目录不会刚写入就过期。
            self._entries[key] = (identity, time.monotonic() + self._ttl_seconds, value)
        return value

    def clear(self) -> None:
        """清空缓存，供测试或目录热更新后显式重置。"""

        with self._lock:
            self._entries.clear()
