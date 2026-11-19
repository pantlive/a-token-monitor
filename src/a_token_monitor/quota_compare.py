"""把 Claude Code、Codex、Grok 的 5 小时和周窗口放在同一张对比表里。

当前窗口同时给出性价比（每 1% 额度的 API 等价美元、用满窗口的美元、
周窗口的回本倍数）和消耗速度。历史来自周期表，5 小时看最近 12 个周期，
周窗口看最近 8 周。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Mapping, Sequence

from .quota import QuotaWindow, project_quota_exhaustion
from .quota_cycles import (
    PERIOD_FIVE_HOURS,
    PERIOD_WEEK,
    QuotaCycleStore,
    is_design_window,
    normalize_period,
    period_api_name,
    sum_account_usage,
)
from .subscription_pricing import (
    COMPARE_PRODUCTS,
    PRODUCT_LABELS,
    load_overrides,
    plan_label,
    resolve_price,
)


_HISTORY_LIMIT = {PERIOD_FIVE_HOURS: 12, PERIOD_WEEK: 8}
_DEFAULT_MINUTES = {PERIOD_FIVE_HOURS: 300.0, PERIOD_WEEK: 10080.0}
_PRODUCT_ORDER = {"claude": 0, "codex": 1, "grok": 2}
_SAMPLE_FLOOR = 5.0
_SHARED_PRODUCTS = frozenset({"claude", "grok"})

NOTE_NO_WINDOW = "无此窗口"
NOTE_NO_SNAPSHOT = "暂无快照"
NOTE_SAMPLE = "样本不足"
NOTE_UNKNOWN_PLAN = "未知套餐，请填写月费"
NOTE_SHARED = "网页版和 App 也会消耗额度"


def build_quota_compare(
    period: str,
    *,
    accounts: Sequence[Mapping[str, object]],
    cycle_store: QuotaCycleStore | None = None,
    usage_index: Path | None = None,
    state_dir: Path | None = None,
    now: float | None = None,
) -> dict[str, object]:
    """组装一个周期的当前窗口表和历史趋势。``period`` 只接受 ``5h`` 或 ``week``。"""

    normalized = normalize_period(period)
    if normalized is None:
        raise ValueError(f"未知额度周期: {period}")
    moment = time.time() if now is None else float(now)
    overrides = load_overrides(state_dir)
    prepared = _prepare_accounts(accounts)
    rows = [
        _current_row(
            account,
            normalized,
            overrides.get((account["product"], account["account_key"])),
            usage_index=usage_index,
        )
        for account in prepared
    ]
    rows.sort(
        key=lambda item: (
            _PRODUCT_ORDER.get(str(item["product"]), 9),
            str(item["display_name"]).lower(),
            str(item["account_key"]),
        )
    )
    names = {
        (str(item["product"]), str(item["account_key"])): str(item["display_name"])
        for item in rows
    }
    history: list[dict[str, object]] = []
    if cycle_store is not None:
        limit = _HISTORY_LIMIT[normalized]
        for cycle in cycle_store.recent(normalized, limit):
            if cycle.product not in COMPARE_PRODUCTS:
                continue
            if normalized == PERIOD_FIVE_HOURS and cycle.product == "grok":
                continue
            history.append(_history_row(cycle, names))
    return {
        "period": period_api_name(normalized),
        "period_key": normalized,
        "updated_at": moment,
        "history_limit": _HISTORY_LIMIT[normalized],
        "rows": rows,
        "history": history,
    }


def _prepare_accounts(
    accounts: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    prepared: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for account in accounts:
        if not isinstance(account, Mapping):
            continue
        product = account.get("product")
        if not isinstance(product, str) or not product.strip():
            product = "codex"
        else:
            product = product.strip()
        if product not in COMPARE_PRODUCTS:
            continue
        account_key = _account_key(account)
        if not account_key or (product, account_key) in seen:
            continue
        seen.add((product, account_key))
        display = account.get("display_name") or account.get("name") or account_key
        quota = account.get("quota")
        detected = account.get("plan_type")
        if not isinstance(detected, str) and isinstance(quota, Mapping):
            detected = quota.get("plan_type")
        prepared.append(
            {
                "product": product,
                "account_key": account_key,
                "display_name": str(display),
                "detected_plan": detected.strip() if isinstance(detected, str) else None,
                "quota": quota if isinstance(quota, Mapping) else None,
            }
        )
    return prepared


def _account_key(account: Mapping[str, object]) -> str:
    explicit = account.get("account_key")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    account_id = account.get("account_id")
    if isinstance(account_id, str) and account_id.strip():
        return account_id.strip()
    profiles = account.get("profiles")
    if isinstance(profiles, list):
        for profile in profiles:
            if not isinstance(profile, Mapping):
                continue
            name = profile.get("name")
            if isinstance(name, str) and name.strip():
                return f"profile:{name.strip()}"
    name = account.get("name")
    if isinstance(name, str) and name.strip():
        return f"profile:{name.strip()}"
    return ""


def _current_row(
    account: Mapping[str, object],
    period: str,
    override: Mapping[str, object] | None,
    *,
    usage_index: Path | None,
) -> dict[str, object]:
    product = str(account["product"])
    detected = account.get("detected_plan")
    detected_plan = detected if isinstance(detected, str) else None
    plan, monthly, unknown = resolve_price(product, detected_plan, override)
    base: dict[str, object] = {
        "product": product,
        "product_label": PRODUCT_LABELS[product],
        "account_key": account["account_key"],
        "display_name": account["display_name"],
        "plan": plan,
        "plan_label": plan_label(product, plan or detected_plan),
        "monthly_usd": monthly,
        "unknown_plan": unknown,
        "no_window": False,
        "missing_snapshot": False,
        "sample_limited": False,
        "shared_quota": False,
        "notes": [],
        "used_percent": None,
        "elapsed_percent": None,
        "pace_ratio": None,
        "projection_status": None,
        "projected_exhausted_at": None,
        "tokens": None,
        "cost_usd": None,
        "usd_per_percent": None,
        "full_window_usd": None,
        "payback_multiple": None,
        "cycle_start": None,
        "resets_at": None,
        "observed_at": None,
    }
    if period == PERIOD_FIVE_HOURS and product == "grok":
        base["no_window"] = True
        base["notes"] = [NOTE_NO_WINDOW]
        return base
    quota = account.get("quota")
    window = _best_window(quota, period) if isinstance(quota, Mapping) else None
    if window is None:
        base["missing_snapshot"] = True
        base["notes"] = [NOTE_NO_SNAPSHOT]
        if unknown:
            notes = list(base["notes"])
            notes.append(NOTE_UNKNOWN_PLAN)
            base["notes"] = notes
        return base
    observed_at = _number(quota.get("observed_at")) if isinstance(quota, Mapping) else None
    used = _number(window.get("used_percent"))
    minutes = _number(window.get("window_minutes")) or _DEFAULT_MINUTES[period]
    resets_at = _number(window.get("resets_at"))
    base["used_percent"] = used
    base["resets_at"] = resets_at
    base["observed_at"] = observed_at
    if resets_at is not None and minutes > 0:
        base["cycle_start"] = resets_at - minutes * 60.0
    elapsed_percent = _elapsed_percent(observed_at, resets_at, minutes)
    base["elapsed_percent"] = elapsed_percent
    if used is not None and elapsed_percent is not None and elapsed_percent > 0:
        base["pace_ratio"] = used / elapsed_percent
    projection = project_quota_exhaustion(
        QuotaWindow(
            limit_id=str(window.get("limit_id") or product),
            name=str(window.get("name") or period),
            used_percent=used,
            window_minutes=minutes,
            resets_at=resets_at,
            reached_type=_text(window.get("reached_type")),
        ),
        observed_at if observed_at is not None else 0.0,
    )
    base["projection_status"] = projection.status
    base["projected_exhausted_at"] = projection.projected_exhausted_at
    if (
        usage_index is not None
        and observed_at is not None
        and resets_at is not None
        and minutes > 0
    ):
        ready, tokens, cost = sum_account_usage(
            usage_index,
            product=product,
            account_key=str(account["account_key"]),
            start=resets_at - minutes * 60.0,
            end=observed_at,
        )
        if ready:
            base["tokens"] = tokens
            base["cost_usd"] = cost
    sample_limited = used is None or used < _SAMPLE_FLOOR
    base["sample_limited"] = sample_limited
    cost = base["cost_usd"]
    if (
        not sample_limited
        and isinstance(used, float)
        and used > 0
        and isinstance(cost, (int, float))
    ):
        per_percent = float(cost) / used
        base["usd_per_percent"] = per_percent
        base["full_window_usd"] = per_percent * 100.0
        if (
            period == PERIOD_WEEK
            and isinstance(monthly, (int, float))
            and monthly > 0
        ):
            weekly_fee = float(monthly) * 12.0 / 52.0
            base["payback_multiple"] = float(cost) / weekly_fee
    notes: list[str] = []
    if sample_limited:
        notes.append(NOTE_SAMPLE)
    if unknown:
        notes.append(NOTE_UNKNOWN_PLAN)
    if product in _SHARED_PRODUCTS:
        base["shared_quota"] = True
        notes.append(NOTE_SHARED)
    base["notes"] = notes
    return base


def _best_window(
    quota: Mapping[str, object],
    period: str,
) -> Mapping[str, object] | None:
    best: Mapping[str, object] | None = None
    best_used = -1.0
    windows = quota.get("windows")
    if not isinstance(windows, list):
        return None
    for window in windows:
        if not isinstance(window, Mapping):
            continue
        if _is_design(window):
            continue
        window_period = normalize_period(_text(window.get("period")))
        if window_period != period:
            continue
        used = _number(window.get("used_percent"))
        score = used if used is not None else -1.0
        if best is None or score > best_used:
            best = window
            best_used = score
    return best


def _is_design(window: Mapping[str, object]) -> bool:
    return is_design_window(
        QuotaWindow(
            limit_id=str(window.get("limit_id") or ""),
            name=str(window.get("name") or ""),
            used_percent=None,
            window_minutes=None,
            resets_at=None,
        )
    )


def _elapsed_percent(
    observed_at: float | None,
    resets_at: float | None,
    minutes: float,
) -> float | None:
    if observed_at is None or resets_at is None or minutes <= 0:
        return None
    duration = minutes * 60.0
    elapsed = duration - (resets_at - observed_at)
    if elapsed < 0:
        elapsed = 0.0
    return elapsed / duration * 100.0


def _history_row(
    cycle: object,
    names: Mapping[tuple[str, str], str],
) -> dict[str, object]:
    product = str(getattr(cycle, "product"))
    account_key = str(getattr(cycle, "account_key"))
    return {
        "product": product,
        "product_label": PRODUCT_LABELS.get(product, product),
        "account_key": account_key,
        "display_name": names.get((product, account_key), account_key),
        "cycle_start": getattr(cycle, "cycle_start"),
        "resets_at": getattr(cycle, "resets_at"),
        "max_used_percent": getattr(cycle, "max_used_percent"),
        "tokens": getattr(cycle, "tokens"),
        "cost_usd": getattr(cycle, "cost_usd"),
        "incomplete": bool(getattr(cycle, "incomplete")),
        "source": getattr(cycle, "source"),
        "settled": getattr(cycle, "settled"),
        "sample_count": getattr(cycle, "sample_count"),
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
