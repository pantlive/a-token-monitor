"""按账号、模型、项目和时间桶累计用量与成本的中间结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from ..agents import product_label
from .periods import (
    _local_day,
)
from .pricing import (
    _estimate_usage,
)
from .records import (
    TokenUsage,
    UsageDelta,
    _UNKNOWN_PROJECT,
    _UsageSource,
    _round_number,
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
    products: set[str] = field(default_factory=set)
    models: dict[str, TokenUsage] = field(default_factory=dict)
    model_costs: dict[str, _ModelCost] = field(default_factory=dict)
    projects: dict[str, "_UsageAggregate"] = field(default_factory=dict)

    def add(
        self,
        source: _UsageSource,
        delta: UsageDelta,
        estimate: Mapping[str, Any] | None = None,
    ) -> None:
        """合并一次 JSONL 增量。"""

        self._add_totals(source, delta, estimate)
        project = delta.project or source.project or _UNKNOWN_PROJECT
        project_aggregate = self.projects.setdefault(
            project,
            _UsageAggregate(),
        )
        project_aggregate._add_totals(source, delta, estimate)

    def _add_totals(
        self,
        source: _UsageSource,
        delta: UsageDelta,
        estimate: Mapping[str, Any] | None = None,
    ) -> None:
        """只更新当前层级的 token 和模型统计，不递归创建项目。"""

        self.usage = self.usage.add(delta.usage)
        self.profiles.add(source.profile_name)
        if source.product:
            self.products.add(source.product)
        previous = self.models.get(delta.model, TokenUsage())
        self.models[delta.model] = previous.add(delta.usage)
        cost = self.model_costs.setdefault(delta.model, _ModelCost())
        cost.add(
            estimate
            if estimate is not None
            else _estimate_usage(delta.billing_usage or delta.usage, delta.model)
        )


@dataclass
class _RollupBin:
    """同小时、自然日、模型和项目的增量汇总，保留边界请求以精确裁剪窗口。"""

    model: str
    project: str | None
    first_at: float = float("inf")
    last_at: float = float("-inf")
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost: _ModelCost = field(default_factory=_ModelCost)
    items: list[tuple[UsageDelta, Mapping[str, Any]]] = field(default_factory=list)

    def add(self, delta: UsageDelta) -> None:
        """仅对新请求估价；长上下文和缓存计价保持请求级口径。"""

        estimate = _estimate_usage(delta.billing_usage or delta.usage, delta.model)
        self.items.append((delta, estimate))
        self.first_at = min(self.first_at, delta.timestamp)
        self.last_at = max(self.last_at, delta.timestamp)
        self.usage = self.usage.add(delta.usage)
        self.cost.add(estimate)

    def entries(
        self, start: float, end: float
    ) -> Iterator[tuple[UsageDelta, Mapping[str, Any]]]:
        """完整桶直接合并，只有跨越时间窗口边界的桶逐条过滤。"""

        if self.last_at < start or self.first_at > end:
            return
        if start <= self.first_at and self.last_at <= end:
            yield (
                UsageDelta(self.last_at, self.model, self.usage, project=self.project),
                {
                    "estimated_credits": self.cost.estimated_credits,
                    "estimated_cost_usd": self.cost.estimated_cost_usd,
                    "cache_savings_usd": self.cost.cache_savings_usd,
                },
            )
        else:
            yield from (
                (delta, estimate)
                for delta, estimate in self.items
                if start <= delta.timestamp <= end
            )


class _FileRollup:
    """逐文件维护增量分桶；截断、替换或累计/兜底切换时重建。"""

    def __init__(self) -> None:
        self.sequence: tuple[UsageDelta, ...] = ()
        self.bins: dict[tuple[object, ...], _RollupBin] = {}

    def update(self, deltas: tuple[UsageDelta, ...]) -> None:
        """复用不可变增量对象，追加时只处理新尾部。"""

        if deltas is self.sequence:
            return
        previous_count = len(self.sequence)
        can_append = previous_count <= len(deltas) and (
            previous_count == 0 or deltas[previous_count - 1] is self.sequence[-1]
        )
        if not can_append:
            self.bins.clear()
            previous_count = 0
        for delta in deltas[previous_count:]:
            key = (
                int(delta.timestamp // 3600),
                _local_day(delta.timestamp),
                delta.model,
                delta.project,
            )
            bucket = self.bins.setdefault(key, _RollupBin(delta.model, delta.project))
            bucket.add(delta)
        self.sequence = deltas

    def entries(
        self, start: float, end: float
    ) -> Iterator[tuple[UsageDelta, Mapping[str, Any]]]:
        """产出指定时间范围内的预计算汇总。"""

        for bucket in self.bins.values():
            yield from bucket.entries(start, end)


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
        # 中文注释：账号口径沿用「真实账号 ID 优先」，这里只标注产品来源，
        # 供面板显示 Codex / Grok / Kimi 等标签，不参与归组。
        "products": sorted({product_label(item) for item in aggregate.products}),
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
