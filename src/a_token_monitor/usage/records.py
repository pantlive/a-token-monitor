"""用量记录的基础数据类型、会话阈值与持久化编解码。"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, cast



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


_CODEX_SESSION_ID_PATTERN = re.compile(
    r"(?P<session_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)


# 中文注释：会话过长提醒的默认阈值，与习惯分析的「超长对话」口径保持一致。
DEFAULT_SESSION_TURN_WARN = 100


DEFAULT_SESSION_CONTEXT_WARN_TOKENS = 200_000


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
    """一个 JSONL 文件所属的 profile、账号身份和产品。"""

    profile_name: str
    account_id: str | None
    codex_home: str | None
    project: str | None = None
    product: str | None = None

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
    account_key: str | None = None
    account_name: str | None = None
    account_id: str | None = None
    product: str | None = None

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


def _delta_from_row(row: tuple[object, ...]) -> UsageDelta | None:
    """从 SQLite 行恢复一个 token 增量。"""

    timestamp = row[1]
    model = row[2]
    if not isinstance(timestamp, (int, float)) or not isinstance(model, str):
        return None
    usage = _usage_from_object(json.loads(cast(str, row[3])))
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


def _codex_session_id(path: Path) -> str | None:
    """从 rollout 文件名提取 Codex session UUID。"""

    match = _CODEX_SESSION_ID_PATTERN.search(path.name)
    if match is None:
        return None
    return match.group("session_id").lower()


def session_id_from_path(path: Path) -> str:
    """返回会话展示名：Codex session UUID 或文件名（不含扩展名）。"""

    return _codex_session_id(path) or path.stem


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
