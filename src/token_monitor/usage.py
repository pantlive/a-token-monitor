"""从 Codex session JSONL 汇总 token、模型和成本估算。

Codex CLI 的 JSONL 会持续写入累计 ``total_token_usage``。本模块只读取
token 用量和模型字段，不保存提示词、工具参数或其他原始事件，适合被
Dashboard 周期性调用。

Plus/OAuth 的实际订阅账单和 Plus credits 扣减规则不会出现在本地 JSONL 中，
因此本模块只在模型有官方 API 单价时给出 API 等价美元估算。credits 永远不从
JSONL 猜测，避免把订阅额度伪造成现金或 credits 消耗。
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .grok import (
    GrokSessionInfo,
    grok_unified_log,
    load_session_index,
    parse_grok_log_chunk,
    read_grok_account,
    resolve_grok_homes,
)
from .dsh import (
    dsh_projcache_home,
    list_dsh_projcache_files,
    parse_dsh_projcache,
    read_dsh_account,
    resolve_dsh_homes,
)
from .claude import (
    claude_home_for,
    list_claude_transcripts,
    parse_claude_chunk,
    read_claude_account,
    resolve_claude_homes,
    resolve_sidechain_policy,
)
from .health import sanitize_error
from .kimi import (
    KimiSessionInfo,
    kimi_wire_session_id,
    load_kimi_session_index,
    parse_kimi_wire_chunk,
    read_kimi_account,
    resolve_kimi_homes,
)
from .registry import MultiSessionRegistry

_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
_UNKNOWN_MODEL = "未知模型"
_UNKNOWN_PROJECT = "未知项目"
_LONG_CONTEXT_INPUT_THRESHOLD = 272_000
_CACHE_WRITE_MULTIPLIER = 1.25
_API_PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
_GROK_API_PRICING_SOURCE = "https://docs.x.ai/docs/models"
_KIMI_API_PRICING_SOURCE = "https://platform.kimi.com/docs/pricing/chat"
_DSH_API_PRICING_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing"
_CLAUDE_API_PRICING_SOURCE = "https://docs.claude.com/en/docs/about-claude/pricing"
# 中文注释：解析规则变化时必须升版本，避免沿用错误的历史增量。
_USAGE_INDEX_VERSION = 6
_USAGE_LINE_HINTS = (
    b"total_token_usage",
    b"last_token_usage",
    b"totalTokenUsage",
    b"lastTokenUsage",
)
_MODEL_LINE_HINTS = (
    b"turn_context",
    b"world_state",
    b"session_meta",
    b"thread_settings",
)
_MAX_MODEL_LINE_BYTES = 64 * 1024
_CODEX_SESSION_ID_PATTERN = re.compile(
    r"(?P<session_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)
# 中文注释：用量检索分组和分页边界。单次检索最多扫描的原始记录数用于防止
# 「全部时间」条件下把整份索引读进内存，命中上限时明确返回 truncated。
_USAGE_SEARCH_GROUPS = ("session", "date", "model")
_USAGE_SEARCH_SORTS = ("recent", "tokens", "cost")
_DEFAULT_SEARCH_LIMIT = 50
_MAX_SEARCH_LIMIT = 500
_MAX_SEARCH_ROWS = 200_000
_MAX_SEARCH_KEYWORD_BYTES = 200
_FACETS_CACHE_SECONDS = 60.0
# 中文注释：相同筛选条件的检索结果缓存 30 秒，避免来回切换筛选时重复聚合。
_SEARCH_CACHE_SECONDS = 30.0
_SEARCH_CACHE_MAX = 24
# 中文注释：命令行和 Dashboard 默认只检索最近 30 天，0 表示不限时间。
DEFAULT_SEARCH_DAYS = 30
# 中文注释：会话过长提醒的默认阈值，与习惯分析的「超长对话」口径保持一致。
DEFAULT_SESSION_TURN_WARN = 100
DEFAULT_SESSION_CONTEXT_WARN_TOKENS = 200_000


@dataclass(frozen=True)
class ModelPricing:
    """一个模型的 credits 和 API 等价单价，单位均为每百万 token。"""

    input_credits: float | None = None
    cached_input_credits: float | None = None
    output_credits: float | None = None
    input_usd: float | None = None
    cached_input_usd: float | None = None
    output_usd: float | None = None
    long_context_threshold: int | None = None
    long_context_inclusive: bool = False
    long_input_multiplier: float = 2.0
    long_cached_multiplier: float = 2.0
    long_output_multiplier: float = 1.5

    @property
    def has_credits(self) -> bool:
        """判断是否有完整的 Codex credits 单价。"""

        return all(
            value is not None
            for value in (
                self.input_credits,
                self.cached_input_credits,
                self.output_credits,
            )
        )

    @property
    def has_api_price(self) -> bool:
        """判断是否有完整的 API 等价美元单价。"""

        return all(
            value is not None
            for value in (
                self.input_usd,
                self.cached_input_usd,
                self.output_usd,
            )
        )


# 这些是官方 API 等价单价；Plus/OAuth 的本地 JSONL 没有可反推的 credits 单价。
# 只收录当前官方模型页有依据的型号；其他历史或第三方型号按未定价处理。
_MODEL_PRICING: dict[str, ModelPricing] = {
    "gpt-6-astra": ModelPricing(
        input_usd=10,
        cached_input_usd=1,
        output_usd=50,
        long_context_threshold=272_000,
    ),
    "gpt-5.6-sol": ModelPricing(
        input_usd=4,
        cached_input_usd=0.4,
        output_usd=20,
    ),
    "gpt-5.6-terra": ModelPricing(
        input_usd=2,
        cached_input_usd=0.2,
        output_usd=12,
    ),
    "gpt-5.6-luna": ModelPricing(
        input_usd=0.2,
        cached_input_usd=0.02,
        output_usd=1.2,
    ),
    "grok-4.6": ModelPricing(
        input_usd=2,
        cached_input_usd=0.5,
        output_usd=6,
        long_context_threshold=200_000,
        long_context_inclusive=True,
        long_input_multiplier=2.0,
        long_cached_multiplier=2.0,
        long_output_multiplier=2.0,
    ),
    "grok-4.5": ModelPricing(
        input_usd=2,
        cached_input_usd=0.3,
        output_usd=6,
        long_context_threshold=200_000,
        long_context_inclusive=True,
        long_input_multiplier=2.0,
        long_cached_multiplier=2.0,
        long_output_multiplier=2.0,
    ),
    "grok-4.3": ModelPricing(
        input_usd=1.25,
        cached_input_usd=0.2,
        output_usd=2.5,
        long_context_threshold=200_000,
        long_context_inclusive=True,
        long_input_multiplier=2.0,
        long_cached_multiplier=2.0,
        long_output_multiplier=2.0,
    ),
    "grok-4.20": ModelPricing(
        input_usd=1.25,
        cached_input_usd=0.2,
        output_usd=2.5,
        long_context_threshold=200_000,
        long_context_inclusive=True,
        long_input_multiplier=2.0,
        long_cached_multiplier=2.0,
        long_output_multiplier=2.0,
    ),
    "grok-build-0.1": ModelPricing(
        input_usd=1,
        cached_input_usd=0.2,
        output_usd=2,
        long_context_threshold=200_000,
        long_context_inclusive=True,
        long_input_multiplier=2.0,
        long_cached_multiplier=2.0,
        long_output_multiplier=2.0,
    ),
    # 中文注释：Kimi Code 是订阅制，本地没有账单；这里用 Kimi 开放平台公开的
    # K3 API 单价做等价参考。K3 官方说明不按上下文长度分段计费，长上下文
    # 倍率固定为 1。K2.x 的 kimi-for-coding* 没有可靠公开单价，按未定价处理。
    "kimi-code/k3": ModelPricing(
        input_usd=3,
        cached_input_usd=0.3,
        output_usd=15,
        long_input_multiplier=1.0,
        long_cached_multiplier=1.0,
        long_output_multiplier=1.0,
    ),
    "kimi-code/k3-256k": ModelPricing(
        input_usd=3,
        cached_input_usd=0.3,
        output_usd=15,
        long_input_multiplier=1.0,
        long_cached_multiplier=1.0,
        long_output_multiplier=1.0,
    ),
    # 中文注释：DeepSeek 官方有峰时/闲时价。这里用峰时单价做保守的 API
    # 等价估算，不代表 Command Code 等转发网关的实际账单。
    "deepseek-v4.1-flash": ModelPricing(
        input_usd=0.3,
        cached_input_usd=0.006,
        output_usd=1.2,
        long_input_multiplier=1.0,
        long_cached_multiplier=1.0,
        long_output_multiplier=1.0,
    ),
    "deepseek-v4-flash": ModelPricing(
        input_usd=0.3,
        cached_input_usd=0.006,
        output_usd=1.2,
        long_input_multiplier=1.0,
        long_cached_multiplier=1.0,
        long_output_multiplier=1.0,
    ),
    "deepseek-flash": ModelPricing(
        input_usd=0.3,
        cached_input_usd=0.006,
        output_usd=1.2,
        long_input_multiplier=1.0,
        long_cached_multiplier=1.0,
        long_output_multiplier=1.0,
    ),
    "deepseek-v4-pro": ModelPricing(
        input_usd=1.32,
        cached_input_usd=0.044,
        output_usd=3.96,
        long_input_multiplier=1.0,
        long_cached_multiplier=1.0,
        long_output_multiplier=1.0,
    ),
    # 中文注释：Anthropic 公开单价（美元 / 百万 token）。缓存读为输入价的
    # 0.1×，缓存写由 _estimate_amount 统一按 1.25× 输入价计算（5 分钟缓存；
    # 1 小时缓存官方为 2×，本工具不做区分）。
    "claude-opus-4-5": ModelPricing(
        input_usd=5,
        cached_input_usd=0.5,
        output_usd=25,
    ),
    "claude-opus-4-1": ModelPricing(
        input_usd=15,
        cached_input_usd=1.5,
        output_usd=75,
    ),
    "claude-opus-4": ModelPricing(
        input_usd=15,
        cached_input_usd=1.5,
        output_usd=75,
    ),
    "claude-sonnet-4-5": ModelPricing(
        input_usd=3,
        cached_input_usd=0.3,
        output_usd=15,
    ),
    "claude-sonnet-4": ModelPricing(
        input_usd=3,
        cached_input_usd=0.3,
        output_usd=15,
    ),
    "claude-haiku-4-5": ModelPricing(
        input_usd=1,
        cached_input_usd=0.1,
        output_usd=5,
    ),
    "claude-3-7-sonnet": ModelPricing(
        input_usd=3,
        cached_input_usd=0.3,
        output_usd=15,
    ),
    "claude-3-5-sonnet": ModelPricing(
        input_usd=3,
        cached_input_usd=0.3,
        output_usd=15,
    ),
    "claude-3-5-haiku": ModelPricing(
        input_usd=0.8,
        cached_input_usd=0.08,
        output_usd=4,
    ),
}


@dataclass(frozen=True)
class TokenUsage:
    """一次累计 token 快照或一次增量 token 用量。"""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TokenUsage | None":
        """从 Codex 的 snake_case 或 camelCase 字段读取 token 数。"""

        parsed: dict[str, int] = {}
        for field_name in _TOKEN_FIELDS:
            amount = _token_number(
                value.get(field_name, value.get(_camel_case(field_name)))
            )
            if amount is not None:
                parsed[field_name] = amount
        if not parsed:
            return None
        total_tokens = parsed.get("total_tokens")
        if total_tokens is None:
            total_tokens = sum(
                parsed.get(field_name, 0)
                for field_name in (
                    "input_tokens",
                    "output_tokens",
                )
            )
        return cls(
            input_tokens=parsed.get("input_tokens", 0),
            cached_input_tokens=parsed.get("cached_input_tokens", 0),
            cache_write_input_tokens=parsed.get(
                "cache_write_input_tokens",
                0,
            ),
            output_tokens=parsed.get("output_tokens", 0),
            reasoning_output_tokens=parsed.get(
                "reasoning_output_tokens",
                0,
            ),
            total_tokens=total_tokens,
        )

    def add(self, other: "TokenUsage") -> "TokenUsage":
        """返回两个 token 用量的逐字段和。"""

        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=(self.cached_input_tokens + other.cached_input_tokens),
            cache_write_input_tokens=(
                self.cache_write_input_tokens + other.cache_write_input_tokens
            ),
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_output_tokens=(
                self.reasoning_output_tokens + other.reasoning_output_tokens
            ),
            total_tokens=self.total_tokens + other.total_tokens,
        )

    def subtract(self, other: "TokenUsage") -> "TokenUsage":
        """返回两个累计快照的非负逐字段差值。"""

        return TokenUsage(
            input_tokens=max(0, self.input_tokens - other.input_tokens),
            cached_input_tokens=max(
                0,
                self.cached_input_tokens - other.cached_input_tokens,
            ),
            cache_write_input_tokens=max(
                0,
                self.cache_write_input_tokens - other.cache_write_input_tokens,
            ),
            output_tokens=max(0, self.output_tokens - other.output_tokens),
            reasoning_output_tokens=max(
                0,
                self.reasoning_output_tokens - other.reasoning_output_tokens,
            ),
            total_tokens=max(0, self.total_tokens - other.total_tokens),
        )

    def decreased_from(self, other: "TokenUsage") -> bool:
        """判断总累计计数是否发生重置或文件内容回退。"""

        # 个别版本可能省略某个明细字段；只用总量判断重置，避免把缺失的
        # 明细当成从较大值回退而重复计算整段用量。
        return self.total_tokens < other.total_tokens

    def as_values(self) -> tuple[int, ...]:
        """返回稳定顺序的字段值，供比较和测试使用。"""

        return tuple(getattr(self, field_name) for field_name in _TOKEN_FIELDS)

    def is_zero(self) -> bool:
        """判断所有 token 字段是否为零。"""

        return not any(self.as_values())

    def to_dict(self) -> dict[str, int]:
        """转换为 Dashboard 安全输出。"""

        return {
            field_name: int(getattr(self, field_name)) for field_name in _TOKEN_FIELDS
        }


@dataclass(frozen=True)
class UsageDelta:
    """JSONL 中一次累计快照产生的 token 增量。"""

    timestamp: float
    model: str
    usage: TokenUsage
    billing_usage: TokenUsage | None = None
    project: str | None = None


@dataclass(frozen=True)
class _UsageSource:
    """一个 JSONL 文件所属的 profile 和账号身份。"""

    profile_name: str
    account_id: str | None
    codex_home: str | None
    project: str | None = None

    @property
    def account_key(self) -> str:
        """返回按真实账号优先的归组键。"""

        return self.account_id or f"profile:{self.profile_name}"

    @property
    def account_name(self) -> str:
        """返回 Dashboard 中的账号显示名。"""

        return self.account_id or self.profile_name


@dataclass(frozen=True)
class _UsageParseState:
    """追加解析需要保留的累计 token、模型和时间状态。"""

    total_baseline: TokenUsage | None = None
    last_baseline: TokenUsage | None = None
    has_total_usage: bool = False
    current_model: str = _UNKNOWN_MODEL
    project: str | None = None
    previous_timestamp: float = 0.0
    discarding_oversized_line: bool = False
    # 中文注释：Claude Code 会重复写入同一个 message.id，这里记住最近见过的 ID。
    recent_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class _UsageParseResult:
    """一次完整或追加读取产生的安全解析结果。"""

    next_offset: int
    total_deltas: tuple[UsageDelta, ...]
    fallback_deltas: tuple[UsageDelta, ...]
    state: _UsageParseState
    bytes_read: int
    reached_eof: bool


@dataclass(frozen=True)
class _CachedFile:
    """一个 JSONL 文件的签名、偏移量和累计解析状态。"""

    signature: tuple[int, int, int]
    next_offset: int
    total_deltas: tuple[UsageDelta, ...]
    fallback_deltas: tuple[UsageDelta, ...]
    state: _UsageParseState
    last_read_bytes: int = 0
    complete: bool = True

    @property
    def deltas(self) -> tuple[UsageDelta, ...]:
        """优先返回 Codex 的累计 total usage 增量。"""

        return self.total_deltas if self.state.has_total_usage else self.fallback_deltas

    @property
    def project(self) -> str | None:
        """返回从 session 元数据中发现的工作目录。"""

        return self.state.project


@dataclass(frozen=True)
class SessionSwitchThresholds:
    """提醒切换新会话的阈值：会话轮数与最近一次请求的上下文规模。"""

    turn_warn: int = DEFAULT_SESSION_TURN_WARN
    context_warn_tokens: int = DEFAULT_SESSION_CONTEXT_WARN_TOKENS

    def __post_init__(self) -> None:
        """拒绝无意义的阈值。"""

        if self.turn_warn <= 0:
            raise ValueError("turn_warn 必须大于 0")
        if self.context_warn_tokens <= 0:
            raise ValueError("context_warn_tokens 必须大于 0")

    def to_dict(self) -> dict[str, int]:
        """返回 Dashboard / CLI 可展示的阈值。"""

        return {
            "turn_warn": self.turn_warn,
            "context_warn_tokens": self.context_warn_tokens,
        }


@dataclass(frozen=True)
class SessionUsage:
    """一个会话（JSONL 文件）的 token 汇总，用于长会话提醒。"""

    path: str
    turns: int
    total_tokens: int
    context_tokens: int
    model: str | None
    first_at: float | None
    last_at: float | None
    estimated_cost_usd: float | None
    api_pricing_known: bool = True

    @property
    def session_id(self) -> str:
        """返回会话展示名：Codex session UUID 或文件名。"""

        return session_id_from_path(Path(self.path))

    def to_dict(self) -> dict[str, Any]:
        """转换为 Dashboard / CLI 展示字段。"""

        return {
            "path": self.path,
            "session_id": self.session_id,
            "turns": self.turns,
            "total_tokens": self.total_tokens,
            "context_tokens": self.context_tokens,
            "model": self.model,
            "first_at": self.first_at,
            "last_at": self.last_at,
            "estimated_cost_usd": self.estimated_cost_usd,
            "api_pricing_known": self.api_pricing_known,
        }

    def reminder(
        self,
        thresholds: SessionSwitchThresholds,
    ) -> dict[str, Any] | None:
        """轮数或上下文超过阈值时返回切换新会话的提醒。"""

        reasons: list[str] = []
        if self.turns >= thresholds.turn_warn:
            reasons.append(f"已进行 {self.turns} 轮")
        if self.context_tokens >= thresholds.context_warn_tokens:
            reasons.append(f"最近一次上下文 {self.context_tokens:,} token")
        if not reasons:
            return None
        label = self.session_id
        model = self.model or "未知模型"
        return {
            "level": "warn",
            "kind": "session",
            "title": f"会话 {label} 建议切换新会话",
            "detail": (
                f"{model} · {'；'.join(reasons)}。超长会话每一轮都按全量上下文"
                "重新计费；任务做到阶段收尾后让模型总结要点，再开新会话更省 token。"
            ),
            "message": (
                f"会话 {label}（{model}）{'；'.join(reasons)}，建议收尾并开启新会话"
            ),
            "session_id": label,
            "path": self.path,
            "turns": self.turns,
            "context_tokens": self.context_tokens,
            "total_tokens": self.total_tokens,
            "model": self.model,
            "reasons": reasons,
        }


@dataclass(frozen=True)
class _IndexRow:
    """用量索引中的一条 token 计量记录。"""

    path: str
    kind: str
    timestamp: float
    model: str
    usage_json: str
    billing_usage_json: str | None
    project: str | None

    def delta(self) -> UsageDelta | None:
        """把索引行还原成 token 增量；损坏记录返回 None。"""

        try:
            return _delta_from_row(
                (
                    self.kind,
                    self.timestamp,
                    self.model,
                    self.usage_json,
                    self.billing_usage_json,
                    self.project,
                )
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return None


class _UsageIndexStore:
    """把已解析的偏移和 token 增量保存到轻量 SQLite 索引。"""

    def __init__(self, path: Path) -> None:
        """初始化索引文件和表结构。"""

        self.path = path.expanduser()
        self._json1: bool | None = None
        self._pending_checked = False
        # 中文注释：只在状态目录写入索引，不复制或压缩原始 JSONL。
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        # 中文注释：索引含有项目路径和统计信息，只允许当前用户读取。
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        """打开一次短连接，避免 Dashboard 长期占用数据库句柄。"""

        connection = sqlite3.connect(self.path, timeout=1.0)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=1000")
        return connection

    def _initialize(self) -> None:
        """创建索引所需的最小表和查询索引。"""

        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS usage_file_state (
                        path TEXT PRIMARY KEY,
                        inode INTEGER NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        file_size INTEGER NOT NULL,
                        next_offset INTEGER NOT NULL,
                        complete INTEGER NOT NULL,
                        state_json TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS usage_delta (
                        path TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        model TEXT NOT NULL,
                        usage_json TEXT NOT NULL,
                        billing_usage_json TEXT,
                        project TEXT,
                        FOREIGN KEY(path) REFERENCES usage_file_state(path)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS usage_delta_path_index
                    ON usage_delta(path)
                    """
                )
                # 中文注释：用量检索按时间、模型和会话过滤，补上对应索引，
                # 避免历史记录变多后每次检索都全表扫描。
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS usage_delta_timestamp_index
                    ON usage_delta(timestamp)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS usage_delta_model_index
                    ON usage_delta(model, timestamp)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS usage_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                version_row = connection.execute(
                    "SELECT value FROM usage_meta WHERE key = 'parser_version'"
                ).fetchone()
                if version_row is None or version_row[0] != str(_USAGE_INDEX_VERSION):
                    connection.execute("DELETE FROM usage_delta")
                    connection.execute("DELETE FROM usage_file_state")
                    connection.execute(
                        """
                        INSERT INTO usage_meta(key, value)
                        VALUES ('parser_version', ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (str(_USAGE_INDEX_VERSION),),
                    )
                delta_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(usage_delta)")
                }
                if "project" not in delta_columns:
                    connection.execute(
                        "ALTER TABLE usage_delta ADD COLUMN project TEXT"
                    )
                if "long_context" not in delta_columns:
                    connection.execute(
                        "ALTER TABLE usage_delta ADD COLUMN long_context INTEGER"
                    )
                # 中文注释：旧索引按行回填一次，避免为了聚合重读所有 JSONL；
                # 回填中断后再次打开会继续补齐。
                if _has_pending_long_context(connection):
                    _backfill_long_context(connection)
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS usage_delta_kind_path_index
                    ON usage_delta(kind, path)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS usage_delta_pending_index
                    ON usage_delta(long_context) WHERE long_context IS NULL
                    """
                )
        except (OSError, sqlite3.DatabaseError):
            # 中文注释：索引损坏时不影响 Dashboard 继续使用内存增量解析。
            raise

    def count_deltas(self) -> int:
        """返回用量增量明细的总行数，供历史数据预览使用。"""

        with closing(self._connect()) as connection:
            row = connection.execute("SELECT COUNT(*) FROM usage_delta").fetchone()
        return int(row[0]) if row is not None else 0

    def count_deltas_before(self, cutoff: float) -> int:
        """返回时间早于 ``cutoff`` 的用量增量行数。"""

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM usage_delta WHERE timestamp < ?",
                (float(cutoff),),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def delete_deltas_before(self, cutoff: float) -> int:
        """删除时间早于 ``cutoff`` 的用量增量，返回删除行数。

        中文注释：只删 ``usage_delta`` 明细，绝不动 ``usage_file_state`` 里的
        增量读取检查点，否则已解析文件会被当作新文件全量重读。
        """

        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM usage_delta WHERE timestamp < ?",
                (float(cutoff),),
            )
        return int(cursor.rowcount or 0)

    def vacuum(self) -> None:
        """删除历史行后压缩索引文件;数据库繁忙时把错误抛给上层处理。"""

        with closing(self._connect()) as connection:
            connection.execute("VACUUM")

    def close(self) -> None:
        """本类只使用短连接，没有需要释放的持久资源;为调用方统一接口保留。"""

    def load(self, path: Path) -> _CachedFile | None:
        """读取一个文件的持久化解析状态；损坏记录按未缓存处理。"""

        try:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    """
                    SELECT inode, mtime_ns, file_size, next_offset, complete,
                           state_json
                    FROM usage_file_state
                    WHERE path = ?
                    """,
                    (str(path),),
                ).fetchone()
                if row is None:
                    return None
                state = _state_from_json(row[5])
                if state is None:
                    return None
                total_deltas: list[UsageDelta] = []
                fallback_deltas: list[UsageDelta] = []
                delta_rows = connection.execute(
                    """
                    SELECT kind, timestamp, model, usage_json,
                           billing_usage_json, project
                    FROM usage_delta
                    WHERE path = ?
                    ORDER BY rowid
                    """,
                    (str(path),),
                )
                for delta_row in delta_rows:
                    delta = _delta_from_row(delta_row)
                    if delta is None:
                        return None
                    if delta_row[0] == "total":
                        total_deltas.append(delta)
                    elif delta_row[0] == "fallback":
                        fallback_deltas.append(delta)
                    else:
                        return None
                return _CachedFile(
                    signature=(int(row[0]), int(row[1]), int(row[2])),
                    next_offset=int(row[3]),
                    total_deltas=tuple(total_deltas),
                    fallback_deltas=tuple(fallback_deltas),
                    state=state,
                    complete=bool(row[4]),
                )
        except (
            OSError,
            OverflowError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            sqlite3.DatabaseError,
        ):
            return None

    def search_deltas(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        models: Sequence[str] = (),
        session: str | None = None,
        project: str | None = None,
        keyword: str | None = None,
        limit: int | None = None,
    ) -> tuple[_IndexRow, ...]:
        """按时间、模型和会话关键词检索索引里的 token 记录。"""

        clauses: list[str] = []
        parameters: list[Any] = []
        if since is not None:
            clauses.append("timestamp >= ?")
            parameters.append(float(since))
        if until is not None:
            clauses.append("timestamp <= ?")
            parameters.append(float(until))
        selected_models = [item for item in models if item]
        if selected_models:
            placeholders = ", ".join("?" for _ in selected_models)
            clauses.append(f"model IN ({placeholders})")
            parameters.extend(selected_models)
        session_pattern = _search_pattern(session)
        if session_pattern is not None:
            clauses.append("path LIKE ? ESCAPE '\\'")
            parameters.append(session_pattern)
        try:
            with closing(self._connect()) as connection:
                file_projects = _file_projects(connection)
                project_pattern = _search_pattern(project)
                if project_pattern is not None:
                    clause, clause_parameters = _project_clause(
                        project_pattern,
                        file_projects,
                    )
                    clauses.append(clause)
                    parameters.extend(clause_parameters)
                keyword_pattern = _search_pattern(keyword)
                if keyword_pattern is not None:
                    keyword_clause, keyword_parameters = _keyword_clause(
                        keyword_pattern,
                        file_projects,
                    )
                    clauses.append(keyword_clause)
                    parameters.extend(keyword_parameters)
                where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
                statement = (
                    "SELECT path, kind, timestamp, model, usage_json, "
                    f"billing_usage_json, project FROM usage_delta{where} "
                    "ORDER BY timestamp DESC, rowid DESC"
                )
                if limit is not None:
                    statement += " LIMIT ?"
                    parameters.append(int(limit))
                rows = connection.execute(statement, parameters).fetchall()
        except sqlite3.DatabaseError:
            # 中文注释：索引损坏或并发写入冲突时退回空结果，不影响 Dashboard。
            return ()
        result: list[_IndexRow] = []
        for row in rows:
            timestamp = row[2]
            model = row[3]
            usage_json = row[4]
            if not isinstance(timestamp, (int, float)) or not isinstance(model, str):
                continue
            if not isinstance(usage_json, str):
                continue
            billing = row[5] if isinstance(row[5], str) else None
            project = row[6] if isinstance(row[6], str) and row[6] else None
            if project is None:
                # 中文注释：索引行本身不带项目时回退到文件级 session 工作目录。
                project = file_projects.get(str(row[0]))
            result.append(
                _IndexRow(
                    path=str(row[0]),
                    kind=str(row[1]),
                    timestamp=float(timestamp),
                    model=model,
                    usage_json=usage_json,
                    billing_usage_json=billing,
                    project=project,
                )
            )
        return tuple(result)

    def session_rows(self, paths: Sequence[str]) -> tuple[_IndexRow, ...]:
        """按路径读取会话的索引记录，用于统计轮数、上下文和累计用量。"""

        wanted = [str(path) for path in paths if path][:500]
        if not wanted:
            return ()
        placeholders = ", ".join("?" for _ in wanted)
        statement = (
            "SELECT path, kind, timestamp, model, usage_json, "
            "billing_usage_json, project FROM usage_delta "
            f"WHERE path IN ({placeholders}) ORDER BY path, timestamp, rowid"
        )
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(statement, wanted).fetchall()
        except sqlite3.DatabaseError:
            return ()
        result: list[_IndexRow] = []
        for row in rows:
            if not isinstance(row[2], (int, float)) or not isinstance(row[3], str):
                continue
            if not isinstance(row[4], str):
                continue
            result.append(
                _IndexRow(
                    path=str(row[0]),
                    kind=str(row[1]),
                    timestamp=float(row[2]),
                    model=str(row[3]),
                    usage_json=row[4],
                    billing_usage_json=row[5] if isinstance(row[5], str) else None,
                    project=row[6] if isinstance(row[6], str) else None,
                )
            )
        return tuple(result)

    def supports_json_aggregation(self) -> bool:
        """探测索引是否支持 JSON1 聚合（用量检索的快速路径）。"""

        if self._json1 is None:
            try:
                with closing(self._connect()) as connection:
                    connection.execute(
                        "SELECT json_extract('{\"a\":1}', '$.a')"
                    ).fetchone()
                self._json1 = True
            except sqlite3.DatabaseError:
                self._json1 = False
        return self._json1

    def has_pending_long_context(self) -> bool:
        """判断是否还有未回填长上下文标记的行（结果只探测一次）。"""

        if self._pending_checked:
            return False
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT 1 FROM usage_delta WHERE long_context IS NULL LIMIT 1"
                ).fetchone()
        except sqlite3.DatabaseError:
            return True
        if row is None:
            self._pending_checked = True
            return False
        return True

    def search_groups(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        models: Sequence[str] = (),
        session: str | None = None,
        project: str | None = None,
        keyword: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """在 SQL 里按「日期 + 会话 + 模型 + 长上下文」聚合出 token 分桶。

        只返回聚合结果，不把逐条用量读进 Python；成本由调用方用每个分桶的
        显式长上下文标记计算，保证与逐条估算一致。
        """

        clauses: list[str] = [
            "(kind = 'total' OR path NOT IN "
            "(SELECT path FROM usage_delta WHERE kind = 'total'))"
        ]
        parameters: list[Any] = []
        if since is not None:
            clauses.append("timestamp >= ?")
            parameters.append(float(since))
        if until is not None:
            clauses.append("timestamp <= ?")
            parameters.append(float(until))
        selected_models = [item for item in models if item]
        if selected_models:
            placeholders = ", ".join("?" for _ in selected_models)
            clauses.append(f"model IN ({placeholders})")
            parameters.extend(selected_models)
        session_pattern = _search_pattern(session)
        if session_pattern is not None:
            clauses.append("path LIKE ? ESCAPE '\\'")
            parameters.append(session_pattern)
        try:
            with closing(self._connect()) as connection:
                file_projects = _file_projects(connection)
                project_pattern = _search_pattern(project)
                if project_pattern is not None:
                    clause, clause_parameters = _project_clause(
                        project_pattern,
                        file_projects,
                    )
                    clauses.append(clause)
                    parameters.extend(clause_parameters)
                keyword_pattern = _search_pattern(keyword)
                if keyword_pattern is not None:
                    keyword_clause, keyword_parameters = _keyword_clause(
                        keyword_pattern,
                        file_projects,
                    )
                    clauses.append(keyword_clause)
                    parameters.extend(keyword_parameters)
                where = " WHERE " + " AND ".join(clauses)
                connection.row_factory = sqlite3.Row
                statement = (
                    "SELECT strftime('%Y-%m-%d', timestamp, 'unixepoch', 'localtime') "
                    "AS day, path, model, COALESCE(long_context, 0) AS long_context, "
                    "COUNT(*) AS records, MIN(timestamp) AS first_at, "
                    "MAX(timestamp) AS last_at, "
                    "SUM(json_extract(usage_json, '$.input_tokens')) AS input_tokens, "
                    "SUM(json_extract(usage_json, '$.cached_input_tokens')) "
                    "AS cached_input_tokens, "
                    "SUM(json_extract(usage_json, '$.cache_write_input_tokens')) "
                    "AS cache_write_input_tokens, "
                    "SUM(json_extract(usage_json, '$.output_tokens')) AS output_tokens, "
                    "SUM(json_extract(usage_json, '$.reasoning_output_tokens')) "
                    "AS reasoning_output_tokens, "
                    "SUM(json_extract(usage_json, '$.total_tokens')) AS total_tokens, "
                    "SUM(COALESCE(json_extract(billing_usage_json, '$.input_tokens'), "
                    "json_extract(usage_json, '$.input_tokens'))) AS billing_input_tokens, "
                    "SUM(COALESCE(json_extract(billing_usage_json, "
                    "'$.cached_input_tokens'), "
                    "json_extract(usage_json, '$.cached_input_tokens'))) "
                    "AS billing_cached_input_tokens, "
                    "SUM(COALESCE(json_extract(billing_usage_json, "
                    "'$.cache_write_input_tokens'), "
                    "json_extract(usage_json, '$.cache_write_input_tokens'))) "
                    "AS billing_cache_write_input_tokens, "
                    "SUM(COALESCE(json_extract(billing_usage_json, '$.output_tokens'), "
                    "json_extract(usage_json, '$.output_tokens'))) "
                    "AS billing_output_tokens "
                    f"FROM usage_delta{where} "
                    "GROUP BY day, path, model, long_context "
                    "ORDER BY last_at DESC"
                )
                rows = connection.execute(statement, parameters).fetchall()
                results: list[dict[str, Any]] = []
                for row in rows:
                    payload = dict(row)
                    project = payload.get("project")
                    if not (isinstance(project, str) and project):
                        # 中文注释：索引行不带项目时回退到文件级 session 工作目录。
                        project = file_projects.get(str(payload.get("path")))
                    payload["project"] = project
                    results.append(payload)
        except sqlite3.DatabaseError:
            return ()
        return tuple(results)

    def facets(self) -> dict[str, Any]:
        """返回索引整体的模型列表、会话数量和覆盖时间范围。"""

        try:
            with closing(self._connect()) as connection:
                summary = connection.execute(
                    "SELECT COUNT(*), MIN(timestamp), MAX(timestamp), "
                    "COUNT(DISTINCT path), COUNT(DISTINCT model) FROM usage_delta"
                ).fetchone()
                model_rows = connection.execute(
                    "SELECT DISTINCT model FROM usage_delta "
                    "WHERE model <> '' ORDER BY model"
                ).fetchall()
        except sqlite3.DatabaseError:
            return {
                "records": 0,
                "sessions": 0,
                "models": [],
                "first_at": None,
                "last_at": None,
            }
        return {
            "records": int(summary[0] or 0),
            "sessions": int(summary[3] or 0),
            "models": [str(row[0]) for row in model_rows],
            "first_at": float(summary[1]) if summary[1] is not None else None,
            "last_at": float(summary[2]) if summary[2] is not None else None,
        }

    def save(
        self,
        path: Path,
        cached_file: _CachedFile,
        total_deltas: tuple[UsageDelta, ...],
        fallback_deltas: tuple[UsageDelta, ...],
        replace_deltas: bool,
    ) -> None:
        """保存文件检查点，只追加本轮新产生的增量。"""

        try:
            with closing(self._connect()) as connection, connection:
                if replace_deltas:
                    connection.execute(
                        "DELETE FROM usage_delta WHERE path = ?",
                        (str(path),),
                    )
                rows = [
                    _delta_to_row(str(path), "total", delta)
                    for delta in total_deltas
                ]
                rows.extend(
                    _delta_to_row(str(path), "fallback", delta)
                    for delta in fallback_deltas
                )
                if rows:
                    connection.executemany(
                        """
                        INSERT INTO usage_delta(
                            path, kind, timestamp, model, usage_json,
                            billing_usage_json, project, long_context
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                connection.execute(
                    """
                    INSERT INTO usage_file_state(
                        path, inode, mtime_ns, file_size, next_offset,
                        complete, state_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        inode = excluded.inode,
                        mtime_ns = excluded.mtime_ns,
                        file_size = excluded.file_size,
                        next_offset = excluded.next_offset,
                        complete = excluded.complete,
                        state_json = excluded.state_json
                    """,
                    (
                        str(path),
                        cached_file.signature[0],
                        cached_file.signature[1],
                        cached_file.signature[2],
                        cached_file.next_offset,
                        int(cached_file.complete),
                        _state_to_json(cached_file.state),
                    ),
                )
        except (OSError, OverflowError, TypeError, ValueError, sqlite3.DatabaseError):
            # 中文注释：持久化失败只损失下次启动的缓存，不中断额度监控。
            return

    def prune(self, paths: Mapping[Path, Any]) -> None:
        """删除已经不在当前扫描范围的文件索引。"""

        allowed = {str(path) for path in paths}
        try:
            with closing(self._connect()) as connection, connection:
                rows = connection.execute(
                    "SELECT path FROM usage_file_state"
                ).fetchall()
                stale = [(row[0],) for row in rows if row[0] not in allowed]
                if stale:
                    connection.executemany(
                        "DELETE FROM usage_delta WHERE path = ?",
                        stale,
                    )
                    connection.executemany(
                        "DELETE FROM usage_file_state WHERE path = ?",
                        stale,
                    )
        except (OSError, sqlite3.DatabaseError):
            return


def _state_to_json(state: _UsageParseState) -> str:
    """把追加解析状态序列化为不含原文的 JSON。"""

    payload = {
        "total_baseline": (
            state.total_baseline.to_dict() if state.total_baseline is not None else None
        ),
        "last_baseline": (
            state.last_baseline.to_dict() if state.last_baseline is not None else None
        ),
        "has_total_usage": state.has_total_usage,
        "current_model": state.current_model,
        "project": state.project,
        "previous_timestamp": state.previous_timestamp,
        "discarding_oversized_line": state.discarding_oversized_line,
        "recent_ids": list(state.recent_ids),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _state_from_json(value: object) -> _UsageParseState | None:
    """从持久化 JSON 恢复追加解析状态。"""

    if not isinstance(value, str):
        return None
    payload = json.loads(value)
    if not isinstance(payload, Mapping):
        return None
    total_baseline = _usage_from_object(payload.get("total_baseline"))
    last_baseline = _usage_from_object(payload.get("last_baseline"))
    current_model = payload.get("current_model", _UNKNOWN_MODEL)
    project = payload.get("project")
    previous_timestamp = payload.get("previous_timestamp", 0.0)
    recent_ids_value = payload.get("recent_ids", ())
    recent_ids = (
        tuple(item for item in recent_ids_value if isinstance(item, str))
        if isinstance(recent_ids_value, (list, tuple))
        else ()
    )
    if not isinstance(current_model, str):
        return None
    if project is not None and not isinstance(project, str):
        return None
    if not isinstance(previous_timestamp, (int, float)):
        return None
    return _UsageParseState(
        total_baseline=total_baseline,
        last_baseline=last_baseline,
        has_total_usage=bool(payload.get("has_total_usage")),
        current_model=current_model,
        project=project,
        previous_timestamp=float(previous_timestamp),
        discarding_oversized_line=bool(
            payload.get("discarding_oversized_line")
        ),
        recent_ids=recent_ids,
    )


def _usage_from_object(value: object) -> TokenUsage | None:
    """从持久化对象恢复 token 用量。"""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("持久化 token 用量不是对象")
    return TokenUsage.from_mapping(value)


def search_since_days(days: float, now: float | None = None) -> float:
    """把「近 N 天」换算成按分钟对齐的起始时间。

    对齐到分钟是为了让相同的筛选条件落到同一个检索缓存键上：如果直接用
    ``time.time() - days * 86400``，每次请求的起始时间都差几秒，缓存永远不命中。
    """

    observed_at = time.time() if now is None else float(now)
    aligned = math.floor(observed_at / 60.0) * 60.0
    return aligned - float(days) * 86_400.0


def _search_pattern(value: str | None) -> str | None:
    """把检索关键词转义成 LIKE 模式；空关键词返回 None。"""

    if value is None:
        return None
    text = str(value).strip()[:_MAX_SEARCH_KEYWORD_BYTES]
    if not text:
        return None
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def enrich_session_views(
    views: Sequence[dict[str, Any]],
    aggregator: "UsageAggregator | None",
    thresholds: "SessionSwitchThresholds | None" = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """给统一会话视图补充 token、上下文、轮数与切换新会话的提醒。

    Dashboard 和命令行共用这一个入口：视图里的 ``jsonl_path`` 是唯一取数依据，
    读取失败时静默返回原视图，不影响其他功能。返回 ``(视图列表, 提醒列表)``。
    """

    views_out = list(views)
    if aggregator is None:
        return views_out, []
    resolved = thresholds or SessionSwitchThresholds()
    paths = [
        str(view.get("jsonl_path"))
        for view in views_out
        if view.get("jsonl_path")
    ]
    if not paths:
        return views_out, []
    try:
        usages = aggregator.session_usages(paths)
    except (OSError, ValueError):
        return views_out, []
    reminders: list[dict[str, Any]] = []
    for view in views_out:
        path = str(view.get("jsonl_path") or "")
        if not path:
            continue
        usage = usages.get(path)
        if usage is None:
            continue
        payload = usage.to_dict()
        reminder = usage.reminder(resolved)
        payload["reminder"] = reminder
        view["usage"] = payload
        # 中文注释：统一模型的 token 字段由用量索引回填，discovery 阶段为 0。
        view["tokens"] = payload["total_tokens"]
        view["context_tokens"] = payload["context_tokens"]
        view["turns"] = payload["turns"]
        if not view.get("model"):
            view["model"] = payload["model"]
        if reminder is None:
            continue
        reminders.append(
            {
                **reminder,
                "thread_id": view.get("thread_id"),
                "account": view.get("account"),
                "product": view.get("product"),
                "cwd": view.get("cwd"),
                "project": view.get("project"),
            }
        )
    reminders.sort(key=lambda item: -int(item.get("context_tokens") or 0))
    return views_out, reminders


def _session_usage_from_rows(
    path: str,
    rows: Sequence[_IndexRow],
) -> SessionUsage | None:
    """把索引行折叠成一个会话的用量汇总。"""

    total_rows = [row for row in rows if row.kind == "total"]
    selected = total_rows or [row for row in rows if row.kind == "fallback"]
    deltas: list[UsageDelta] = []
    for row in selected:
        delta = row.delta()
        if delta is not None:
            deltas.append(delta)
    return _session_usage_from_deltas(path, tuple(deltas))


def _session_usage_from_deltas(
    path: str,
    deltas: Sequence[UsageDelta],
) -> SessionUsage | None:
    """把会话的 token 增量折叠成轮数、上下文和成本。"""

    if not deltas:
        return None
    totals = TokenUsage()
    cost = _ModelCost()
    for delta in deltas:
        totals = totals.add(delta.usage)
        cost.add(_estimate_usage(delta.billing_usage or delta.usage, delta.model))
    last = max(deltas, key=lambda item: item.timestamp)
    return SessionUsage(
        path=path,
        turns=len(deltas),
        total_tokens=totals.total_tokens,
        # 中文注释：一次增量的 input_tokens 就是该轮请求的上下文规模，
        # 取最近一轮作为「当前上下文」的近似值。
        context_tokens=last.usage.input_tokens,
        model=last.model,
        first_at=min(item.timestamp for item in deltas),
        last_at=last.timestamp,
        estimated_cost_usd=(
            _round_number(cost.estimated_cost_usd)
            if cost.estimated_cost_usd is not None
            else None
        ),
        api_pricing_known=cost.api_pricing_known,
    )


def _file_projects(connection: sqlite3.Connection) -> dict[str, str]:
    """读取每个 JSONL 文件解析出的 session 工作目录。"""

    try:
        rows = connection.execute(
            "SELECT path, state_json FROM usage_file_state"
        ).fetchall()
    except sqlite3.DatabaseError:
        return {}
    projects: dict[str, str] = {}
    for path, state_json in rows:
        if not isinstance(state_json, str):
            continue
        try:
            state = _state_from_json(state_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if state is not None and state.project:
            projects[str(path)] = state.project
    return projects


def _matching_paths(pattern: str, projects: Mapping[str, str]) -> list[str]:
    """返回项目路径匹配关键词的文件，用于按项目筛选记录。"""

    matches = [
        path
        for path, project in projects.items()
        if _matches_pattern(project, pattern)
    ]
    # 中文注释：极端情况下避免生成过长的 IN 子句。
    return matches[:500]


def _matches_pattern(value: str, pattern: str) -> bool:
    """按 LIKE 转义规则做一次大小写不敏感匹配。"""

    needle = pattern.strip("%").replace("\\%", "%").replace("\\_", "_")
    return needle.lower() in value.lower()


def _project_clause(
    pattern: str,
    projects: Mapping[str, str],
) -> tuple[str, list[Any]]:
    """生成项目筛选子句：记录自带项目或文件级项目命中都算匹配。"""

    paths = _matching_paths(pattern, projects)
    clause = "COALESCE(project, '') LIKE ? ESCAPE '\\'"
    parameters: list[Any] = [pattern]
    if paths:
        placeholders = ", ".join("?" for _ in paths)
        clause = f"({clause} OR path IN ({placeholders}))"
        parameters.extend(paths)
    return clause, parameters


def _keyword_clause(
    pattern: str,
    projects: Mapping[str, str],
) -> tuple[str, list[Any]]:
    """生成关键词子句：匹配会话路径、模型、项目或文件级项目。"""

    paths = _matching_paths(pattern, projects)
    clause = (
        "(path LIKE ? ESCAPE '\\' OR COALESCE(project, '') LIKE ? ESCAPE '\\' "
        "OR model LIKE ? ESCAPE '\\'"
    )
    parameters: list[Any] = [pattern, pattern, pattern]
    if paths:
        placeholders = ", ".join("?" for _ in paths)
        clause += f" OR path IN ({placeholders})"
        parameters.extend(paths)
    return f"{clause})", parameters


def _local_day(timestamp: float) -> str:
    """把时间戳格式化为本地自然日，与用量趋势的日期口径一致。"""

    return (
        datetime.fromtimestamp(float(timestamp))
        .astimezone()
        .strftime("%Y-%m-%d")
    )


def _search_bucket_key(
    group: str,
    date_key: str,
    path: str,
    model: str,
) -> tuple[Any, ...]:
    """返回一个检索分组的键：按会话、按日期或按模型。"""

    if group == "date":
        return (date_key,)
    if group == "model":
        return (model,)
    return (date_key, path, model)


def _new_search_bucket(
    group: str,
    key: tuple[Any, ...],
    day: str,
    path: str,
    model: str,
    project: str | None,
    timestamp: float,
) -> dict[str, Any]:
    """创建一个检索分组（会话明细 / 按日期 / 按模型共用）。"""

    return {
        "key": "|".join(str(part) for part in key),
        "date": day,
        "session_id": session_id_from_path(Path(path)) if group == "session" else None,
        "session_path": path if group == "session" else None,
        "model": model if group == "session" else None,
        "models": {},
        "project": project,
        "usage": TokenUsage(),
        "cost": _ModelCost(),
        "records": 0,
        "first_at": timestamp,
        "last_at": timestamp,
    }


def _accumulate_search_bucket(
    bucket: dict[str, Any],
    *,
    usage: TokenUsage,
    estimate: Mapping[str, Any],
    model: str,
    first_at: float,
    last_at: float,
    records: int,
    project: str | None,
) -> None:
    """把一批 token 与成本累加进检索分组。"""

    bucket["usage"] = bucket["usage"].add(usage)
    bucket["cost"].add(estimate)
    bucket["models"][model] = bucket["models"].get(model, 0) + usage.total_tokens
    bucket["records"] += records
    bucket["first_at"] = min(bucket["first_at"], first_at)
    bucket["last_at"] = max(bucket["last_at"], last_at)
    if bucket["project"] is None:
        bucket["project"] = project


def _token_usage_from_row(row: Mapping[str, Any], prefix: str = "") -> TokenUsage:
    """从 SQL 聚合行读取 token 求和字段。"""

    return TokenUsage(
        input_tokens=int(row.get(f"{prefix}input_tokens") or 0),
        cached_input_tokens=int(row.get(f"{prefix}cached_input_tokens") or 0),
        cache_write_input_tokens=int(row.get(f"{prefix}cache_write_input_tokens") or 0),
        output_tokens=int(row.get(f"{prefix}output_tokens") or 0),
        reasoning_output_tokens=int(row.get(f"{prefix}reasoning_output_tokens") or 0),
        total_tokens=int(row.get(f"{prefix}total_tokens") or 0),
    )


def _search_row_to_dict(bucket: Mapping[str, Any]) -> dict[str, Any]:
    """把一个检索分组转换为 Dashboard / CLI 安全输出。"""

    usage: TokenUsage = bucket["usage"]
    cost: _ModelCost = bucket["cost"]
    models = [
        name
        for name, _ in sorted(
            bucket["models"].items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    payload: dict[str, Any] = {
        "key": bucket["key"],
        "date": bucket["date"],
        "session_id": bucket["session_id"],
        "session_path": bucket["session_path"],
        "model": bucket["model"],
        "models": models[:8],
        "project": bucket["project"],
        "usage": usage.to_dict(),
        "total_tokens": usage.total_tokens,
        "records": bucket["records"],
        "first_at": bucket["first_at"],
        "last_at": bucket["last_at"],
    }
    payload.update(cost.to_dict())
    return payload


def _search_sort_key(sort: str) -> Any:
    """返回检索结果的排序键：最近、token 总量或金额。"""

    if sort == "tokens":
        return lambda row: (-int(row["total_tokens"]), -float(row["last_at"]))
    if sort == "cost":
        return lambda row: (
            row["estimated_cost_usd"] is None,
            -float(row["estimated_cost_usd"] or 0.0),
            -float(row["last_at"]),
        )
    return lambda row: (-float(row["last_at"]), -int(row["total_tokens"]))


def _empty_search_totals() -> dict[str, Any]:
    """返回空检索的合计结构。"""

    return {
        "usage": TokenUsage().to_dict(),
        "total_tokens": 0,
        "cost_usd": None,
        "api_pricing_known": True,
        "cache_savings_usd": None,
        "rows": 0,
        "records": 0,
        "sessions": 0,
        "models": 0,
        "first_at": None,
        "last_at": None,
    }


def _delta_to_row(path: str, kind: str, delta: UsageDelta) -> tuple[object, ...]:
    """把一个 token 增量转换成 SQLite 行。"""

    billing_usage = delta.billing_usage or delta.usage
    return (
        path,
        kind,
        delta.timestamp,
        delta.model,
        json.dumps(delta.usage.to_dict(), separators=(",", ":")),
        json.dumps(billing_usage.to_dict(), separators=(",", ":")),
        delta.project,
        _long_context_flag(billing_usage, delta.model),
    )


def _long_context_flag(usage: TokenUsage, model: str) -> int:
    """判断一次用量是否按长上下文计价，结果随行落盘供聚合查询分组。"""

    pricing = _lookup_pricing(model)
    if pricing is None:
        return 0
    return 1 if _is_long_context(usage.input_tokens, pricing) else 0


def _has_pending_long_context(connection: sqlite3.Connection) -> bool:
    """判断索引里是否还有等待回填长上下文标记的行。"""

    try:
        row = connection.execute(
            "SELECT 1 FROM usage_delta WHERE long_context IS NULL LIMIT 1"
        ).fetchone()
    except sqlite3.DatabaseError:
        return False
    return row is not None


def _backfill_long_context(connection: sqlite3.Connection) -> int:
    """给旧索引行补齐长上下文标记，只做一次。"""

    rows = connection.execute(
        "SELECT rowid, model, billing_usage_json, usage_json FROM usage_delta"
    ).fetchall()
    updates: list[tuple[int, int]] = []
    for row_id, model, billing_json, usage_json in rows:
        if not isinstance(model, str):
            continue
        payload = billing_json if isinstance(billing_json, str) else usage_json
        if not isinstance(payload, str):
            continue
        try:
            usage = _usage_from_object(json.loads(payload))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if usage is None:
            continue
        updates.append((_long_context_flag(usage, model), int(row_id)))
    if updates:
        connection.executemany(
            "UPDATE usage_delta SET long_context = ? WHERE rowid = ?",
            updates,
        )
    return len(updates)


def _delta_from_row(row: tuple[object, ...]) -> UsageDelta | None:
    """从 SQLite 行恢复一个 token 增量。"""

    timestamp = row[1]
    model = row[2]
    if not isinstance(timestamp, (int, float)) or not isinstance(model, str):
        return None
    usage = _usage_from_object(json.loads(row[3]))
    billing_value = row[4]
    billing_usage = (
        _usage_from_object(json.loads(billing_value))
        if isinstance(billing_value, str)
        else None
    )
    if usage is None:
        return None
    project = row[5] if len(row) > 5 else None
    if project is not None and not isinstance(project, str):
        project = None
    return UsageDelta(
        timestamp=float(timestamp),
        model=model,
        usage=usage,
        billing_usage=billing_usage,
        project=project,
    )


@dataclass
class _ModelCost:
    """一个模型的可计价增量汇总。"""

    estimated_credits: float | None = 0.0
    estimated_cost_usd: float | None = 0.0
    cache_savings_usd: float | None = 0.0
    credits_pricing_known: bool = True
    api_pricing_known: bool = True

    def add(self, estimate: Mapping[str, Any]) -> None:
        """合并一次模型估算，并保留未知价格。"""

        credits = estimate.get("estimated_credits")
        if credits is None:
            self.estimated_credits = None
            self.credits_pricing_known = False
        elif self.estimated_credits is not None:
            self.estimated_credits += float(credits)
        cost = estimate.get("estimated_cost_usd")
        if cost is None:
            self.estimated_cost_usd = None
            self.api_pricing_known = False
        elif self.estimated_cost_usd is not None:
            self.estimated_cost_usd += float(cost)
        savings = estimate.get("cache_savings_usd")
        if savings is None:
            self.cache_savings_usd = None
        elif self.cache_savings_usd is not None:
            self.cache_savings_usd += float(savings)

    def to_dict(self) -> dict[str, Any]:
        """转换为 Dashboard 使用的估算字段。"""

        return {
            "estimated_credits": (
                _round_number(self.estimated_credits)
                if self.estimated_credits is not None
                else None
            ),
            "estimated_cost_usd": (
                _round_number(self.estimated_cost_usd)
                if self.estimated_cost_usd is not None
                else None
            ),
            "api_equivalent_cost_usd": (
                _round_number(self.estimated_cost_usd)
                if self.estimated_cost_usd is not None
                else None
            ),
            "cache_savings_usd": (
                _round_number(self.cache_savings_usd)
                if self.cache_savings_usd is not None
                else None
            ),
            "subscription_cost_usd": None,
            "credits_pricing_known": self.credits_pricing_known,
            "api_pricing_known": self.api_pricing_known,
        }


@dataclass
class _UsageAggregate:
    """一个账号或模型的可变汇总。"""

    usage: TokenUsage = field(default_factory=TokenUsage)
    profiles: set[str] = field(default_factory=set)
    models: dict[str, TokenUsage] = field(default_factory=dict)
    model_costs: dict[str, _ModelCost] = field(default_factory=dict)
    projects: dict[str, "_UsageAggregate"] = field(default_factory=dict)

    def add(self, source: _UsageSource, delta: UsageDelta) -> None:
        """合并一次 JSONL 增量。"""

        self._add_totals(source, delta)
        project = delta.project or source.project or _UNKNOWN_PROJECT
        project_aggregate = self.projects.setdefault(
            project,
            _UsageAggregate(),
        )
        project_aggregate._add_totals(source, delta)

    def _add_totals(self, source: _UsageSource, delta: UsageDelta) -> None:
        """只更新当前层级的 token 和模型统计，不递归创建项目。"""

        self.usage = self.usage.add(delta.usage)
        self.profiles.add(source.profile_name)
        previous = self.models.get(delta.model, TokenUsage())
        self.models[delta.model] = previous.add(delta.usage)
        cost = self.model_costs.setdefault(delta.model, _ModelCost())
        cost.add(_estimate_usage(delta.billing_usage or delta.usage, delta.model))


@dataclass(frozen=True)
class _ConversationMetrics:
    """一个对话文件的规模、成本和时段分布，用于习惯分析。"""

    label: str
    account: str
    project: str | None
    turns: int
    first_at: float
    last_at: float
    usage: TokenUsage
    estimated_cost_usd: float
    has_unpriced: bool
    cache_write_premium_usd: float
    hour_tokens: tuple[tuple[int, int], ...]
    day_tokens: tuple[tuple[str, int], ...]
    model_usage: tuple[tuple[str, TokenUsage, float | None], ...]


class UsageAggregator:
    """扫描多个 CODEX_HOME 并按账号、时间窗口汇总 JSONL 用量。"""
    # 中文注释：单轮和单文件都最多读取 1 MiB，避免冷的 ext4.vhdx 突发读满。
    _DEFAULT_READ_BUDGET = 1 * 1024 * 1024
    _PER_FILE_READ_BUDGET = 1 * 1024 * 1024
    _PERIODS = (
        ("today", "今天", "calendar_day"),
        ("seven_days", "近 7 天", "seven_days"),
        ("month", "近 30 天", "thirty_days"),
        ("year", "近 365 天", "three_hundred_sixty_five_days"),
    )

    _DAILY_TREND_DAYS = 30

    # 中文注释：后台索引限速，避免冷 vhdx 被一次读满，同时不再靠手工连点。
    _DEFAULT_INDEX_BYTES_PER_SEC = 1 * 1024 * 1024

    def __init__(
        self,
        discovery_interval: float = 300.0,
        refresh_interval: float = 300.0,
        read_budget_bytes: int = _DEFAULT_READ_BUDGET,
        cache_path: Path | None = None,
        background_indexing: bool = False,
        index_bytes_per_sec: int = _DEFAULT_INDEX_BYTES_PER_SEC,
        grok_homes: Sequence[Path] | None = None,
        kimi_homes: Sequence[Path] | None = None,
        dsh_homes: Sequence[Path] | None = None,
        claude_homes: Sequence[Path] | None = None,
    ) -> None:
        """创建有刷新间隔、持久化检查点和单轮磁盘预算的用量缓存。"""

        if discovery_interval <= 0:
            raise ValueError("discovery_interval 必须大于 0")
        if refresh_interval <= 0:
            raise ValueError("refresh_interval 必须大于 0")
        if read_budget_bytes <= 0:
            raise ValueError("read_budget_bytes 必须大于 0")
        if index_bytes_per_sec <= 0:
            raise ValueError("index_bytes_per_sec 必须大于 0")
        self.discovery_interval = discovery_interval
        self.refresh_interval = refresh_interval
        self.read_budget_bytes = read_budget_bytes
        self.background_indexing = background_indexing
        self.index_bytes_per_sec = index_bytes_per_sec
        self._grok_homes = (
            resolve_grok_homes(grok_homes) if grok_homes is not None else ()
        )
        self._grok_sessions: dict[Path, dict[str, GrokSessionInfo]] = {}
        self._grok_sessions_at: dict[Path, float] = {}
        self._kimi_homes = (
            resolve_kimi_homes(kimi_homes) if kimi_homes is not None else ()
        )
        self._kimi_sessions: dict[Path, dict[str, KimiSessionInfo]] = {}
        self._kimi_sessions_at: dict[Path, float] = {}
        self._dsh_homes = (
            resolve_dsh_homes(dsh_homes) if dsh_homes is not None else ()
        )
        self._claude_homes = (
            resolve_claude_homes(claude_homes) if claude_homes is not None else ()
        )
        self._claude_sidechain_cache: dict[Path, tuple[float, bool]] = {}
        self._cache: dict[Path, _CachedFile] = {}
        self._discovered: dict[Path, tuple[Path, ...]] = {}
        self._discovered_at: dict[Path, float] = {}
        self._snapshot_cache: dict[str, Any] | None = None
        self._snapshot_cached_at = 0.0
        self._snapshot_scope: tuple[tuple[str, str, str, str], ...] = ()
        self._facets_cache: dict[str, Any] | None = None
        self._facets_cached_at = 0.0
        self._search_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
        self._index_cursor = 0
        self._deduplicated_files = 0
        self._deduplicated_bytes = 0
        self._last_indexed_at: float | None = None
        self._last_index_error: str | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._job: tuple[
            Mapping[str, MultiSessionRegistry],
            Mapping[str, Mapping[str, str | None]],
            float | None,
        ] | None = None
        self._persistent = None
        if cache_path is not None:
            try:
                self._persistent = _UsageIndexStore(cache_path)
            except (OSError, sqlite3.DatabaseError):
                # 中文注释：无法创建索引时仍可退化为内存缓存，不能影响监控主流程。
                self._persistent = None

    def snapshot(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        account_metadata: Mapping[str, Mapping[str, str | None]] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """读取所有 session JSONL 并返回四个时间范围的安全汇总。"""

        metadata_by_profile = account_metadata or {}
        scope = self._scope_key(registries, metadata_by_profile)
        with self._lock:
            self._job = (registries, metadata_by_profile, now)
            if self.background_indexing:
                self._ensure_worker()
            current_monotonic = time.monotonic()
            cached_snapshot = self._snapshot_cache
            indexing_complete = True
            if isinstance(cached_snapshot, Mapping):
                indexing = cached_snapshot.get("indexing")
                if isinstance(indexing, Mapping):
                    indexing_complete = bool(indexing.get("complete", True))
            if (
                now is None
                and cached_snapshot is not None
                and scope == self._snapshot_scope
            ):
                if self.background_indexing and not indexing_complete:
                    # 中文注释：后台线程正在继续读盘，HTTP 请求不再同步读取历史。
                    return cached_snapshot
                if (
                    indexing_complete
                    and current_monotonic - self._snapshot_cached_at
                    < self.refresh_interval
                ):
                    return cached_snapshot
            return self._run_index_once(registries, metadata_by_profile, now, scope)

    def close(self) -> None:
        """停止后台索引线程。"""

        self._stop.set()
        worker = self._worker
        self._worker = None
        if worker is not None and worker.is_alive():
            worker.join(timeout=2)

    def update_homes(
        self,
        *,
        grok_homes: Sequence[Path] | None = None,
        kimi_homes: Sequence[Path] | None = None,
        dsh_homes: Sequence[Path] | None = None,
        claude_homes: Sequence[Path] | None = None,
    ) -> None:
        """热更新各 provider 的扫描目录；None 保持不变，显式元组（含空）替换。

        只替换实例属性，下一轮 ``_index_once``/``_build_sources`` 自动按新目录
        建源；被移除目录的内存缓存和索引行由 ``_remove_stale_cache`` 和
        ``store.prune`` 在下一轮清出，不删除磁盘上的索引文件。
        """

        with self._lock:
            if grok_homes is not None:
                self._grok_homes = resolve_grok_homes(grok_homes)
                self._grok_sessions = {
                    home: index
                    for home, index in self._grok_sessions.items()
                    if home in self._grok_homes
                }
                self._grok_sessions_at = {
                    home: cached_at
                    for home, cached_at in self._grok_sessions_at.items()
                    if home in self._grok_homes
                }
            if kimi_homes is not None:
                self._kimi_homes = resolve_kimi_homes(kimi_homes)
                self._kimi_sessions = {
                    home: index
                    for home, index in self._kimi_sessions.items()
                    if home in self._kimi_homes
                }
                self._kimi_sessions_at = {
                    home: cached_at
                    for home, cached_at in self._kimi_sessions_at.items()
                    if home in self._kimi_homes
                }
            if dsh_homes is not None:
                self._dsh_homes = resolve_dsh_homes(dsh_homes)
            if claude_homes is not None:
                self._claude_homes = resolve_claude_homes(claude_homes)
                self._claude_sidechain_cache = {
                    home: cached
                    for home, cached in self._claude_sidechain_cache.items()
                    if home in self._claude_homes
                }

    def _ensure_worker(self) -> None:
        """启动一次性的后台索引线程。"""

        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._run_background,
            name="usage-indexer",
            daemon=True,
        )
        self._worker.start()

    def _run_background(self) -> None:
        """按限速把 JSONL 索引完，不阻塞 Dashboard 请求。"""

        while not self._stop.is_set():
            read_bytes = 0
            complete = True
            with self._lock:
                job = self._job
                if job is not None:
                    try:
                        snapshot = self._run_index_once(*job)
                    except Exception:  # noqa: BLE001 - 索引失败不能杀掉后台线程
                        # 中文注释：错误已由 _run_index_once 记录；下一轮按
                        # 完整节奏重试，避免异常造成忙等。
                        complete = True
                    else:
                        indexing = snapshot.get("indexing")
                        if isinstance(indexing, Mapping):
                            complete = bool(indexing.get("complete", True))
                            read_bytes = int(
                                indexing.get("read_bytes_this_refresh") or 0
                            )
            delay = self._background_delay(complete, read_bytes)
            if self._stop.wait(timeout=delay):
                return

    def _background_delay(self, complete: bool, read_bytes: int) -> float:
        """计算后台下一轮等待时间，限制平均读速和空转频率。"""

        if complete:
            # 中文注释：索引完成后只按正常刷新周期检查追加，禁止每 2 秒
            # 重建全部时间窗口和聚合结果。
            return self.refresh_interval
        minimum_delay = max(
            0.05,
            min(1.0, self.read_budget_bytes / self.index_bytes_per_sec),
        )
        return max(minimum_delay, read_bytes / self.index_bytes_per_sec)

    def _run_index_once(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        metadata_by_profile: Mapping[str, Mapping[str, str | None]],
        now: float | None,
        scope: tuple[tuple[str, str, str, str], ...] | None = None,
    ) -> dict[str, Any]:
        """执行一轮索引并维护健康摘要；异常记录后原样抛出。"""

        try:
            snapshot = self._index_once(registries, metadata_by_profile, now, scope)
        except Exception as error:
            self._last_index_error = sanitize_error(error)
            raise
        self._last_indexed_at = time.time()
        self._last_index_error = None
        return snapshot

    def index_health(self) -> dict[str, object]:
        """返回用量索引的健康摘要；未启用后台索引时 worker_alive 为 None。"""

        worker = self._worker
        return {
            "last_indexed_at": self._last_indexed_at,
            "last_error": self._last_index_error,
            "worker_alive": worker.is_alive() if worker is not None else None,
        }

    def _index_once(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        metadata_by_profile: Mapping[str, Mapping[str, str | None]],
        now: float | None,
        scope: tuple[tuple[str, str, str, str], ...] | None = None,
    ) -> dict[str, Any]:
        """在已持有锁的情况下读取一轮有界 JSONL 并更新快照。"""

        if scope is None:
            scope = self._scope_key(registries, metadata_by_profile)
        observed_at = time.time() if now is None else float(now)
        sources = self._build_sources(registries, metadata_by_profile)
        self._remove_stale_cache(sources)
        file_deltas: dict[Path, tuple[UsageDelta, ...]] = {}
        effective_sources = dict(sources)
        ordered_paths = tuple(sorted(sources, key=str))
        if ordered_paths:
            start = self._index_cursor % len(ordered_paths)
            ordered_paths = ordered_paths[start:] + ordered_paths[:start]
        remaining_budget = self.read_budget_bytes
        next_cursor = self._index_cursor
        read_bytes_this_refresh = 0
        for path_index, path in enumerate(ordered_paths):
            cached_file = self._cache.get(path)
            if remaining_budget > 0:
                per_file_budget = min(
                    remaining_budget,
                    self._PER_FILE_READ_BUDGET,
                )
                cached_file = self._read_file(
                    path,
                    maximum_bytes=per_file_budget,
                )
                if cached_file is not None:
                    read_bytes = cached_file.last_read_bytes
                    remaining_budget = max(0, remaining_budget - read_bytes)
                    read_bytes_this_refresh += read_bytes
                    if read_bytes > 0:
                        next_cursor = self._index_cursor + path_index + 1
            if cached_file is None:
                continue
            file_deltas[path] = cached_file.deltas
            source = sources[path]
            if source.project is None and cached_file.project is not None:
                effective_sources[path] = _UsageSource(
                    profile_name=source.profile_name,
                    account_id=source.account_id,
                    codex_home=source.codex_home,
                    project=cached_file.project,
                )
        if ordered_paths:
            self._index_cursor = next_cursor % len(ordered_paths)
        indexing = self._index_progress(
            tuple(sources),
            read_bytes_this_refresh=read_bytes_this_refresh,
        )
        indexing["bytes_per_sec"] = self.index_bytes_per_sec
        indexing["deduplicated_files"] = self._deduplicated_files
        indexing["deduplicated_bytes"] = self._deduplicated_bytes
        # 中文注释：未遍历完全部 JSONL 时不计算也不输出可被误认为最终值的数字。
        periods = (
            [
                self._build_period(
                    key=key,
                    label=label,
                    period_kind=period_kind,
                    now=observed_at,
                    sources=effective_sources,
                    file_deltas=file_deltas,
                    metadata_by_profile=metadata_by_profile,
                )
                for key, label, period_kind in self._PERIODS
            ]
            if indexing["complete"]
            else []
        )
        daily = (
            self._build_daily(observed_at, file_deltas)
            if indexing["complete"]
            else []
        )
        snapshot = {
            "observed_at": observed_at,
            "pricing": pricing_metadata(),
            "periods": periods,
            "daily": daily,
            "cache_seconds": self.refresh_interval,
            "indexing": indexing,
        }
        self._snapshot_cache = snapshot
        self._snapshot_cached_at = time.monotonic()
        self._snapshot_scope = scope
        return snapshot

    def cached_snapshot(self, now: float | None = None) -> dict[str, Any]:
        """返回内存中的用量结果，不触碰 CODEX_HOME 历史文件。"""

        with self._lock:
            if self._snapshot_cache is not None:
                return self._snapshot_cache
        return self.empty_snapshot(now=now)

    @staticmethod
    def empty_snapshot(now: float | None = None) -> dict[str, Any]:
        """返回没有扫描源时仍可被 Dashboard 使用的空结构。"""

        observed_at = time.time() if now is None else float(now)
        return {
            "observed_at": observed_at,
            "pricing": pricing_metadata(),
            "periods": [],
            "daily": [],
            "cache_seconds": 300.0,
        }

    @staticmethod
    def empty_insights(now: float | None = None) -> dict[str, Any]:
        """返回没有用量索引时仍可被 Dashboard 使用的空分析结构。"""

        return {
            "ready": False,
            "observed_at": time.time() if now is None else float(now),
            "indexing": None,
        }

    def insights(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        account_metadata: Mapping[str, Mapping[str, str | None]] | None = None,
        now: float | None = None,
        since_days: int | None = None,
    ) -> dict[str, Any]:
        """对已索引的对话做习惯分析；可按最近天数限定统计窗口。"""

        metadata_by_profile = account_metadata or {}
        with self._lock:
            snapshot = self.snapshot(registries, metadata_by_profile, now)
            indexing = snapshot.get("indexing")
            observed_at = float(snapshot.get("observed_at", time.time()))
            if not isinstance(indexing, Mapping) or not indexing.get("complete"):
                return {
                    "ready": False,
                    "observed_at": observed_at,
                    "indexing": dict(indexing) if isinstance(indexing, Mapping) else None,
                }
            since = (
                observed_at - since_days * 86_400
                if since_days is not None and since_days > 0
                else None
            )
            sources = self._build_sources(registries, metadata_by_profile)
            conversations: list[_ConversationMetrics] = []
            for path, cached_file in self._cache.items():
                metrics = _conversation_metrics(
                    path,
                    cached_file,
                    sources.get(path),
                    since=since,
                )
                if metrics is not None:
                    conversations.append(metrics)
            return _build_insights(
                conversations,
                observed_at,
                window_days=since_days if since is not None else None,
            )

    @staticmethod
    def empty_search(
        group: str = "session",
        sort: str = "recent",
        limit: int = _DEFAULT_SEARCH_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """返回没有用量索引时仍可被 Dashboard 使用的空检索结构。"""

        return {
            "available": False,
            "group": group,
            "sort": sort,
            "limit": limit,
            "offset": offset,
            "matched_rows": 0,
            "has_more": False,
            "truncated": False,
            "scanned_records": 0,
            "rows": [],
            "totals": _empty_search_totals(),
        }

    def usage_facets(self, now: float | None = None) -> dict[str, Any]:
        """返回索引整体范围，供检索页填充模型下拉和范围提示。"""

        observed_at = time.time() if now is None else float(now)
        with self._lock:
            cached = self._facets_cache
            if (
                cached is not None
                and observed_at - self._facets_cached_at < _FACETS_CACHE_SECONDS
            ):
                return cached
        store = self._persistent
        facets = (
            store.facets()
            if store is not None
            else {
                "records": 0,
                "sessions": 0,
                "models": [],
                "first_at": None,
                "last_at": None,
            }
        )
        facets["available"] = store is not None and facets["records"] > 0
        with self._lock:
            self._facets_cache = facets
            self._facets_cached_at = observed_at
        return facets

    def search(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        models: Sequence[str] = (),
        session: str | None = None,
        project: str | None = None,
        keyword: str | None = None,
        group: str = "session",
        sort: str = "recent",
        limit: int = _DEFAULT_SEARCH_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """按日期、模型和会话检索已落盘的 token 用量历史。

        优先走 SQL 聚合（把逐条用量留在 SQLite 里），只有 JSON1 不可用或旧索引
        还没补齐长上下文标记时才退回逐条扫描；相同筛选条件命中 30 秒缓存。
        """

        if group not in _USAGE_SEARCH_GROUPS:
            raise ValueError(f"未知分组方式: {group}")
        if sort not in _USAGE_SEARCH_SORTS:
            raise ValueError(f"未知排序方式: {sort}")
        if limit <= 0 or limit > _MAX_SEARCH_LIMIT:
            raise ValueError(f"limit 必须在 1 到 {_MAX_SEARCH_LIMIT} 之间")
        if offset < 0:
            raise ValueError("offset 不能小于 0")
        store = self._persistent
        if store is None:
            return self.empty_search(group=group, sort=sort, limit=limit, offset=offset)
        cache_key = (
            since,
            until,
            tuple(models),
            session,
            project,
            keyword,
            group,
            sort,
            limit,
            offset,
        )
        cached = self._cached_search(cache_key)
        if cached is not None:
            return cached
        if store.supports_json_aggregation() and not store.has_pending_long_context():
            result = self._search_grouped(
                store,
                since=since,
                until=until,
                models=models,
                session=session,
                project=project,
                keyword=keyword,
                group=group,
                sort=sort,
                limit=limit,
                offset=offset,
            )
        else:
            result = self._search_scan(
                store,
                since=since,
                until=until,
                models=models,
                session=session,
                project=project,
                keyword=keyword,
                group=group,
                sort=sort,
                limit=limit,
                offset=offset,
            )
        self._store_search_cache(cache_key, result)
        return result

    def _search_grouped(
        self,
        store: "_UsageIndexStore",
        *,
        since: float | None = None,
        until: float | None = None,
        models: Sequence[str] = (),
        session: str | None = None,
        project: str | None = None,
        keyword: str | None = None,
        group: str = "session",
        sort: str = "recent",
        limit: int = _DEFAULT_SEARCH_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """用 SQL 聚合结果拼装检索视图，成本按分桶的长上下文标记估算。"""

        rows = store.search_groups(
            since=since,
            until=until,
            models=models,
            session=session,
            project=project,
            keyword=keyword,
        )
        truncated = len(rows) > _MAX_SEARCH_ROWS
        if truncated:
            rows = rows[:_MAX_SEARCH_ROWS]
        buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
        totals_usage = TokenUsage()
        totals_cost = _ModelCost()
        session_paths: set[str] = set()
        model_names: set[str] = set()
        scanned_records = 0
        first_at: float | None = None
        last_at: float | None = None
        for row in rows:
            path = str(row["path"])
            model = str(row["model"] or _UNKNOWN_MODEL)
            day = str(row["day"] or "")
            records = int(row["records"] or 0)
            row_first = float(row["first_at"] or 0.0)
            row_last = float(row["last_at"] or 0.0)
            usage = _token_usage_from_row(row)
            billing = _token_usage_from_row(row, prefix="billing_")
            pricing = _lookup_pricing(model)
            # 中文注释：长上下文标记在建索引时按每条记录算好，这里直接使用，
            # 不会因为多条短请求相加而误判成长上下文。
            estimate = (
                _estimate_with_pricing(billing, pricing, bool(row["long_context"]))
                if pricing is not None
                else _unknown_pricing_estimate()
            )
            key = _search_bucket_key(group, day, path, model)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = _new_search_bucket(
                    group,
                    key,
                    day,
                    path,
                    model,
                    row.get("project"),
                    row_first,
                )
                buckets[key] = bucket
            _accumulate_search_bucket(
                bucket,
                usage=usage,
                estimate=estimate,
                model=model,
                first_at=row_first,
                last_at=row_last,
                records=records,
                project=row.get("project"),
            )
            scanned_records += records
            totals_usage = totals_usage.add(usage)
            totals_cost.add(estimate)
            session_paths.add(path)
            model_names.add(model)
            first_at = row_first if first_at is None else min(first_at, row_first)
            last_at = row_last if last_at is None else max(last_at, row_last)
        return self._search_payload(
            buckets=buckets,
            totals_usage=totals_usage,
            totals_cost=totals_cost,
            session_paths=session_paths,
            model_names=model_names,
            scanned_records=scanned_records,
            first_at=first_at,
            last_at=last_at,
            truncated=truncated,
            group=group,
            sort=sort,
            limit=limit,
            offset=offset,
        )

    def _search_payload(
        self,
        *,
        buckets: Mapping[tuple[Any, ...], dict[str, Any]],
        totals_usage: TokenUsage,
        totals_cost: _ModelCost,
        session_paths: set[str],
        model_names: set[str],
        scanned_records: int,
        first_at: float | None,
        last_at: float | None,
        truncated: bool,
        group: str,
        sort: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        """把分桶结果整理成 Dashboard / CLI 使用的返回结构。"""

        results = [_search_row_to_dict(bucket) for bucket in buckets.values()]
        results.sort(key=_search_sort_key(sort))
        page = results[offset : offset + limit]
        totals = {
            "usage": totals_usage.to_dict(),
            "total_tokens": totals_usage.total_tokens,
            "cost_usd": (
                _round_number(totals_cost.estimated_cost_usd)
                if totals_cost.estimated_cost_usd is not None
                else None
            ),
            "api_pricing_known": totals_cost.api_pricing_known,
            "cache_savings_usd": (
                _round_number(totals_cost.cache_savings_usd)
                if totals_cost.cache_savings_usd is not None
                else None
            ),
            "rows": len(results),
            "records": scanned_records,
            "sessions": len(session_paths),
            "models": len(model_names),
            "first_at": first_at,
            "last_at": last_at,
        }
        return {
            "available": True,
            "group": group,
            "sort": sort,
            "limit": limit,
            "offset": offset,
            "matched_rows": len(results),
            "has_more": offset + len(page) < len(results),
            "truncated": truncated,
            "scanned_records": scanned_records,
            "rows": page,
            "totals": totals,
        }

    def _cached_search(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        """读取检索结果缓存；过期条目直接丢弃。"""

        now = time.monotonic()
        with self._lock:
            entry = self._search_cache.get(key)
            if entry is None:
                return None
            if now - entry[0] >= _SEARCH_CACHE_SECONDS:
                self._search_cache.pop(key, None)
                return None
            return entry[1]

    def _store_search_cache(
        self,
        key: tuple[Any, ...],
        payload: dict[str, Any],
    ) -> None:
        """写入检索结果缓存，超过上限时丢弃最旧的一半。"""

        with self._lock:
            self._search_cache[key] = (time.monotonic(), payload)
            if len(self._search_cache) > _SEARCH_CACHE_MAX:
                for stale in sorted(
                    self._search_cache,
                    key=lambda item: self._search_cache[item][0],
                )[: len(self._search_cache) - _SEARCH_CACHE_MAX]:
                    self._search_cache.pop(stale, None)

    def _search_scan(
        self,
        store: "_UsageIndexStore",
        *,
        since: float | None = None,
        until: float | None = None,
        models: Sequence[str] = (),
        session: str | None = None,
        project: str | None = None,
        keyword: str | None = None,
        group: str = "session",
        sort: str = "recent",
        limit: int = _DEFAULT_SEARCH_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """逐条读取索引并聚合（JSON1 不可用时的回退路径）。"""

        rows = store.search_deltas(
            since=since,
            until=until,
            models=models,
            session=session,
            project=project,
            keyword=keyword,
            limit=_MAX_SEARCH_ROWS + 1,
        )
        truncated = len(rows) > _MAX_SEARCH_ROWS
        if truncated:
            rows = rows[:_MAX_SEARCH_ROWS]
        total_deltas = {row.path for row in rows if row.kind == "total"}
        buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
        totals_usage = TokenUsage()
        totals_cost = _ModelCost()
        session_paths: set[str] = set()
        model_names: set[str] = set()
        scanned_records = 0
        first_at: float | None = None
        last_at: float | None = None
        for row in rows:
            # 中文注释：一个文件同时有 total 和 fallback 时只用 total，
            # 与 _CachedFile.deltas 的取值规则保持一致，避免重复计数。
            if row.kind != "total" and row.path in total_deltas:
                continue
            delta = row.delta()
            if delta is None:
                continue
            scanned_records += 1
            estimate = _estimate_usage(delta.billing_usage or delta.usage, delta.model)
            date_key = _local_day(delta.timestamp)
            key = _search_bucket_key(group, date_key, row.path, delta.model)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = _new_search_bucket(
                    group,
                    key,
                    date_key,
                    row.path,
                    delta.model,
                    row.project,
                    delta.timestamp,
                )
                buckets[key] = bucket
            _accumulate_search_bucket(
                bucket,
                usage=delta.usage,
                estimate=estimate,
                model=delta.model,
                first_at=delta.timestamp,
                last_at=delta.timestamp,
                records=1,
                project=row.project,
            )
            totals_usage = totals_usage.add(delta.usage)
            totals_cost.add(estimate)
            session_paths.add(row.path)
            model_names.add(delta.model)
            first_at = (
                delta.timestamp if first_at is None else min(first_at, delta.timestamp)
            )
            last_at = (
                delta.timestamp if last_at is None else max(last_at, delta.timestamp)
            )

        return self._search_payload(
            buckets=buckets,
            totals_usage=totals_usage,
            totals_cost=totals_cost,
            session_paths=session_paths,
            model_names=model_names,
            scanned_records=scanned_records,
            first_at=first_at,
            last_at=last_at,
            truncated=truncated,
            group=group,
            sort=sort,
            limit=limit,
            offset=offset,
        )

    def refresh_index(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        account_metadata: Mapping[str, Mapping[str, str | None]] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """执行一轮有界索引，让没有 Dashboard 的进程也能用最新用量数据。"""

        metadata_by_profile = account_metadata or {}
        with self._lock:
            return self._run_index_once(registries, metadata_by_profile, now)

    def session_usages(self, paths: Sequence[str]) -> dict[str, SessionUsage]:
        """按 JSONL 路径返回会话的轮数、上下文和累计用量。"""

        wanted = [str(path) for path in paths if path]
        if not wanted:
            return {}
        usages: dict[str, SessionUsage] = {}
        store = self._persistent
        if store is not None:
            rows = store.session_rows(wanted)
            grouped: dict[str, list[_IndexRow]] = {}
            for row in rows:
                grouped.setdefault(row.path, []).append(row)
            for path, items in grouped.items():
                summary = _session_usage_from_rows(path, items)
                if summary is not None:
                    usages[path] = summary
        cached_by_path = {str(path): cached for path, cached in self._cache.items()}
        for path in wanted:
            if path in usages:
                continue
            cached = cached_by_path.get(str(path))
            if cached is None:
                continue
            summary = _session_usage_from_deltas(path, cached.deltas)
            if summary is not None:
                usages[path] = summary
        return usages

    def _scope_key(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        metadata_by_profile: Mapping[str, Mapping[str, str | None]],
    ) -> tuple[tuple[str, str, str, str], ...]:
        """生成缓存作用域，避免复用到另一组账号目录。"""

        registry_scope = tuple(
            sorted(
                (
                    profile_name,
                    str(registry.state_dir),
                    str(
                        metadata_by_profile.get(profile_name, {}).get("account_id")
                        or ""
                    ),
                    str(
                        metadata_by_profile.get(profile_name, {}).get("codex_home")
                        or ""
                    ),
                )
                for profile_name, registry in registries.items()
            )
        )
        grok_scope = tuple(("grok", str(home), "", "") for home in self._grok_homes)
        kimi_scope = tuple(("kimi", str(home), "", "") for home in self._kimi_homes)
        dsh_scope = tuple(("dsh", str(home), "", "") for home in self._dsh_homes)
        return registry_scope + grok_scope + kimi_scope + dsh_scope

    def _build_sources(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        metadata_by_profile: Mapping[str, Mapping[str, str | None]],
    ) -> dict[Path, _UsageSource]:
        """建立 JSONL 路径到账号身份的映射。"""

        sources: dict[Path, _UsageSource] = {}
        for profile_name, registry in registries.items():
            metadata = metadata_by_profile.get(profile_name, {})
            account_id = _text_value(metadata.get("account_id"))
            codex_home = _text_value(metadata.get("codex_home"))
            default_source = _UsageSource(
                profile_name=_text_value(metadata.get("profile_name")) or profile_name,
                account_id=account_id,
                codex_home=codex_home,
            )
            if codex_home is not None:
                root = Path(codex_home).expanduser() / "sessions"
                for path in self._discover_jsonl(root):
                    sources.setdefault(path, default_source)

            # 注册表中的路径优先于当前 auth.json 的账号 ID。这样 profile
            # 换号后，旧会话的历史用量仍归到它原来记录的账号。
            for session in registry.list_sessions(active_only=False):
                path = _normalized_path(session.jsonl_path)
                if path is None:
                    continue
                session_source = _UsageSource(
                    profile_name=default_source.profile_name,
                    account_id=session.account_id or account_id,
                    codex_home=codex_home,
                    project=_text_value(session.cwd),
                )
                current_source = sources.get(path)
                if (
                    current_source is None
                    or session.account_id is not None
                    or (
                        current_source.project is None
                        and session_source.project is not None
                    )
                ):
                    sources[path] = session_source
        # 中文注释：同一 Codex session 可能在多个 CODEX_HOME 中留下前缀副本。
        # Grok 的 unified.jsonl 没有 Codex session UUID，必须在加入 Grok 前去重。
        sources = self._deduplicate_codex_sources(sources)
        for grok_home in self._grok_homes:
            log_path = grok_unified_log(grok_home)
            if not log_path.is_file():
                continue
            account = read_grok_account(grok_home)
            sources[log_path] = _UsageSource(
                profile_name=account.profile_name,
                account_id=account.account_id,
                codex_home=str(grok_home),
            )
        for kimi_home in self._kimi_homes:
            account = read_kimi_account(kimi_home)
            for path in self._discover_kimi_wires(kimi_home):
                sources[path] = _UsageSource(
                    profile_name=account.profile_name,
                    account_id=account.account_id,
                    codex_home=str(kimi_home),
                )
        for dsh_home in self._dsh_homes:
            account = read_dsh_account(dsh_home)
            for path in list_dsh_projcache_files(dsh_home):
                sources[path] = _UsageSource(
                    profile_name=account.profile_name,
                    account_id=account.account_id,
                    codex_home=str(dsh_home),
                )
        for claude_home in self._claude_homes:
            account = read_claude_account(claude_home)
            include_subagents = self._claude_include_subagents(claude_home)
            for path in list_claude_transcripts(
                claude_home,
                include_subagents=include_subagents,
            ):
                sources[path] = _UsageSource(
                    profile_name=account.profile_name,
                    account_id=account.account_id,
                    codex_home=str(claude_home),
                )
        return sources

    def _claude_include_subagents(self, claude_home: Path) -> bool:
        """判断是否要索引 subagents 目录，带 10 分钟缓存。"""

        now = time.monotonic()
        with self._lock:
            cached = self._claude_sidechain_cache.get(claude_home)
            if cached is not None and now - cached[0] < 600.0:
                return cached[1]
        include = resolve_sidechain_policy([claude_home]).get(claude_home, True)
        with self._lock:
            self._claude_sidechain_cache[claude_home] = (now, include)
        return include

    def _deduplicate_codex_sources(
        self,
        sources: Mapping[Path, _UsageSource],
    ) -> dict[Path, _UsageSource]:
        """按 Codex session UUID 保留最长副本，避免跨 profile 重复统计。"""

        canonical_by_session: dict[str, Path] = {}
        unique_sources: dict[Path, _UsageSource] = {}
        duplicate_paths: list[Path] = []
        for path, source in sorted(sources.items(), key=lambda item: str(item[0])):
            session_id = _codex_session_id(path)
            if session_id is None:
                unique_sources[path] = source
                continue
            previous_path = canonical_by_session.get(session_id)
            if previous_path is None:
                canonical_by_session[session_id] = path
                unique_sources[path] = source
                continue
            if _canonical_source_rank(path) > _canonical_source_rank(previous_path):
                unique_sources.pop(previous_path)
                duplicate_paths.append(previous_path)
                canonical_by_session[session_id] = path
                unique_sources[path] = source
            else:
                duplicate_paths.append(path)
        self._deduplicated_files = len(duplicate_paths)
        self._deduplicated_bytes = sum(
            _safe_file_size(path) for path in duplicate_paths
        )
        return unique_sources

    def _discover_jsonl(self, root: Path) -> tuple[Path, ...]:
        """发现一个 session 根目录下的 JSONL，并按时间缓存目录遍历。"""

        normalized_root = _normalized_path(str(root)) or root
        current_time = time.time()
        previous_time = self._discovered_at.get(normalized_root, 0)
        if current_time - previous_time < self.discovery_interval:
            return self._discovered.get(normalized_root, ())
        try:
            paths = tuple(
                sorted(
                    (
                        _normalized_path(str(path))
                        for path in normalized_root.rglob("*.jsonl")
                    ),
                    key=lambda item: str(item),
                )
            )
        except OSError:
            paths = ()
        normalized_paths = tuple(path for path in paths if path is not None)
        self._discovered[normalized_root] = normalized_paths
        self._discovered_at[normalized_root] = current_time
        return normalized_paths

    def _discover_kimi_wires(self, kimi_home: Path) -> tuple[Path, ...]:
        """发现一个 KIMI_CODE_HOME 下的 wire.jsonl，并按时间缓存遍历。"""

        root = kimi_home / "sessions"
        normalized_root = _normalized_path(str(root)) or root
        current_time = time.time()
        previous_time = self._discovered_at.get(normalized_root, 0)
        if current_time - previous_time < self.discovery_interval:
            return self._discovered.get(normalized_root, ())
        try:
            paths = tuple(
                sorted(
                    (
                        _normalized_path(str(path))
                        for path in normalized_root.rglob("wire.jsonl")
                    ),
                    key=lambda item: str(item),
                )
            )
        except OSError:
            paths = ()
        normalized_paths = tuple(path for path in paths if path is not None)
        self._discovered[normalized_root] = normalized_paths
        self._discovered_at[normalized_root] = current_time
        return normalized_paths

    def _remove_stale_cache(self, sources: Mapping[Path, _UsageSource]) -> None:
        """删除已经不在扫描范围内的缓存，避免长期运行无限增长。"""

        for path in tuple(self._cache):
            if path not in sources:
                del self._cache[path]
        if self._persistent is not None:
            self._persistent.prune(sources)

    def _index_progress(
        self,
        paths: tuple[Path, ...],
        read_bytes_this_refresh: int,
    ) -> dict[str, int | float | bool]:
        """汇总有界索引进度，供 Dashboard 明确标注统计完整性。"""

        total_bytes = 0
        indexed_bytes = 0
        pending_files = 0
        for path in paths:
            cached = self._cache.get(path)
            try:
                current_signature = _file_signature(path)
            except OSError:
                continue
            if current_signature is None:
                continue
            file_size = current_signature[2]
            total_bytes += file_size
            if cached is None:
                pending_files += 1
                continue
            if cached.signature == current_signature and cached.complete:
                indexed_bytes += file_size
                continue
            pending_files += 1
            # 中文注释：文件发生追加时保留已确认偏移，重写/截断则从零计进度。
            if (
                cached.signature[0] == current_signature[0]
                and file_size >= cached.next_offset
            ):
                indexed_bytes += min(cached.next_offset, file_size)
        percent = 100.0 if total_bytes == 0 else indexed_bytes * 100 / total_bytes
        return {
            "complete": pending_files == 0,
            "files": len(paths),
            "pending_files": pending_files,
            "indexed_bytes": indexed_bytes,
            "total_bytes": total_bytes,
            "percent": round(percent, 2),
            "read_bytes_this_refresh": read_bytes_this_refresh,
        }

    def _read_file(
        self,
        path: Path,
        maximum_bytes: int,
    ) -> _CachedFile | None:
        """按偏移量读取追加内容；只有替换或截断时才重新解析。"""

        if maximum_bytes <= 0:
            raise ValueError("maximum_bytes 必须大于 0")

        grok_home = self._grok_home_for_log(path)
        if grok_home is not None:
            return self._read_grok_log(path, grok_home, maximum_bytes)

        kimi_home = self._kimi_home_for_wire(path)
        if kimi_home is not None:
            return self._read_kimi_wire(path, kimi_home, maximum_bytes)

        dsh_home = dsh_projcache_home(path, self._dsh_homes)
        if dsh_home is not None:
            return self._read_dsh_projcache(path, maximum_bytes)

        if claude_home_for(path, self._claude_homes) is not None:
            return self._read_claude_transcript(path, maximum_bytes)

        try:
            stat_result = path.stat()
        except OSError:
            return None
        signature = (
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )
        cached = self._cache.get(path)
        if cached is None and self._persistent is not None:
            cached = self._persistent.load(path)
            if cached is not None:
                self._cache[path] = cached
        if cached is not None and cached.signature == signature and cached.complete:
            unchanged = replace(cached, last_read_bytes=0)
            self._cache[path] = unchanged
            return unchanged

        can_append = (
            cached is not None
            and cached.signature[0] == signature[0]
            and (not cached.complete or stat_result.st_size > cached.signature[2])
            and stat_result.st_size >= cached.next_offset
        )
        if can_append and cached is not None:
            parsed = _parse_usage_chunk(
                path,
                offset=cached.next_offset,
                state=cached.state,
                maximum_bytes=maximum_bytes,
            )
            cached_file = _CachedFile(
                signature=signature,
                next_offset=parsed.next_offset,
                total_deltas=cached.total_deltas + parsed.total_deltas,
                fallback_deltas=(cached.fallback_deltas + parsed.fallback_deltas),
                state=parsed.state,
                last_read_bytes=parsed.bytes_read,
                complete=parsed.reached_eof,
            )
            replace_deltas = False
        else:
            parsed = _parse_usage_chunk(
                path,
                offset=0,
                state=_UsageParseState(
                    previous_timestamp=stat_result.st_mtime,
                ),
                maximum_bytes=maximum_bytes,
            )
            cached_file = _CachedFile(
                signature=signature,
                next_offset=parsed.next_offset,
                total_deltas=parsed.total_deltas,
                fallback_deltas=parsed.fallback_deltas,
                state=parsed.state,
                last_read_bytes=parsed.bytes_read,
                complete=parsed.reached_eof,
            )
            replace_deltas = True
        self._cache[path] = cached_file
        if self._persistent is not None:
            self._persistent.save(
                path,
                cached_file,
                parsed.total_deltas,
                parsed.fallback_deltas,
                replace_deltas=replace_deltas,
            )
        return cached_file

    def _grok_home_for_log(self, path: Path) -> Path | None:
        """判断路径是否为某个 GROK_HOME 的统一用量日志。"""

        for home in self._grok_homes:
            if path == grok_unified_log(home):
                return home
        return None

    def _grok_session_index(
        self,
        grok_home: Path,
    ) -> dict[str, GrokSessionInfo]:
        """按发现间隔缓存 Grok session 的模型和项目。"""

        current_time = time.time()
        previous_time = self._grok_sessions_at.get(grok_home, 0)
        cached = self._grok_sessions.get(grok_home)
        if cached is not None and current_time - previous_time < self.discovery_interval:
            return cached
        index = load_session_index(grok_home)
        self._grok_sessions[grok_home] = index
        self._grok_sessions_at[grok_home] = current_time
        return index

    def _read_grok_log(
        self,
        path: Path,
        grok_home: Path,
        maximum_bytes: int,
    ) -> _CachedFile | None:
        """增量解析 Grok unified.jsonl 中的单次请求用量。"""

        try:
            stat_result = path.stat()
        except OSError:
            return None
        signature = (
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )
        cached = self._cache.get(path)
        if cached is None and self._persistent is not None:
            cached = self._persistent.load(path)
            if cached is not None:
                self._cache[path] = cached
        if cached is not None and cached.signature == signature and cached.complete:
            unchanged = replace(cached, last_read_bytes=0)
            self._cache[path] = unchanged
            return unchanged

        can_append = (
            cached is not None
            and cached.signature[0] == signature[0]
            and (not cached.complete or stat_result.st_size > cached.signature[2])
            and stat_result.st_size >= cached.next_offset
        )
        session_index = self._grok_session_index(grok_home)
        default_model = "grok-4.6"
        start_state = (
            cached.state
            if can_append and cached is not None
            else _UsageParseState(previous_timestamp=stat_result.st_mtime)
        )
        parsed = parse_grok_log_chunk(
            path,
            offset=cached.next_offset if can_append and cached is not None else 0,
            session_index=session_index,
            default_model=default_model,
            discarding_oversized_line=start_state.discarding_oversized_line,
            maximum_bytes=maximum_bytes,
        )
        new_deltas = tuple(
            UsageDelta(
                timestamp=event.timestamp,
                model=event.model,
                usage=TokenUsage(
                    input_tokens=event.input_tokens,
                    cached_input_tokens=event.cached_input_tokens,
                    output_tokens=event.output_tokens,
                    reasoning_output_tokens=event.reasoning_output_tokens,
                    total_tokens=event.total_tokens,
                ),
                billing_usage=TokenUsage(
                    input_tokens=event.input_tokens,
                    cached_input_tokens=event.cached_input_tokens,
                    output_tokens=event.output_tokens,
                    reasoning_output_tokens=event.reasoning_output_tokens,
                    total_tokens=event.total_tokens,
                ),
                project=event.project,
            )
            for event in parsed.events
        )
        if can_append and cached is not None:
            cached_file = _CachedFile(
                signature=signature,
                next_offset=parsed.next_offset,
                total_deltas=cached.total_deltas + new_deltas,
                fallback_deltas=cached.fallback_deltas,
                state=_UsageParseState(
                    has_total_usage=True,
                    current_model=default_model,
                    previous_timestamp=stat_result.st_mtime,
                    discarding_oversized_line=parsed.discarding_oversized_line,
                ),
                last_read_bytes=parsed.bytes_read,
                complete=parsed.reached_eof,
            )
            replace_deltas = False
            persist_deltas = new_deltas
        else:
            cached_file = _CachedFile(
                signature=signature,
                next_offset=parsed.next_offset,
                total_deltas=new_deltas,
                fallback_deltas=(),
                state=_UsageParseState(
                    has_total_usage=True,
                    current_model=default_model,
                    previous_timestamp=stat_result.st_mtime,
                    discarding_oversized_line=parsed.discarding_oversized_line,
                ),
                last_read_bytes=parsed.bytes_read,
                complete=parsed.reached_eof,
            )
            replace_deltas = True
            persist_deltas = new_deltas
        self._cache[path] = cached_file
        if self._persistent is not None:
            self._persistent.save(
                path,
                cached_file,
                persist_deltas,
                (),
                replace_deltas=replace_deltas,
            )
        return cached_file

    def _kimi_home_for_wire(self, path: Path) -> Path | None:
        """判断路径是否为某个 KIMI_CODE_HOME 的会话 wire 日志。"""

        if path.name != "wire.jsonl":
            return None
        for home in self._kimi_homes:
            try:
                if path.is_relative_to(home / "sessions"):
                    return home
            except ValueError:
                continue
        return None

    def _kimi_session_index(
        self,
        kimi_home: Path,
    ) -> dict[str, KimiSessionInfo]:
        """按发现间隔缓存 Kimi session 的工作目录。"""

        current_time = time.time()
        previous_time = self._kimi_sessions_at.get(kimi_home, 0)
        cached = self._kimi_sessions.get(kimi_home)
        if cached is not None and current_time - previous_time < self.discovery_interval:
            return cached
        index = load_kimi_session_index(kimi_home)
        self._kimi_sessions[kimi_home] = index
        self._kimi_sessions_at[kimi_home] = current_time
        return index

    def _read_kimi_wire(
        self,
        path: Path,
        kimi_home: Path,
        maximum_bytes: int,
    ) -> _CachedFile | None:
        """增量解析 Kimi wire.jsonl 中的单次请求用量。"""

        try:
            stat_result = path.stat()
        except OSError:
            return None
        signature = (
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )
        cached = self._cache.get(path)
        if cached is None and self._persistent is not None:
            cached = self._persistent.load(path)
            if cached is not None:
                self._cache[path] = cached
        if cached is not None and cached.signature == signature and cached.complete:
            unchanged = replace(cached, last_read_bytes=0)
            self._cache[path] = unchanged
            return unchanged

        can_append = (
            cached is not None
            and cached.signature[0] == signature[0]
            and (not cached.complete or stat_result.st_size > cached.signature[2])
            and stat_result.st_size >= cached.next_offset
        )
        session_index = self._kimi_session_index(kimi_home)
        default_model = "kimi-code/k3-256k"
        start_state = (
            cached.state
            if can_append and cached is not None
            else _UsageParseState(previous_timestamp=stat_result.st_mtime)
        )
        parsed = parse_kimi_wire_chunk(
            path,
            offset=cached.next_offset if can_append and cached is not None else 0,
            session_index=session_index,
            default_model=default_model,
            discarding_oversized_line=start_state.discarding_oversized_line,
            maximum_bytes=maximum_bytes,
        )
        new_deltas = tuple(
            UsageDelta(
                timestamp=event.timestamp,
                model=event.model,
                usage=TokenUsage(
                    input_tokens=event.input_tokens,
                    cached_input_tokens=event.cached_input_tokens,
                    cache_write_input_tokens=event.cache_write_input_tokens,
                    output_tokens=event.output_tokens,
                    total_tokens=event.total_tokens,
                ),
                billing_usage=TokenUsage(
                    input_tokens=event.input_tokens,
                    cached_input_tokens=event.cached_input_tokens,
                    cache_write_input_tokens=event.cache_write_input_tokens,
                    output_tokens=event.output_tokens,
                    total_tokens=event.total_tokens,
                ),
                project=event.project,
            )
            for event in parsed.events
        )
        if can_append and cached is not None:
            cached_file = _CachedFile(
                signature=signature,
                next_offset=parsed.next_offset,
                total_deltas=cached.total_deltas + new_deltas,
                fallback_deltas=cached.fallback_deltas,
                state=_UsageParseState(
                    has_total_usage=True,
                    current_model=default_model,
                    previous_timestamp=stat_result.st_mtime,
                    discarding_oversized_line=parsed.discarding_oversized_line,
                ),
                last_read_bytes=parsed.bytes_read,
                complete=parsed.reached_eof,
            )
            replace_deltas = False
            persist_deltas = new_deltas
        else:
            cached_file = _CachedFile(
                signature=signature,
                next_offset=parsed.next_offset,
                total_deltas=new_deltas,
                fallback_deltas=(),
                state=_UsageParseState(
                    has_total_usage=True,
                    current_model=default_model,
                    previous_timestamp=stat_result.st_mtime,
                    discarding_oversized_line=parsed.discarding_oversized_line,
                ),
                last_read_bytes=parsed.bytes_read,
                complete=parsed.reached_eof,
            )
            replace_deltas = True
            persist_deltas = new_deltas
        self._cache[path] = cached_file
        if self._persistent is not None:
            self._persistent.save(
                path,
                cached_file,
                persist_deltas,
                (),
                replace_deltas=replace_deltas,
            )
        return cached_file

    def _read_claude_transcript(
        self,
        path: Path,
        maximum_bytes: int,
    ) -> _CachedFile | None:
        """增量解析 Claude Code 会话 JSONL 中的单次请求用量。"""

        try:
            stat_result = path.stat()
        except OSError:
            return None
        signature = (
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )
        cached = self._cache.get(path)
        if cached is None and self._persistent is not None:
            cached = self._persistent.load(path)
            if cached is not None:
                self._cache[path] = cached
        if cached is not None and cached.signature == signature and cached.complete:
            unchanged = replace(cached, last_read_bytes=0)
            self._cache[path] = unchanged
            return unchanged

        can_append = (
            cached is not None
            and cached.signature[0] == signature[0]
            and (not cached.complete or stat_result.st_size > cached.signature[2])
            and stat_result.st_size >= cached.next_offset
        )
        start_state = (
            cached.state
            if can_append and cached is not None
            else _UsageParseState(previous_timestamp=stat_result.st_mtime)
        )
        parsed = parse_claude_chunk(
            path,
            offset=cached.next_offset if can_append and cached is not None else 0,
            seen_ids=start_state.recent_ids,
            discarding_oversized_line=start_state.discarding_oversized_line,
            maximum_bytes=maximum_bytes,
        )
        new_deltas = tuple(
            UsageDelta(
                timestamp=event.timestamp,
                model=event.model,
                usage=TokenUsage(
                    input_tokens=event.input_tokens,
                    cached_input_tokens=event.cached_input_tokens,
                    cache_write_input_tokens=event.cache_write_input_tokens,
                    output_tokens=event.output_tokens,
                    total_tokens=event.total_tokens,
                ),
                billing_usage=TokenUsage(
                    input_tokens=event.input_tokens,
                    cached_input_tokens=event.cached_input_tokens,
                    cache_write_input_tokens=event.cache_write_input_tokens,
                    output_tokens=event.output_tokens,
                    total_tokens=event.total_tokens,
                ),
                project=event.project,
            )
            for event in parsed.events
        )
        next_state = _UsageParseState(
            has_total_usage=True,
            current_model=(
                parsed.events[-1].model
                if parsed.events
                else start_state.current_model
            ),
            project=parsed.project or start_state.project,
            previous_timestamp=stat_result.st_mtime,
            discarding_oversized_line=parsed.discarding_oversized_line,
            recent_ids=parsed.seen_ids,
        )
        if can_append and cached is not None:
            cached_file = _CachedFile(
                signature=signature,
                next_offset=parsed.next_offset,
                total_deltas=cached.total_deltas + new_deltas,
                fallback_deltas=cached.fallback_deltas,
                state=next_state,
                last_read_bytes=parsed.bytes_read,
                complete=parsed.reached_eof,
            )
            replace_deltas = False
            persist_deltas = new_deltas
        else:
            cached_file = _CachedFile(
                signature=signature,
                next_offset=parsed.next_offset,
                total_deltas=new_deltas,
                fallback_deltas=(),
                state=next_state,
                last_read_bytes=parsed.bytes_read,
                complete=parsed.reached_eof,
            )
            replace_deltas = True
            persist_deltas = new_deltas
        self._cache[path] = cached_file
        if self._persistent is not None:
            self._persistent.save(
                path,
                cached_file,
                persist_deltas,
                (),
                replace_deltas=replace_deltas,
            )
        return cached_file

    def _read_dsh_projcache(
        self,
        path: Path,
        maximum_bytes: int,
    ) -> _CachedFile | None:
        """把 DeepSeek Harness projcache 的累计 token 转成增量。"""

        try:
            stat_result = path.stat()
        except OSError:
            return None
        if stat_result.st_size > maximum_bytes:
            return None
        signature = (
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )
        cached = self._cache.get(path)
        if cached is None and self._persistent is not None:
            cached = self._persistent.load(path)
            if cached is not None:
                self._cache[path] = cached
        if cached is not None and cached.signature == signature and cached.complete:
            unchanged = replace(cached, last_read_bytes=0)
            self._cache[path] = unchanged
            return unchanged
        snapshot = parse_dsh_projcache(path)
        if snapshot is None:
            empty = _CachedFile(
                signature=signature,
                next_offset=stat_result.st_size,
                total_deltas=cached.total_deltas if cached is not None else (),
                fallback_deltas=(),
                state=cached.state if cached is not None else _UsageParseState(),
                last_read_bytes=stat_result.st_size,
                complete=True,
            )
            self._cache[path] = empty
            return empty
        current = TokenUsage(
            input_tokens=snapshot.input_tokens,
            cached_input_tokens=snapshot.cached_input_tokens,
            cache_write_input_tokens=snapshot.cache_write_input_tokens,
            output_tokens=snapshot.output_tokens,
            total_tokens=snapshot.total_tokens,
        )
        previous = cached.state.total_baseline if cached is not None else None
        delta_usage = _token_usage_delta(current, previous)
        new_deltas: tuple[UsageDelta, ...] = ()
        if delta_usage is not None:
            new_deltas = (
                UsageDelta(
                    timestamp=snapshot.timestamp,
                    model=snapshot.model,
                    usage=delta_usage,
                    billing_usage=delta_usage,
                    project=snapshot.project,
                ),
            )
        if cached is not None and previous is not None:
            cached_file = _CachedFile(
                signature=signature,
                next_offset=stat_result.st_size,
                total_deltas=cached.total_deltas + new_deltas,
                fallback_deltas=cached.fallback_deltas,
                state=_UsageParseState(
                    total_baseline=current,
                    has_total_usage=True,
                    current_model=snapshot.model,
                    project=snapshot.project,
                    previous_timestamp=snapshot.timestamp,
                ),
                last_read_bytes=stat_result.st_size,
                complete=True,
            )
            replace_deltas = False
            persist_deltas = new_deltas
        else:
            cached_file = _CachedFile(
                signature=signature,
                next_offset=stat_result.st_size,
                total_deltas=new_deltas,
                fallback_deltas=(),
                state=_UsageParseState(
                    total_baseline=current,
                    has_total_usage=True,
                    current_model=snapshot.model,
                    project=snapshot.project,
                    previous_timestamp=snapshot.timestamp,
                ),
                last_read_bytes=stat_result.st_size,
                complete=True,
            )
            replace_deltas = True
            persist_deltas = new_deltas
        self._cache[path] = cached_file
        if self._persistent is not None:
            self._persistent.save(
                path,
                cached_file,
                persist_deltas,
                (),
                replace_deltas=replace_deltas,
            )
        return cached_file

    def _build_period(
        self,
        key: str,
        label: str,
        period_kind: str,
        now: float,
        sources: Mapping[Path, _UsageSource],
        file_deltas: Mapping[Path, tuple[UsageDelta, ...]],
        metadata_by_profile: Mapping[str, Mapping[str, str | None]],
    ) -> dict[str, Any]:
        """把文件增量过滤到一个时间范围并生成账号汇总。"""

        start = _period_start(period_kind, now)
        aggregates: dict[str, _UsageAggregate] = {}
        account_sources: dict[str, _UsageSource] = {}
        for profile_name, metadata in metadata_by_profile.items():
            profile = _text_value(metadata.get("profile_name")) or profile_name
            account_id = _text_value(metadata.get("account_id"))
            source = _UsageSource(
                profile_name=profile,
                account_id=account_id,
                codex_home=_text_value(metadata.get("codex_home")),
            )
            account_key = account_id or f"profile:{profile}"
            aggregate = aggregates.setdefault(account_key, _UsageAggregate())
            aggregate.profiles.add(profile)
            account_sources.setdefault(account_key, source)

        for path, deltas in file_deltas.items():
            source = sources[path]
            account_sources.setdefault(source.account_key, source)
            account = aggregates.setdefault(
                source.account_key,
                _UsageAggregate(),
            )
            for delta in deltas:
                if start <= delta.timestamp <= now:
                    account.add(source, delta)

        accounts = []
        for account_key, aggregate in sorted(
            aggregates.items(),
            key=lambda item: item[0],
        ):
            source = account_sources.get(account_key)
            if source is None:
                source = _source_for_account_key(account_key, sources)
            accounts.append(
                _aggregate_to_dict(
                    aggregate,
                    account_name=(source.account_name if source else account_key),
                    account_id=(source.account_id if source else None),
                    profiles=aggregate.profiles,
                )
            )
        return {
            "key": key,
            "label": label,
            "start_at": start,
            "end_at": now,
            "accounts": accounts,
        }

    def _build_daily(
        self,
        now: float,
        file_deltas: Mapping[Path, tuple[UsageDelta, ...]],
    ) -> list[dict[str, Any]]:
        """按本地自然日汇总近 30 天全部账号的 token 与 API 等价金额。"""

        local_now = datetime.fromtimestamp(now).astimezone()
        today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        days: list[dict[str, Any]] = []
        index: dict[str, dict[str, Any]] = {}
        for offset in range(self._DAILY_TREND_DAYS - 1, -1, -1):
            day_start = today_start - timedelta(days=offset)
            key = day_start.strftime("%Y-%m-%d")
            entry = {
                "date": key,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
                "has_unpriced": False,
            }
            days.append(entry)
            index[key] = entry
        first_start = (today_start - timedelta(days=self._DAILY_TREND_DAYS - 1)).timestamp()
        for deltas in file_deltas.values():
            for delta in deltas:
                if not first_start <= delta.timestamp <= now:
                    continue
                key = (
                    datetime.fromtimestamp(delta.timestamp)
                    .astimezone()
                    .strftime("%Y-%m-%d")
                )
                entry = index.get(key)
                if entry is None:
                    continue
                entry["total_tokens"] += delta.usage.total_tokens
                estimate = _estimate_usage(
                    delta.billing_usage or delta.usage,
                    delta.model,
                )
                cost = estimate.get("estimated_cost_usd")
                if cost is None:
                    entry["has_unpriced"] = True
                else:
                    entry["estimated_cost_usd"] += float(cost)
        for entry in days:
            entry["estimated_cost_usd"] = _round_number(entry["estimated_cost_usd"])
        return days


def _conversation_metrics(
    path: Path,
    cached_file: _CachedFile,
    source: _UsageSource | None,
    since: float | None = None,
) -> _ConversationMetrics | None:
    """把一个文件的 token 增量汇总成对话级指标；没有用量时跳过。"""

    deltas = cached_file.deltas
    if since is not None:
        # 中文注释：时间段分析只统计窗口内的增量，跨窗口的长对话按近期部分参与。
        deltas = tuple(delta for delta in deltas if delta.timestamp >= since)
    if not deltas:
        return None
    totals = TokenUsage()
    model_usage: dict[str, TokenUsage] = {}
    model_costs: dict[str, float | None] = {}
    cost = 0.0
    has_unpriced = False
    write_premium = 0.0
    hours: dict[int, int] = {}
    days: dict[str, int] = {}
    first_at = math.inf
    last_at = 0.0
    for delta in deltas:
        billing = delta.billing_usage or delta.usage
        totals = totals.add(delta.usage)
        previous = model_usage.get(delta.model, TokenUsage())
        model_usage[delta.model] = previous.add(delta.usage)
        estimate = _estimate_usage(billing, delta.model)
        value = estimate.get("estimated_cost_usd")
        if value is None:
            # 中文注释：未定价模型只丢掉自己的金额，保留对话其余可计价部分。
            has_unpriced = True
            model_costs[delta.model] = None
        else:
            cost += float(value)
            if delta.model not in model_costs:
                model_costs[delta.model] = 0.0
            if model_costs[delta.model] is not None:
                model_costs[delta.model] += float(value)
        pricing = _lookup_pricing(delta.model)
        if pricing is not None and pricing.input_usd is not None:
            write_premium += (
                billing.cache_write_input_tokens
                * float(pricing.input_usd)
                * (_CACHE_WRITE_MULTIPLIER - 1.0)
                / 1_000_000
            )
        moment = datetime.fromtimestamp(delta.timestamp).astimezone()
        hour = moment.hour
        hours[hour] = hours.get(hour, 0) + delta.usage.total_tokens
        day_key = moment.strftime("%Y-%m-%d")
        days[day_key] = days.get(day_key, 0) + delta.usage.total_tokens
        first_at = min(first_at, delta.timestamp)
        last_at = max(last_at, delta.timestamp)
    if totals.total_tokens <= 0:
        return None
    project = source.project if source is not None else None
    if project is None:
        project = cached_file.project
    return _ConversationMetrics(
        label=_codex_session_id(path) or kimi_wire_session_id(path) or path.stem,
        account=source.account_name if source is not None else "codex",
        project=project,
        turns=len(deltas),
        first_at=first_at,
        last_at=last_at,
        usage=totals,
        estimated_cost_usd=_round_number(cost),
        has_unpriced=has_unpriced,
        cache_write_premium_usd=write_premium,
        hour_tokens=tuple(sorted(hours.items())),
        day_tokens=tuple(sorted(days.items())),
        model_usage=tuple(
            (model, model_usage[model], model_costs.get(model))
            for model in sorted(model_usage)
        ),
    )


# 中文注释：习惯分析只统计 token 元数据，不读取提示词或工具输出等对话内容。
def _build_insights(
    conversations: Sequence[_ConversationMetrics],
    observed_at: float,
    window_days: int | None = None,
) -> dict[str, Any]:
    """汇总对话级指标，生成使用画像和可执行的省 token 建议。"""

    total_tokens = sum(item.usage.total_tokens for item in conversations)
    total_input = sum(item.usage.input_tokens for item in conversations)
    total_cached = sum(item.usage.cached_input_tokens for item in conversations)
    total_cache_write = sum(
        item.usage.cache_write_input_tokens for item in conversations
    )
    total_output = sum(item.usage.output_tokens for item in conversations)
    total_cost = _round_number(
        sum(item.estimated_cost_usd for item in conversations)
    )
    has_unpriced = any(item.has_unpriced for item in conversations)
    cache_hit_rate = (
        round(total_cached / total_input * 100, 1) if total_input > 0 else None
    )
    cache_write_share = (
        round(total_cache_write / total_input * 100, 1) if total_input > 0 else None
    )
    hour_buckets = [0] * 24
    for item in conversations:
        for hour, tokens in item.hour_tokens:
            if 0 <= hour < 24:
                hour_buckets[hour] += tokens
    model_usage: dict[str, TokenUsage] = {}
    model_costs: dict[str, float | None] = {}
    for item in conversations:
        for model, usage, model_cost in item.model_usage:
            previous = model_usage.get(model, TokenUsage())
            model_usage[model] = previous.add(usage)
            if model_cost is None:
                model_costs[model] = None
            elif model not in model_costs:
                model_costs[model] = model_cost
            elif model_costs[model] is not None:
                model_costs[model] += model_cost
    model_rows = [
        {
            "model": model,
            "total_tokens": model_usage[model].total_tokens,
            "estimated_cost_usd": (
                _round_number(model_costs[model])
                if model_costs.get(model) is not None
                else None
            ),
        }
        for model in sorted(
            model_usage,
            key=lambda name: (
                model_costs.get(name) is None,
                -(model_costs.get(name) or 0.0),
                name,
            ),
        )[:6]
    ]
    size_buckets: list[dict[str, Any]] = []
    for limit, label in ((2, "1–2 轮"), (10, "3–10 轮"), (30, "11–30 轮"), (None, "31 轮以上")):
        if limit is None:
            members = [item for item in conversations if item.turns > 30]
        else:
            lower = {2: 1, 10: 3, 30: 11}[limit]
            members = [
                item for item in conversations if lower <= item.turns <= limit
            ]
        member_costs = [
            item.estimated_cost_usd
            for item in members
            if item.estimated_cost_usd is not None
        ]
        size_buckets.append(
            {
                "label": label,
                "conversations": len(members),
                "estimated_cost_usd": (
                    _round_number(sum(member_costs)) if members else 0.0
                ),
                "has_unpriced": any(item.has_unpriced for item in members),
            }
        )
    top_conversations = [
        {
            "label": item.label,
            "account": item.account,
            "project": item.project,
            "turns": item.turns,
            "total_tokens": item.usage.total_tokens,
            "estimated_cost_usd": item.estimated_cost_usd,
            "has_unpriced": item.has_unpriced,
        }
        for item in sorted(
            conversations,
            key=lambda entry: (-entry.estimated_cost_usd, -entry.usage.total_tokens),
        )[:5]
    ]
    project_stats: dict[str, dict[str, int]] = {}
    day_totals: dict[str, int] = {}
    for item in conversations:
        key = item.project or _UNKNOWN_PROJECT
        stats = project_stats.setdefault(
            key,
            {"conversations": 0, "tokens": 0, "input": 0, "cached": 0},
        )
        stats["conversations"] += 1
        stats["tokens"] += item.usage.total_tokens
        stats["input"] += item.usage.input_tokens
        stats["cached"] += item.usage.cached_input_tokens
        for day, tokens in item.day_tokens:
            day_totals[day] = day_totals.get(day, 0) + tokens
    suggestions = _insight_suggestions(
        conversations,
        model_usage,
        model_costs,
        project_stats,
        total_input=total_input,
        total_output=total_output,
        total_tokens=total_tokens,
        total_cost=total_cost,
        cache_hit_rate=cache_hit_rate,
        cache_write_share=cache_write_share,
    )
    observations = _insight_observations(
        conversations,
        model_usage,
        model_costs,
        project_stats,
        hour_buckets,
        day_totals,
        total_cost=total_cost,
    )
    potential = _round_number(
        sum(
            float(suggestion["saving_usd"])
            for suggestion in suggestions
            if suggestion.get("saving_usd") is not None
        )
    )
    return {
        "ready": True,
        "observed_at": observed_at,
        "window_days": window_days,
        "insufficient": len(conversations) < 3,
        "conversation_count": len(conversations),
        "total_tokens": total_tokens,
        "estimated_cost_usd": total_cost,
        "has_unpriced": has_unpriced,
        "cache_hit_rate": cache_hit_rate,
        "cache_write_share": cache_write_share,
        "potential_savings_usd": potential,
        "hour_histogram": [
            {"hour": hour, "total_tokens": tokens}
            for hour, tokens in enumerate(hour_buckets)
        ],
        "models": model_rows,
        "size_buckets": size_buckets,
        "top_conversations": top_conversations,
        "observations": observations,
        "suggestions": suggestions,
    }


def _insight_observations(
    conversations: Sequence[_ConversationMetrics],
    model_usage: Mapping[str, TokenUsage],
    model_costs: Mapping[str, float | None],
    project_stats: Mapping[str, Mapping[str, int]],
    hour_buckets: Sequence[int],
    day_totals: Mapping[str, int],
    *,
    total_cost: float | None,
) -> list[str]:
    """从画像指标提炼始终展示的习惯观察，不依赖告警阈值。"""

    if not conversations:
        return []
    observations: list[str] = []
    total_turns = sum(item.turns for item in conversations)
    total_tokens = sum(item.usage.total_tokens for item in conversations)
    observations.append(
        f"平均每个对话 {total_turns / len(conversations):.1f} 轮、"
        f"{round(total_tokens / len(conversations)):,} tokens"
    )
    if any(hour_buckets):
        peak_hour = max(range(24), key=lambda hour: hour_buckets[hour])
        peak_share = hour_buckets[peak_hour] / max(1, sum(hour_buckets)) * 100
        observations.append(
            f"最活跃时段是 {peak_hour}:00–{peak_hour + 1}:00，"
            f"占全部 token 的 {peak_share:.1f}%"
        )
    priced_models = {
        model: cost
        for model, cost in model_costs.items()
        if cost is not None and cost > 0
    }
    if total_cost and priced_models:
        top_model = max(priced_models, key=lambda model: priced_models[model])
        observations.append(
            f"{top_model} 贡献了 {priced_models[top_model] / total_cost * 100:.1f}% "
            "的 API 等价成本"
        )
    elif model_usage:
        top_model = max(
            model_usage,
            key=lambda model: model_usage[model].total_tokens,
        )
        observations.append(
            f"{top_model} 是最常使用的模型，"
            f"占 {model_usage[top_model].total_tokens / max(1, total_tokens) * 100:.1f}% 的 token"
        )
    if project_stats:
        top_project = max(
            project_stats,
            key=lambda project: project_stats[project]["tokens"],
        )
        stats = project_stats[top_project]
        observations.append(
            f"对话最多的是项目 {top_project}（{stats['conversations']} 个对话，"
            f"{stats['tokens']:,} tokens）"
        )
    if day_totals:
        peak_day, peak_tokens = max(
            day_totals.items(),
            key=lambda item: item[1],
        )
        observations.append(
            f"用量最高的一天是 {peak_day}，共 {peak_tokens:,} tokens"
        )
        weekend_tokens = sum(
            tokens
            for day, tokens in day_totals.items()
            if datetime.strptime(day, "%Y-%m-%d").weekday() >= 5
        )
        total_day_tokens = sum(day_totals.values())
        if total_day_tokens > 0 and weekend_tokens > 0:
            observations.append(
                f"周末 token 占 {weekend_tokens / total_day_tokens * 100:.1f}%"
            )
    cache_savings = 0.0
    for model, usage in model_usage.items():
        pricing = _lookup_pricing(model)
        if (
            pricing is None
            or pricing.input_usd is None
            or pricing.cached_input_usd is None
        ):
            continue
        cache_savings += (
            usage.cached_input_tokens
            * max(0.0, float(pricing.input_usd) - float(pricing.cached_input_usd))
            / 1_000_000
        )
    if cache_savings > 0:
        observations.append(f"缓存命中已累计节省约 ${cache_savings:.4f}")
    return observations


def _insight_suggestions(
    conversations: Sequence[_ConversationMetrics],
    model_usage: Mapping[str, TokenUsage],
    model_costs: Mapping[str, float | None],
    project_stats: Mapping[str, Mapping[str, int]],
    *,
    total_input: int,
    total_output: int,
    total_tokens: int,
    total_cost: float,
    cache_hit_rate: float | None,
    cache_write_share: float | None,
) -> list[dict[str, Any]]:
    """按规则从画像指标生成省 token 建议，含可量化的节省估算。"""

    suggestions: list[dict[str, Any]] = []
    enough_data = len(conversations) >= 3
    if (
        enough_data
        and cache_hit_rate is not None
        and total_input >= 100_000
        and cache_hit_rate < 60
    ):
        saving = 0.0
        for model, usage in model_usage.items():
            pricing = _lookup_pricing(model)
            if pricing is None or pricing.input_usd is None:
                continue
            if pricing.cached_input_usd is None:
                continue
            extra_cached = max(
                0.0,
                0.6 * usage.input_tokens - usage.cached_input_tokens,
            )
            saving += (
                extra_cached
                * (float(pricing.input_usd) - float(pricing.cached_input_usd))
                / 1_000_000
            )
        suggestions.append(
            {
                "level": "warn" if cache_hit_rate < 40 else "tip",
                "title": f"整体缓存命中率仅 {cache_hit_rate:.1f}%",
                "detail": (
                    "同一项目里尽量在原会话中继续提问，避免频繁新建会话或清空"
                    "上下文；长前缀复用能显著降低输入费用。若命中率提升到 60%，"
                    "按当前用量规模约可节省这些费用。"
                ),
                "saving_usd": _round_number(saving) if saving > 0 else None,
            }
        )
    short = [item for item in conversations if item.turns <= 2]
    if enough_data and len(conversations) >= 5:
        short_share = len(short) / len(conversations)
        if short and short_share >= 0.25:
            short_premium = sum(item.cache_write_premium_usd for item in short)
            suggestions.append(
                {
                    "level": "warn" if short_share >= 0.5 else "tip",
                    "title": (
                        f"{len(short)} 个对话（{short_share * 100:.0f}%）不超过 2 轮"
                    ),
                    "detail": (
                        "极短对话无法复用前缀缓存，每个新会话都要重新写入系统"
                        "提示和项目上下文。把零碎问题合并到同一会话更省 token。"
                    ),
                    "saving_usd": (
                        _round_number(short_premium) if short_premium > 0 else None
                    ),
                }
            )
    total_premium = sum(item.cache_write_premium_usd for item in conversations)
    if (
        enough_data
        and cache_write_share is not None
        and total_input >= 100_000
        and cache_write_share > 15
    ):
        suggestions.append(
            {
                "level": "tip",
                "title": f"缓存写入占输入的 {cache_write_share:.1f}%",
                "detail": (
                    "缓存写入按输入价的 1.25 倍计费。频繁变动对话前缀（切换"
                    "项目、修改系统指令）会增加写入开销，保持稳定前缀更划算。"
                ),
                "saving_usd": (
                    _round_number(total_premium) if total_premium > 0 else None
                ),
            }
        )
    giant = [item for item in conversations if item.usage.total_tokens >= 200_000]
    if enough_data and giant:
        suggestions.append(
            {
                "level": "tip",
                "title": f"{len(giant)} 个对话累计超过 20 万 token",
                "detail": (
                    "上下文越长，每轮重计的输入越多。完成阶段性任务后让模型"
                    "总结要点，再开新会话继续，能避免旧上下文反复计费。"
                ),
                "saving_usd": None,
            }
        )
    marathon = [item for item in conversations if item.turns > 100]
    if enough_data and len(marathon) >= 3:
        suggestions.append(
            {
                "level": "tip",
                "title": f"{len(marathon)} 个对话超过 100 轮",
                "detail": (
                    "超长对话每一轮都按全量上下文重新计费。任务做到阶段收尾就"
                    "让模型总结、再开新会话，比无限续聊更省 token。"
                ),
                "saving_usd": None,
            }
        )
    if (
        enough_data
        and total_tokens >= 100_000
        and total_output / total_tokens > 0.2
    ):
        suggestions.append(
            {
                "level": "tip",
                "title": f"输出 token 占总量的 {total_output / total_tokens * 100:.1f}%",
                "detail": (
                    "输出单价通常是输入的数倍。可以要求模型「只给结论」"
                    "「限制篇幅」来压缩输出成本。"
                ),
                "saving_usd": None,
            }
        )
    if enough_data and total_cost > 0 and len(model_costs) >= 2:
        priced_models = {
            model: cost
            for model, cost in model_costs.items()
            if cost is not None and cost > 0
        }
        if priced_models:
            top_model = max(
                priced_models,
                key=lambda model: priced_models[model],
            )
            top_share = priced_models[top_model] / total_cost
            if top_share > 0.8:
                suggestions.append(
                    {
                        "level": "tip",
                        "title": (
                            f"{top_share * 100:.0f}% 的成本集中在 {top_model}"
                        ),
                        "detail": (
                            "模型越贵越要留给复杂任务。代码格式化、简单问答等"
                            "轻量任务切到更便宜的模型，能直接摊薄平均单价。"
                        ),
                        "saving_usd": None,
                    }
                )
    if (
        enough_data
        and total_cost > 0
        and len(conversations) >= 5
    ):
        top_cost = max(
            (item.estimated_cost_usd or 0.0) for item in conversations
        )
        if top_cost / total_cost > 0.4:
            suggestions.append(
                {
                    "level": "tip",
                    "title": (
                        f"最贵的一个对话占了 {top_cost / total_cost * 100:.0f}% 的成本"
                    ),
                    "detail": (
                        "单个对话成本占比过高通常意味着上下文过长或反复重写。"
                        "可以回顾这个对话，把可复用的结论沉淀成文档，"
                        "后续在新会话中引用。"
                    ),
                    "saving_usd": None,
                }
            )
    if (
        enough_data
        and cache_hit_rate is not None
        and cache_hit_rate >= 40
    ):
        worst_project = None
        worst_rate = 100.0
        for project, stats in project_stats.items():
            if stats["input"] < 50_000:
                continue
            rate = stats["cached"] / stats["input"] * 100
            if rate < worst_rate:
                worst_rate = rate
                worst_project = project
        if worst_project is not None and worst_rate < 30:
            suggestions.append(
                {
                    "level": "tip",
                    "title": (
                        f"项目 {worst_project} 的缓存命中率仅 {worst_rate:.1f}%"
                    ),
                    "detail": (
                        "明显低于整体水平。这个项目里可能每次都新开会话或"
                        "上下文前缀不稳定，集中排查这里的用法最划算。"
                    ),
                    "saving_usd": None,
                }
            )
    unpriced_tokens = sum(
        usage.total_tokens
        for model, usage in model_usage.items()
        if _lookup_pricing(model) is None
    )
    if total_tokens > 0 and unpriced_tokens / total_tokens > 0.2:
        suggestions.append(
            {
                "level": "info",
                "title": (
                    f"{unpriced_tokens / total_tokens * 100:.1f}% 的 token "
                    "来自未定价模型"
                ),
                "detail": (
                    "这些用量只统计了 token 数，金额合计未包含它们，"
                    "实际开销高于页面的估算值。"
                ),
                "saving_usd": None,
            }
        )
    if (
        enough_data
        and cache_hit_rate is not None
        and total_input >= 100_000
        and cache_hit_rate >= 90
    ):
        suggestions.append(
            {
                "level": "info",
                "title": f"缓存命中率 {cache_hit_rate:.1f}%，前缀复用做得很好",
                "detail": (
                    "保持当前用法：在同一项目里延续原会话，"
                    "避免频繁清空上下文或新开会话。"
                ),
                "saving_usd": None,
            }
        )
    return suggestions


def pricing_metadata() -> dict[str, str]:
    """返回估算来源和语义，供 API 与 Dashboard 明确展示。"""

    return {
        "api_source": _API_PRICING_SOURCE,
        "grok_api_source": _GROK_API_PRICING_SOURCE,
        "kimi_api_source": _KIMI_API_PRICING_SOURCE,
        "dsh_api_source": _DSH_API_PRICING_SOURCE,
        "claude_api_source": _CLAUDE_API_PRICING_SOURCE,
        "cost_kind": "api_equivalent_estimate",
        "credits_kind": "not_available_from_plus_jsonl",
        "note": (
            "Plus/OAuth 和 SuperGrok 实际订阅账单不会出现在本地日志；本页美元是按"
            "官方 API 单价换算的等价值，不是订阅扣款；Codex credits 无法从 JSONL"
            "反推；Grok 周额度百分比来自本地 billing 日志；Kimi Code 为订阅制，"
            "金额按 K3 公开 API 单价等价换算，不代表会员扣费；DeepSeek Harness "
            "用量来自本地 projcache 合计，金额按 DeepSeek 官方峰时 API 单价估算，"
            "不代表 Command Code 等转发账单；Claude Code 用量来自本地会话 JSONL，"
            "金额按 Anthropic 公开 API 单价换算，缓存写统一按 1.25× 输入价。"
            "未定价模型只展示 token，不计入 API 等价值。"
        ),
    }


def _aggregate_to_dict(
    aggregate: _UsageAggregate,
    account_name: str,
    account_id: str | None,
    profiles: set[str],
) -> dict[str, Any]:
    """把内部汇总转换成不包含原始 JSONL 的 API 数据。"""

    totals = _usage_totals_to_dict(aggregate)
    projects = [
        {
            "project": project,
            **_usage_totals_to_dict(project_aggregate),
        }
        for project, project_aggregate in sorted(aggregate.projects.items())
    ]
    return {
        "account": account_name,
        "account_id": account_id,
        "profiles": sorted(profiles),
        **totals,
        "projects": projects,
    }


def _usage_totals_to_dict(aggregate: _UsageAggregate) -> dict[str, Any]:
    """把一个账号或项目层级的 token、模型和费用转换成 API 数据。"""

    model_items: list[dict[str, Any]] = []
    total_credits = 0.0
    total_usd = 0.0
    total_savings = 0.0
    credits_priced_models = 0
    usd_priced_models = 0
    savings_priced_models = 0
    unpriced_models: list[str] = []
    for model, usage in sorted(aggregate.models.items()):
        model_cost = aggregate.model_costs.get(model)
        estimate = (
            model_cost.to_dict()
            if model_cost is not None
            else _estimate_usage(usage, model)
        )
        if estimate["estimated_credits"] is not None:
            total_credits += float(estimate["estimated_credits"])
            credits_priced_models += 1
        if estimate["estimated_cost_usd"] is not None:
            total_usd += float(estimate["estimated_cost_usd"])
            usd_priced_models += 1
        if estimate.get("cache_savings_usd") is not None:
            total_savings += float(estimate["cache_savings_usd"])
            savings_priced_models += 1
        # 中文注释：credits 对 Plus/OAuth 没有公开 token 价格，不把它当作未定价模型。
        if not estimate["api_pricing_known"]:
            unpriced_models.append(model)
        model_items.append(
            {
                "model": model,
                **usage.to_dict(),
                **estimate,
            }
        )

    has_models = bool(model_items)
    return {
        **aggregate.usage.to_dict(),
        "estimated_credits": (
            _round_number(total_credits) if credits_priced_models else None
        ),
        "estimated_cost_usd": (
            _round_number(total_usd) if usd_priced_models or not has_models else None
        ),
        "api_equivalent_cost_usd": (
            _round_number(total_usd) if usd_priced_models or not has_models else None
        ),
        "cache_savings_usd": (
            _round_number(total_savings)
            if savings_priced_models or not has_models
            else None
        ),
        "subscription_cost_usd": None,
        "pricing_complete": (
            not has_models or usd_priced_models == len(model_items)
        ),
        "credits_pricing_complete": False,
        "api_pricing_complete": (
            not has_models or usd_priced_models == len(model_items)
        ),
        "unpriced_models": unpriced_models,
        "models": model_items,
    }


def _estimate_usage(usage: TokenUsage, model: str) -> dict[str, Any]:
    """按公开单价估算一次模型用量。"""

    pricing = _lookup_pricing(model)
    if pricing is None:
        return _unknown_pricing_estimate()
    return _estimate_with_pricing(
        usage,
        pricing,
        _is_long_context(usage.input_tokens, pricing),
    )


def _unknown_pricing_estimate() -> dict[str, Any]:
    """返回没有公开单价时的估算占位。"""

    return {
        "estimated_credits": None,
        "estimated_cost_usd": None,
        "api_equivalent_cost_usd": None,
        "cache_savings_usd": None,
        "subscription_cost_usd": None,
        "credits_pricing_known": False,
        "api_pricing_known": False,
    }


def _estimate_with_pricing(
    usage: TokenUsage,
    pricing: ModelPricing,
    long_context: bool,
) -> dict[str, Any]:
    """用已知单价和显式的长上下文标记估算一次用量。

    检索聚合已经按长上下文标记分桶，因此这里必须接受调用方给定的标记，
    不能再用聚合后的 input_tokens 重新判断，否则会把多条短请求误判成长上下文。
    """

    # 中文注释：Codex 的 input_tokens 是输入总量，cached 和 cache-write 是其
    # 子项；分别拆分，避免把缓存输入按普通输入重复收费。
    uncached_input = (
        max(
            0,
            usage.input_tokens
            - usage.cached_input_tokens
            - usage.cache_write_input_tokens,
        )
    )
    cached_input = usage.cached_input_tokens
    cache_write_input = usage.cache_write_input_tokens
    output = usage.output_tokens
    estimates: dict[str, Any] = {
        "estimated_credits": _estimate_amount(
            uncached_input,
            cached_input,
            cache_write_input,
            output,
            pricing.input_credits,
            pricing.cached_input_credits,
            pricing.output_credits,
            long_context=long_context,
            input_multiplier=pricing.long_input_multiplier,
            cached_multiplier=pricing.long_cached_multiplier,
            output_multiplier=pricing.long_output_multiplier,
        ),
        "estimated_cost_usd": _estimate_amount(
            uncached_input,
            cached_input,
            cache_write_input,
            output,
            pricing.input_usd,
            pricing.cached_input_usd,
            pricing.output_usd,
            long_context=long_context,
            input_multiplier=pricing.long_input_multiplier,
            cached_multiplier=pricing.long_cached_multiplier,
            output_multiplier=pricing.long_output_multiplier,
        ),
    }
    estimates["api_equivalent_cost_usd"] = estimates["estimated_cost_usd"]
    estimates["cache_savings_usd"] = _cache_savings_usd(
        cached_input,
        pricing,
        long_context,
    )
    estimates["subscription_cost_usd"] = None
    estimates["credits_pricing_known"] = pricing.has_credits
    estimates["api_pricing_known"] = pricing.has_api_price
    return estimates


def _cache_savings_usd(
    cached_input_tokens: int,
    pricing: ModelPricing,
    long_context: bool,
) -> float | None:
    """估算缓存命中相对全价输入节省的美元；未定价时返回 None。"""

    if not pricing.has_api_price:
        return None
    input_price = float(pricing.input_usd or 0)
    cache_price = float(pricing.cached_input_usd or 0)
    if long_context:
        input_price *= pricing.long_input_multiplier
        cache_price *= pricing.long_cached_multiplier
    saved_per_token = max(0.0, input_price - cache_price) / 1_000_000
    return cached_input_tokens * saved_per_token


def _estimate_amount(
    uncached_input: int,
    cached_input: int,
    cache_write_input: int,
    output: int,
    input_price: float | None,
    cached_input_price: float | None,
    output_price: float | None,
    long_context: bool = False,
    input_multiplier: float = 2.0,
    cached_multiplier: float = 2.0,
    output_multiplier: float = 1.5,
) -> float | None:
    """用每百万 token 单价计算总额；单价不全时返回未知。"""

    if any(price is None for price in (input_price, cached_input_price, output_price)):
        return None
    assert input_price is not None
    assert cached_input_price is not None
    assert output_price is not None
    applied_input = input_multiplier if long_context else 1.0
    applied_cached = cached_multiplier if long_context else 1.0
    applied_output = output_multiplier if long_context else 1.0
    # 中文注释：单次事件不提前四舍五入，避免大量小事件累加出系统性误差；
    # 只在模型/账号最终输出时统一舍入。
    return (
        uncached_input * input_price * applied_input / 1_000_000
        + cached_input * cached_input_price * applied_cached / 1_000_000
        + (
            cache_write_input
            * input_price
            * _CACHE_WRITE_MULTIPLIER
            * applied_input
            / 1_000_000
        )
        + output * output_price * applied_output / 1_000_000
    )


def _is_long_context(input_tokens: int, pricing: ModelPricing) -> bool:
    """按模型官方阈值判断是否应按长上下文单价计费。"""

    if pricing.long_context_threshold is None:
        return input_tokens > _LONG_CONTEXT_INPUT_THRESHOLD
    if pricing.long_context_inclusive:
        return input_tokens >= pricing.long_context_threshold
    return input_tokens > pricing.long_context_threshold


def _token_usage_delta(
    current: TokenUsage,
    previous: TokenUsage | None,
) -> TokenUsage | None:
    """两个累计快照之间的非负增量；计数回退时重新发出当前合计。"""

    if previous is None:
        if current.total_tokens <= 0 and current.input_tokens <= 0:
            return None
        return current
    if (
        current.input_tokens < previous.input_tokens
        or current.output_tokens < previous.output_tokens
        or current.total_tokens < previous.total_tokens
    ):
        return current
    delta = TokenUsage(
        input_tokens=current.input_tokens - previous.input_tokens,
        cached_input_tokens=max(
            0, current.cached_input_tokens - previous.cached_input_tokens
        ),
        cache_write_input_tokens=max(
            0,
            current.cache_write_input_tokens - previous.cache_write_input_tokens,
        ),
        output_tokens=current.output_tokens - previous.output_tokens,
        reasoning_output_tokens=max(
            0,
            current.reasoning_output_tokens - previous.reasoning_output_tokens,
        ),
        total_tokens=max(0, current.total_tokens - previous.total_tokens),
    )
    if (
        delta.input_tokens
        + delta.cached_input_tokens
        + delta.output_tokens
        + delta.total_tokens
        <= 0
    ):
        return None
    return delta


def _lookup_pricing(model: str) -> ModelPricing | None:
    """按模型 ID 查找价格，允许官方模型带 snapshot 后缀。"""

    normalized = model.strip().lower()
    if not normalized or normalized == _UNKNOWN_MODEL.lower():
        return None
    if normalized == "gpt-5.6":
        normalized = "gpt-5.6-sol"
    exact = _MODEL_PRICING.get(normalized)
    if exact is not None:
        return exact
    if "/" in normalized:
        tail = normalized.rsplit("/", 1)[-1]
        exact = _MODEL_PRICING.get(tail)
        if exact is not None:
            return exact
        normalized = tail
    for model_id in sorted(_MODEL_PRICING, key=len, reverse=True):
        if normalized.startswith(f"{model_id}-"):
            return _MODEL_PRICING[model_id]
    return None


def _parse_usage_file(
    path: Path,
    fallback_timestamp: float,
) -> tuple[tuple[UsageDelta, ...], str | None]:
    """安全解析一个 JSONL，只提取累计 token 和模型字段。"""

    parsed = _parse_usage_chunk(
        path,
        offset=0,
        state=_UsageParseState(
            previous_timestamp=fallback_timestamp,
        ),
    )
    deltas = (
        parsed.total_deltas if parsed.state.has_total_usage else parsed.fallback_deltas
    )
    return deltas, parsed.state.project


def _parse_usage_chunk(
    path: Path,
    offset: int,
    state: _UsageParseState,
    maximum_bytes: int | None = None,
) -> _UsageParseResult:
    """从已确认的完整行偏移继续解析 JSONL 追加内容。"""

    if offset < 0:
        raise ValueError("offset 不能小于 0")
    if maximum_bytes is not None and maximum_bytes <= 0:
        raise ValueError("maximum_bytes 必须大于 0")
    total_deltas: list[UsageDelta] = []
    fallback_deltas: list[UsageDelta] = []
    total_baseline = state.total_baseline
    last_baseline = state.last_baseline
    has_total_usage = state.has_total_usage
    current_model = state.current_model
    project = state.project
    previous_timestamp = state.previous_timestamp
    next_offset = offset
    bytes_read = 0
    reached_eof = False
    discarding_oversized_line = state.discarding_oversized_line
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            content = (
                handle.read(maximum_bytes)
                if maximum_bytes is not None
                else handle.read()
            )
            bytes_read = len(content)
            reached_physical_eof = maximum_bytes is None or bytes_read < maximum_bytes
            content_offset = 0
            if discarding_oversized_line:
                first_newline = content.find(b"\n")
                if first_newline < 0:
                    next_offset = offset + bytes_read
                    reached_eof = reached_physical_eof
                    return _UsageParseResult(
                        next_offset=next_offset,
                        total_deltas=(),
                        fallback_deltas=(),
                        state=replace(
                            state,
                            discarding_oversized_line=True,
                        ),
                        bytes_read=bytes_read,
                        reached_eof=reached_eof,
                    )
                content_offset = first_newline + 1
                discarding_oversized_line = False

            remaining = content[content_offset:]
            last_newline = remaining.rfind(b"\n")
            if last_newline < 0:
                if reached_physical_eof:
                    # 中文注释：EOF 半行不计入偏移，文件追加完成后会重新读取。
                    next_offset = offset + content_offset
                    reached_eof = True
                else:
                    # 中文注释：单行超过预算时分段跳过，防止一次读入超大工具输出。
                    next_offset = offset + bytes_read
                    discarding_oversized_line = True
                complete_content = b""
            else:
                complete_content = remaining[: last_newline + 1]
                next_offset = offset + content_offset + len(complete_content)
                reached_eof = reached_physical_eof

            for raw_line in complete_content.splitlines(keepends=True):
                if not _should_parse_usage_line(raw_line):
                    continue
                line = raw_line.decode("utf-8", errors="replace")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, Mapping):
                    continue
                model = _extract_model(event)
                if model is not None:
                    current_model = model
                if project is None:
                    project = _extract_project(event)
                event_timestamp = _event_timestamp(event)
                if event_timestamp is not None:
                    previous_timestamp = event_timestamp
                total_usage = _extract_usage(event, "total_token_usage")
                last_usage = _extract_usage(event, "last_token_usage")
                if total_usage is not None:
                    has_total_usage = True
                    previous_total = total_baseline
                    decreased = (
                        previous_total is not None
                        and total_usage.decreased_from(previous_total)
                    )
                    total_delta = _next_delta(total_usage, previous_total)
                    unchanged = (
                        previous_total is not None
                        and not decreased
                        and total_delta.is_zero()
                    )
                    total_baseline = total_usage
                    if last_usage is not None:
                        last_baseline = last_usage
                    if unchanged:
                        continue
                    # 中文注释：正常情况下累计差值是完整用量；累计计数回退时，
                    # 当前 total 往往仍携带上下文，只能加入本次 last 用量。
                    emitted = (
                        last_usage
                        if decreased and last_usage is not None
                        else total_delta
                    )
                    if emitted.is_zero():
                        continue
                    total_deltas.append(
                        UsageDelta(
                            timestamp=previous_timestamp,
                            model=current_model,
                            usage=emitted,
                            billing_usage=_billing_usage(emitted, last_usage),
                        )
                    )
                elif last_usage is not None:
                    # 中文注释：last_token_usage 是一次请求而不是累计计数；相邻
                    # 完全相同通常是同一快照重复写入，只做相等去重。
                    repeated = (
                        last_baseline is not None
                        and last_usage.as_values() == last_baseline.as_values()
                    )
                    last_baseline = last_usage
                    if not repeated and not last_usage.is_zero():
                        fallback_deltas.append(
                            UsageDelta(
                                timestamp=previous_timestamp,
                                model=current_model,
                                usage=last_usage,
                                billing_usage=last_usage,
                            )
                        )
    except (OSError, UnicodeError):
        return _UsageParseResult(
            next_offset=offset,
            total_deltas=(),
            fallback_deltas=(),
            state=state,
            bytes_read=0,
            reached_eof=False,
        )
    return _UsageParseResult(
        next_offset=next_offset,
        total_deltas=tuple(total_deltas),
        fallback_deltas=tuple(fallback_deltas),
        state=_UsageParseState(
            total_baseline=total_baseline,
            last_baseline=last_baseline,
            has_total_usage=has_total_usage,
            current_model=current_model,
            project=project,
            previous_timestamp=previous_timestamp,
            discarding_oversized_line=discarding_oversized_line,
        ),
        bytes_read=bytes_read,
        reached_eof=reached_eof,
    )


def _next_delta(
    current: TokenUsage,
    previous: TokenUsage | None,
) -> TokenUsage:
    """把累计快照转换为增量；检测到计数回退时视为新一段累计。"""

    if previous is None or current.decreased_from(previous):
        return current
    return current.subtract(previous)


def _should_parse_usage_line(raw_line: bytes) -> bool:
    """只解析用量、模型和会话元数据行，跳过巨型工具输出。"""

    if any(hint in raw_line for hint in _USAGE_LINE_HINTS):
        return True
    if len(raw_line) > _MAX_MODEL_LINE_BYTES:
        return False
    return any(hint in raw_line for hint in _MODEL_LINE_HINTS)


def _billing_usage(
    total_delta: TokenUsage,
    last_usage: TokenUsage | None,
) -> TokenUsage:
    """选择用于 API 等价计价的单次请求用量。"""

    # 中文注释：只有 last 与累计增量一致时，才能确认它完整覆盖该增量。
    # 若中间快照缺失，直接使用较小的 last 会系统性漏算 token 和成本。
    if (
        last_usage is not None
        and last_usage.as_values() == total_delta.as_values()
    ):
        return last_usage
    return total_delta


def _codex_session_id(path: Path) -> str | None:
    """从 rollout 文件名提取 Codex session UUID。"""

    match = _CODEX_SESSION_ID_PATTERN.search(path.name)
    if match is None:
        return None
    return match.group("session_id").lower()


def session_id_from_path(path: Path) -> str:
    """返回会话展示名：Codex session UUID 或文件名（不含扩展名）。"""

    return _codex_session_id(path) or path.stem


def _canonical_source_rank(path: Path) -> tuple[int, int, str]:
    """按文件长度、修改时间和路径稳定选择最完整的 session 副本。"""

    try:
        stat_result = path.stat()
    except OSError:
        return (-1, -1, str(path))
    return (stat_result.st_size, stat_result.st_mtime_ns, str(path))


def _safe_file_size(path: Path) -> int:
    """安全读取文件长度，文件消失时按零处理。"""

    try:
        return path.stat().st_size
    except OSError:
        return 0


def _extract_usage(
    event: Mapping[str, Any],
    field_name: str,
) -> TokenUsage | None:
    """只在已知 Codex info 路径中读取 token 对象。"""

    for container in _event_containers(event):
        info = container.get("info")
        if isinstance(info, Mapping):
            value = info.get(field_name)
            if isinstance(value, Mapping):
                usage = TokenUsage.from_mapping(value)
                if usage is not None:
                    return usage
        value = container.get(field_name)
        if isinstance(value, Mapping):
            usage = TokenUsage.from_mapping(value)
            if usage is not None:
                return usage
    return None


def _extract_model(event: Mapping[str, Any]) -> str | None:
    """在 Codex 已知结构中读取当前模型，不遍历用户输入内容。"""

    paths = (
        ("model",),
        ("model_id",),
        ("model_name",),
        ("model_slug",),
        ("info", "model"),
        ("thread_settings", "model"),
        ("thread_settings", "collaboration_mode", "settings", "model"),
        ("collaboration_mode", "settings", "model"),
        ("turn_context", "model"),
        ("turn_context", "payload", "model"),
        ("world_state", "model"),
        ("world_state", "payload", "state", "model"),
        ("state", "model"),
    )
    for container in _event_containers(event):
        for path in paths:
            value: Any = container
            for key in path:
                if not isinstance(value, Mapping):
                    value = None
                    break
                value = value.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:160]
    return None


def _extract_project(event: Mapping[str, Any]) -> str | None:
    """从 session_meta 等已知结构读取工作目录，不遍历用户内容。"""

    paths = (
        ("cwd",),
        ("working_directory",),
        ("workingDirectory",),
        ("session_meta", "cwd"),
        ("session_meta", "working_directory"),
        ("session_meta", "workingDirectory"),
    )
    for container in _event_containers(event):
        for path in paths:
            value: Any = container
            for key in path:
                if not isinstance(value, Mapping):
                    value = None
                    break
                value = value.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:1_024]
    return None


def _event_containers(event: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """返回外层事件和有限深度的已知 payload 容器。"""

    containers: list[Mapping[str, Any]] = [event]
    first_payload = event.get("payload")
    if isinstance(first_payload, Mapping):
        containers.append(first_payload)
        second_payload = first_payload.get("payload")
        if isinstance(second_payload, Mapping):
            containers.append(second_payload)
    return tuple(containers)


def _event_timestamp(event: Mapping[str, Any]) -> float | None:
    """解析 JSONL 顶层或已知 payload 中的事件时间。"""

    values: list[Any] = [event.get("timestamp")]
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        values.append(payload.get("timestamp"))
    for value in values:
        timestamp = _timestamp(value)
        if timestamp is not None:
            return timestamp
    return None


def _timestamp(value: Any) -> float | None:
    """解析 Unix 秒、毫秒或 ISO 8601 时间。"""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return number / 1000 if number > 10_000_000_000 else number
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            number = float(stripped)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        return number / 1000 if number > 10_000_000_000 else number
    return None


def _period_start(period_kind: str, now: float) -> float:
    """计算本地时区的今天或滚动时间窗口起点。"""

    if period_kind == "calendar_day":
        local_now = datetime.fromtimestamp(now).astimezone()
        local_start = local_now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return local_start.timestamp()
    days = {
        "seven_days": 7,
        "thirty_days": 30,
        "three_hundred_sixty_five_days": 365,
    }
    try:
        duration = timedelta(days=days[period_kind])
    except KeyError as error:
        raise ValueError(f"未知用量时间窗口: {period_kind}") from error
    return now - duration.total_seconds()


def _source_for_account_key(
    account_key: str,
    sources: Mapping[Path, _UsageSource],
) -> _UsageSource | None:
    """从文件来源中找到账号的展示信息。"""

    for source in sources.values():
        if source.account_key == account_key:
            return source
    if account_key.startswith("profile:"):
        return _UsageSource(
            profile_name=account_key.removeprefix("profile:"),
            account_id=None,
            codex_home=None,
        )
    return None


def _normalized_path(value: str | None) -> Path | None:
    """把 JSONL 路径规范化为稳定的绝对 Path。"""

    if value is None or not value.strip():
        return None
    path = Path(value).expanduser()
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()


def _file_signature(path: Path) -> tuple[int, int, int] | None:
    """读取文件 inode、修改时间和大小，不读取文件内容。"""

    try:
        stat_result = path.stat()
    except OSError:
        return None
    return stat_result.st_ino, stat_result.st_mtime_ns, stat_result.st_size


def _text_value(value: object) -> str | None:
    """读取非空字符串元数据。"""

    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _token_number(value: Any) -> int | None:
    """把 token 字段转换为有限的非负整数。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number)


def _camel_case(field_name: str) -> str:
    """返回 token 字段的兼容 camelCase 写法。"""

    head, *tail = field_name.split("_")
    return head + "".join(item.capitalize() for item in tail)


def _round_number(value: float) -> float:
    """避免 API 中出现浮点计算噪声。"""

    return round(value, 8)
