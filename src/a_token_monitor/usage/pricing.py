"""官方 API 等价单价表与成本估算。

credits 永远不从 JSONL 猜测；没有官方单价的模型按未定价处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .records import (
    TokenUsage,
    _UNKNOWN_MODEL,
)


_LONG_CONTEXT_INPUT_THRESHOLD = 272_000


_CACHE_WRITE_MULTIPLIER = 1.25


_API_PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"


_GROK_API_PRICING_SOURCE = "https://docs.x.ai/docs/models"


_KIMI_API_PRICING_SOURCE = "https://platform.kimi.com/docs/pricing/chat"


_DSH_API_PRICING_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing"


_CLAUDE_API_PRICING_SOURCE = "https://docs.claude.com/en/docs/about-claude/pricing"


_MIMO_API_PRICING_SOURCE = "https://mimo.mi.com/"


_ZAI_API_PRICING_SOURCE = "https://docs.z.ai/guides/overview/pricing"


_STEPFUN_API_PRICING_SOURCE = "https://platform.stepfun.com/docs/zh/guides/pricing/details"


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


def _claude_pricing(input_usd: float, cached_input_usd: float, output_usd: float) -> ModelPricing:
    """Claude 模型的 API 等价单价：整个上下文窗口内不分档加价。"""

    return ModelPricing(
        input_usd=input_usd,
        cached_input_usd=cached_input_usd,
        output_usd=output_usd,
        long_input_multiplier=1.0,
        long_cached_multiplier=1.0,
        long_output_multiplier=1.0,
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
    # 官方文档：$2 / $0.2 / $10，>272K 输入按 2x 输入与缓存、1.5x 输出计费。
    "gpt-6-sol": ModelPricing(
        input_usd=2,
        cached_input_usd=0.2,
        output_usd=10,
        long_context_threshold=272_000,
    ),
    # 官方文档：$2 / $0.1 / $10，长上下文加价规则同上（缓存价减半）。
    "gpt-6.1-sol": ModelPricing(
        input_usd=2,
        cached_input_usd=0.1,
        output_usd=10,
        long_context_threshold=272_000,
    ),
    # 官方文档：$0.1 / $0.01 / $0.5，长上下文加价规则同上。
    "gpt-6-luna": ModelPricing(
        input_usd=0.1,
        cached_input_usd=0.01,
        output_usd=0.5,
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
    # 官方文档：gpt-5.3-codex $1.75 / $0.175 / $14，按 GPT 统一的 272K 长上下文规则。
    "gpt-5.3-codex": ModelPricing(
        input_usd=1.75,
        cached_input_usd=0.175,
        output_usd=14,
        long_context_threshold=272_000,
    ),
    # 中文注释：Codex 的自动代码审查走 codex-auto-review，底层是 gpt-5.3-codex，
    # 收费期间按它的单价；2026-10-08 起免费，见 _PRICE_CHANGES。
    "codex-auto-review": ModelPricing(
        input_usd=1.75,
        cached_input_usd=0.175,
        output_usd=14,
        long_context_threshold=272_000,
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
    # 小米 MiMo 开放平台：国际站 $0.14 / $0.0028（缓存命中）/ $0.28 每百万 token
    # （国内站为 ¥1 / ¥0.02 / ¥2）。官方没有长上下文加价，因此显式把三个乘数设为
    # 1.0，避免套用默认的 272K 加价规则。缓存写入官方未单独定价，沿用全局 1.25x 规则。
    "mimo-v2.6-flash": ModelPricing(
        input_usd=0.14,
        cached_input_usd=0.0028,
        output_usd=0.28,
        long_context_threshold=_LONG_CONTEXT_INPUT_THRESHOLD,
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
    # 中文注释：Z.ai（智谱）官方美元价目，缓存命中价按官方标价；官方未公布
    # 长上下文分段计费，三个倍率固定为 1。
    "glm-5.3": ModelPricing(
        input_usd=1.4,
        cached_input_usd=0.26,
        output_usd=4.4,
        long_context_threshold=_LONG_CONTEXT_INPUT_THRESHOLD,
        long_input_multiplier=1.0,
        long_cached_multiplier=1.0,
        long_output_multiplier=1.0,
    ),
    "glm-5.3-flash": ModelPricing(
        input_usd=0.15,
        cached_input_usd=0.03,
        output_usd=0.5,
        long_context_threshold=_LONG_CONTEXT_INPUT_THRESHOLD,
        long_input_multiplier=1.0,
        long_cached_multiplier=1.0,
        long_output_multiplier=1.0,
    ),
    "glm-5.3-flashx": ModelPricing(
        input_usd=0.37,
        cached_input_usd=0.075,
        output_usd=1.25,
        long_context_threshold=_LONG_CONTEXT_INPUT_THRESHOLD,
        long_input_multiplier=1.0,
        long_cached_multiplier=1.0,
        long_output_multiplier=1.0,
    ),
    # 中文注释：阶跃星辰官方价目为人民币（输入 ¥7、缓存命中 ¥0.35、输出 ¥20 每百万
    # token），与表里其它人民币价目一样按 7.0 折算成美元；官方未公布长上下文分段。
    "step-5-preview": ModelPricing(
        input_usd=1.0,
        cached_input_usd=0.05,
        output_usd=2.86,
        long_context_threshold=_LONG_CONTEXT_INPUT_THRESHOLD,
        long_input_multiplier=1.0,
        long_cached_multiplier=1.0,
        long_output_multiplier=1.0,
    ),
    # 中文注释：Claude 单价取自 Anthropic 官方价格页（输入 / 缓存读取 / 输出，每百万
    # token）。Claude 4.6 及之后的模型在整个 1M 上下文内按标准价计费，Haiku 5.5 例外；
    # 4.5 及更早的多数模型上下文只有 200K，本来就到不了长上下文档。所以默认不加价，
    # 避免落到全局 272K 的 GPT 长上下文规则。缓存写入按输入价的 1.25 倍统一估算。
    "claude-fable-5-1": _claude_pricing(10, 0.25, 50),
    "claude-mythos-5-1": _claude_pricing(10, 0.25, 50),
    "claude-fable-5": _claude_pricing(10, 1, 50),
    "claude-mythos-5": _claude_pricing(10, 1, 50),
    "claude-opus-5-5": _claude_pricing(4, 0.2, 20),
    "claude-opus-5": _claude_pricing(5, 0.5, 25),
    "claude-opus-4-8": _claude_pricing(5, 0.5, 25),
    "claude-opus-4-7": _claude_pricing(5, 0.5, 25),
    "claude-opus-4-6": _claude_pricing(5, 0.5, 25),
    "claude-opus-4-5": _claude_pricing(5, 0.5, 25),
    "claude-opus-4-1": _claude_pricing(15, 1.5, 75),
    "claude-opus-4": _claude_pricing(15, 1.5, 75),
    "claude-sonnet-5-5": _claude_pricing(2, 0.1, 10),
    "claude-sonnet-5": _claude_pricing(2, 0.2, 10),
    "claude-sonnet-4-6": _claude_pricing(3, 0.3, 15),
    # 中文注释：Sonnet 4 / 4.5 的 1M 上下文（beta）对超过 200K 输入的请求按
    # 输入 2×、输出 1.5× 计价，缓存读写随输入同倍。
    "claude-sonnet-4-5": ModelPricing(
        input_usd=3,
        cached_input_usd=0.3,
        output_usd=15,
        long_context_threshold=200_000,
    ),
    "claude-sonnet-4": ModelPricing(
        input_usd=3,
        cached_input_usd=0.3,
        output_usd=15,
        long_context_threshold=200_000,
    ),
    # 中文注释：Haiku 5.5 按提示长度分档：提示（含缓存读写）超过 100K token 时，
    # 输入、缓存与输出单价都变为 5 倍（$0.50 / $0.05 / $2.50）。
    "claude-haiku-5-5": ModelPricing(
        input_usd=0.1,
        cached_input_usd=0.01,
        output_usd=0.5,
        long_context_threshold=100_000,
        long_input_multiplier=5.0,
        long_cached_multiplier=5.0,
        long_output_multiplier=5.0,
    ),
    "claude-haiku-4-5": _claude_pricing(1, 0.1, 5),
    "claude-3-7-sonnet": _claude_pricing(3, 0.3, 15),
    "claude-3-5-sonnet": _claude_pricing(3, 0.3, 15),
    "claude-3-5-haiku": _claude_pricing(0.8, 0.08, 4),
}

# 中文注释：免费期按 0 计价（仍算「有单价」，金额合计里计入 $0，而不是当成未定价）。
_FREE_PRICING = ModelPricing(input_usd=0, cached_input_usd=0, output_usd=0)

# 中文注释：价格变更表：模型 ID -> ((生效时间戳, 新价格), ...)，按生效时间升序。
# 生效之前沿用 _MODEL_PRICING 里的价格；每条用量按它发生的时间取价，历史金额不随
# 调价改变。时间戳一律按 UTC 写。
_PRICE_CHANGES: dict[str, tuple[tuple[float, ModelPricing], ...]] = {
    # 2026-10-08 00:00 UTC 起 codex-auto-review 免费。
    "codex-auto-review": ((1_791_417_600.0, _FREE_PRICING),),
}


def pricing_metadata() -> dict[str, str]:
    """返回估算来源和语义，供 API 与 Dashboard 明确展示。"""

    return {
        "api_source": _API_PRICING_SOURCE,
        "grok_api_source": _GROK_API_PRICING_SOURCE,
        "kimi_api_source": _KIMI_API_PRICING_SOURCE,
        "dsh_api_source": _DSH_API_PRICING_SOURCE,
        "claude_api_source": _CLAUDE_API_PRICING_SOURCE,
        "mimo_api_source": _MIMO_API_PRICING_SOURCE,
        "zai_api_source": _ZAI_API_PRICING_SOURCE,
        "stepfun_api_source": _STEPFUN_API_PRICING_SOURCE,
        "cost_kind": "api_equivalent_estimate",
        "credits_kind": "not_available_from_plus_jsonl",
        "note": (
            "Plus/OAuth 和 SuperGrok 实际订阅账单不会出现在本地日志；本页美元是按"
            "官方 API 单价换算的等价值，不是订阅扣款；Codex credits 无法从 JSONL"
            "反推；Grok 周额度百分比来自本地 billing 日志；Kimi Code 为订阅制，"
            "金额按 K3 公开 API 单价等价换算，不代表会员扣费；DeepSeek Harness "
            "用量来自本地 projcache 合计，金额按 DeepSeek 官方峰时 API 单价估算，"
            "不代表 Command Code 等转发账单；Claude Code 用量来自本地会话 JSONL，"
            "金额按 Anthropic 公开 API 单价换算，缓存写统一按 1.25× 输入价；"
            "小米 MiMo 按开放平台国际站单价估算（官方未公布长上下文加价）；"
            "智谱 GLM 按 Z.ai 官方美元价目估算；阶跃星辰 Step 按开放平台人民币价目"
            "按 7.0 折算（官方未公布长上下文分段）。"
            "未定价模型只展示 token，不计入 API 等价值。"
        ),
    }


def _estimate_usage(
    usage: TokenUsage,
    model: str,
    timestamp: float | None = None,
) -> dict[str, Any]:
    """按公开单价估算一次模型用量；给出发生时间时按当时的价格计算。"""

    pricing = _lookup_pricing(model, timestamp)
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


def _pricing_key(model: str) -> str | None:
    """把模型 ID 解析成价目表里的键，允许官方模型带 snapshot 后缀。"""

    normalized = model.strip().lower()
    if not normalized or normalized == _UNKNOWN_MODEL.lower():
        return None
    if normalized == "gpt-5.6":
        normalized = "gpt-5.6-sol"
    if normalized == "gpt-6.1":
        normalized = "gpt-6.1-sol"
    if normalized in _MODEL_PRICING:
        return normalized
    if "/" in normalized:
        tail = normalized.rsplit("/", 1)[-1]
        if tail in _MODEL_PRICING:
            return tail
        normalized = tail
    for model_id in sorted(_MODEL_PRICING, key=len, reverse=True):
        if normalized.startswith(f"{model_id}-"):
            return model_id
    return None


def _lookup_pricing(model: str, timestamp: float | None = None) -> ModelPricing | None:
    """按模型 ID 查找价格；给出用量发生时间时按 _PRICE_CHANGES 取当时的价格。"""

    key = _pricing_key(model)
    if key is None:
        return None
    period = price_period(model, timestamp)
    if period == 0:
        return _MODEL_PRICING[key]
    return _PRICE_CHANGES[key][period - 1][1]


def price_period(model: str, timestamp: float | None) -> int:
    """用量所处的价格时段：0 为 _MODEL_PRICING 的原价，n 为第 n 次调价之后。

    没有时间戳或模型没有调价记录时都是 0；SQL 检索按它分组，保证一个聚合桶里
    不会混入调价前后的用量。
    """

    if timestamp is None:
        return 0
    key = _pricing_key(model)
    changes = _PRICE_CHANGES.get(key or "", ())
    period = 0
    for index, (effective_at, _pricing) in enumerate(changes, start=1):
        if float(timestamp) >= effective_at:
            period = index
    return period
