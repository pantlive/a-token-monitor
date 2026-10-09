"""``UsageAggregator`` 各 mixin 共享的实例状态声明。

``querying`` / ``sources`` / ``readers`` 三个 mixin 都直接读写 ``UsageAggregator``
在 ``__init__`` 里建立的属性。这里集中声明这些属性的类型（不在运行时赋值），
既让类型检查器能核对 mixin 的用法，也是一份「mixin 之间约定的共享状态」清单：
新增共享属性时先在这里登记。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from ..grok import GrokSessionInfo
from ..kimi import KimiSessionInfo
from .aggregates import _FileRollup
from .records import _CachedFile
from .store import _UsageIndexStore
from .timed_cache import TimedCache


class _AggregatorState:
    """只声明类型的基类；实际赋值都在 ``UsageAggregator.__init__``。"""

    discovery_interval: float
    _lock: threading.RLock
    _snapshot_lock: threading.Lock
    _persistent: _UsageIndexStore | None
    _homes: dict[str, tuple[Path, ...]]
    _cache: dict[Path, _CachedFile]
    _rollups: dict[Path, _FileRollup]
    _chat_partial: dict[Path, tuple[Any, ...]]
    _search_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]]
    _discovered: TimedCache[Path, tuple[Path, ...]]
    _grok_sessions: TimedCache[Path, dict[str, GrokSessionInfo]]
    _kimi_sessions: TimedCache[Path, dict[str, KimiSessionInfo]]
    _claude_sidechain_cache: dict[Path, tuple[float, bool]]
    _facets_cache: dict[str, Any] | None
    _facets_cached_at: float

    def _cached_paths(self, key: Path, loader: Any) -> tuple[Path, ...]:
        """由 ``_FileReaderMixin`` 实现；``_SourceDiscoveryMixin`` 也会调用。"""

        raise NotImplementedError
