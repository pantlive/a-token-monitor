"""从 Codex session JSONL 汇总 token、模型和成本估算。

Codex CLI 的 JSONL 会持续写入累计 ``total_token_usage``。本包只读取
token 用量和模型字段，不保存提示词、工具参数或其他原始事件，适合被
Dashboard 周期性调用。

Plus/OAuth 的实际订阅账单和 Plus credits 扣减规则不会出现在本地 JSONL 中，
因此本包只在模型有官方 API 单价时给出 API 等价美元估算。credits 永远不从
JSONL 猜测，避免把订阅额度伪造成现金或 credits 消耗。

子模块划分（依赖自上而下，无环）：

* ``records``    基础数据类型、会话阈值与持久化编解码
* ``pricing``    官方 API 等价单价与成本估算
* ``periods``    本地日历日与滚动时间窗口
* ``parsing``    各家会话日志的 token 增量解析
* ``aggregates`` 账号 / 模型 / 项目维度的累计结构
* ``search``     SQLite 检索条件、分组与分页
* ``store``      用量增量的 SQLite 持久索引
* ``insights``   使用习惯分析
* ``aggregator`` ``UsageAggregator``：文件发现、有界增量读取与快照

这里重新导出包外（含测试）使用的名字，``from a_token_monitor.usage import X``
保持可用。
"""

from __future__ import annotations

from .aggregates import (
    _UsageAggregate,
    _aggregate_to_dict,
)
from .aggregator import (
    UsageAggregator,
)
from .periods import (
    calendar_day_start,
)
from .pricing import (
    _estimate_usage,
    _lookup_pricing,
    pricing_metadata,
)
from .records import (
    DEFAULT_SESSION_CONTEXT_WARN_TOKENS,
    DEFAULT_SESSION_TURN_WARN,
    SessionSwitchThresholds,
    SessionUsage,
    TokenUsage,
    UsageDelta,
    _UsageSource,
    session_id_from_path,
)
from .search import (
    DEFAULT_SEARCH_DAYS,
    enrich_session_views,
    search_since_days,
)
from .store import (
    _UsageIndexStore,
)

__all__ = [
    "DEFAULT_SEARCH_DAYS",
    "DEFAULT_SESSION_CONTEXT_WARN_TOKENS",
    "DEFAULT_SESSION_TURN_WARN",
    "SessionSwitchThresholds",
    "SessionUsage",
    "TokenUsage",
    "UsageAggregator",
    "UsageDelta",
    "_UsageAggregate",
    "_UsageIndexStore",
    "_UsageSource",
    "_aggregate_to_dict",
    "_estimate_usage",
    "_lookup_pricing",
    "calendar_day_start",
    "enrich_session_views",
    "pricing_metadata",
    "search_since_days",
    "session_id_from_path",
]
