"""使用习惯分析：只统计 token 元数据，不读取提示词或工具输出等对话内容。"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..local_time import to_local
from ..kimi import (
    kimi_wire_session_id,
)
from .aggregates import (
    _ConversationMetrics,
)
from .pricing import (
    _CACHE_WRITE_MULTIPLIER,
    _estimate_usage,
    _lookup_pricing,
)
from .records import (
    TokenUsage,
    _CachedFile,
    _UNKNOWN_PROJECT,
    _UsageSource,
    _codex_session_id,
    _round_number,
)


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
        estimate = _estimate_usage(billing, delta.model, delta.timestamp)
        value = estimate.get("estimated_cost_usd")
        if value is None:
            # 中文注释：未定价模型只丢掉自己的金额，保留对话其余可计价部分。
            has_unpriced = True
            model_costs[delta.model] = None
        else:
            cost += float(value)
            if delta.model not in model_costs:
                model_costs[delta.model] = 0.0
            current = model_costs[delta.model]
            if current is not None:
                model_costs[delta.model] = current + float(value)
        pricing = _lookup_pricing(delta.model, delta.timestamp)
        if pricing is not None and pricing.input_usd is not None:
            write_premium += (
                billing.cache_write_input_tokens
                * float(pricing.input_usd)
                * (_CACHE_WRITE_MULTIPLIER - 1.0)
                / 1_000_000
            )
        moment = to_local(delta.timestamp)
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
    window_kind: str = "all",
    window_since: float | None = None,
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
            elif (current := model_costs[model]) is not None:
                model_costs[model] = current + model_cost
    model_rows = [
        {
            "model": model,
            "total_tokens": model_usage[model].total_tokens,
            "estimated_cost_usd": (
                _round_number(cost)
                if (cost := model_costs.get(model)) is not None
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
        "window_kind": window_kind,
        "window_since": window_since,
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
