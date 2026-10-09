"""按固定间隔失效的小缓存：目录列举、会话索引这类开销大但变化慢的读取。"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable, Iterator
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class TimedCache(Generic[K, V]):
    """每个键记住加载时间，超过 ``max_age`` 秒后下一次读取重新加载。

    不自带锁：调用方（``UsageAggregator``）已在自己的锁内访问。加载函数抛出的
    异常原样向上传递，不缓存失败结果；需要把失败当空结果缓存的调用方自己捕获。
    """

    def __init__(self) -> None:
        self._values: dict[K, V] = {}
        self._loaded_at: dict[K, float] = {}

    def get(
        self,
        key: K,
        loader: Callable[[], V],
        max_age: float,
        now: float | None = None,
    ) -> V:
        """返回未过期的缓存值，否则调用 ``loader`` 加载并记录加载时间。"""

        current = time.time() if now is None else now
        if key in self._values and current - self._loaded_at[key] < max_age:
            return self._values[key]
        value = loader()
        self.put(key, value, current)
        return value

    def put(self, key: K, value: V, loaded_at: float) -> None:
        """直接写入一个值及其加载时间。"""

        self._values[key] = value
        self._loaded_at[key] = loaded_at

    def retain(self, keep: Callable[[K], bool]) -> None:
        """只保留 ``keep(key)`` 为真的条目，例如扫描目录被移除后清掉旧目录的缓存。"""

        for key in [key for key in self._values if not keep(key)]:
            del self._values[key]
            del self._loaded_at[key]

    def __contains__(self, key: object) -> bool:
        return key in self._values

    def __iter__(self) -> Iterator[K]:
        return iter(tuple(self._values))

    def __len__(self) -> int:
        return len(self._values)
