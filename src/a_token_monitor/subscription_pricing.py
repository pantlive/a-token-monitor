"""订阅月费：内置默认价，按账号的覆盖值写进状态目录的 settings.json。

未知套餐（含 Codex 的 probusiness）以及 Claude 只读到 ``max`` 时不猜价格，
由设置页让用户填写。写入时保留文件里已有的其它键，避免冲掉保留期配置。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Mapping

from .retention import SETTINGS_FILENAME, RetentionSettings


SETTINGS_KEY = "subscription_prices"
MAX_MONTHLY_USD = 100_000.0

# 中文注释：公开月费（美元）。prolite 按用户确认的 5x 档，100 美元/月。
DEFAULT_MONTHLY_USD: dict[str, dict[str, float]] = {
    "codex": {"plus": 20.0, "prolite": 100.0, "pro": 200.0},
    "claude": {"pro": 20.0, "max5x": 100.0, "max20x": 200.0},
    "grok": {"supergrok": 30.0, "heavy": 300.0},
}

PLAN_LABELS: dict[tuple[str, str], str] = {
    ("codex", "plus"): "Plus",
    ("codex", "prolite"): "Pro Lite",
    ("codex", "pro"): "Pro",
    ("claude", "pro"): "Pro",
    ("claude", "max5x"): "Max 5x",
    ("claude", "max20x"): "Max 20x",
    ("grok", "supergrok"): "SuperGrok",
    ("grok", "heavy"): "SuperGrok Heavy",
}

PRODUCT_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "grok": "Grok",
}

COMPARE_PRODUCTS = frozenset(DEFAULT_MONTHLY_USD)

_PLAN_ALIASES: dict[str, dict[str, str]] = {
    "codex": {
        "plus": "plus",
        "pro": "pro",
        "prolite": "prolite",
        "pro_lite": "prolite",
        "pro-lite": "prolite",
    },
    "claude": {
        "pro": "pro",
        "max5x": "max5x",
        "max_5x": "max5x",
        "max-5x": "max5x",
        "5x": "max5x",
        "max20x": "max20x",
        "max_20x": "max20x",
        "max-20x": "max20x",
        "20x": "max20x",
    },
    "grok": {
        "supergrok": "supergrok",
        "super_grok": "supergrok",
        "super-grok": "supergrok",
        "heavy": "heavy",
        "supergrokheavy": "heavy",
        "super_grok_heavy": "heavy",
        "supergrok_heavy": "heavy",
    },
}


class SubscriptionPriceError(ValueError):
    """订阅月费配置不合法。"""


def canonical_plan(product: str, plan: str | None) -> str | None:
    """把套餐名收成价表里的键。认不出时返回 None，不拿相近档位顶上。

    Claude 凭据里常见的 ``max`` 分不出 5x 和 20x，必须留空让用户选。
    """

    if product not in COMPARE_PRODUCTS or plan is None:
        return None
    key = str(plan).strip().lower().replace(" ", "")
    if not key:
        return None
    return _PLAN_ALIASES[product].get(key)


def plan_label(product: str, plan: str | None) -> str | None:
    """返回套餐的展示名；未知套餐原样返回，方便设置页提示。"""

    canonical = canonical_plan(product, plan)
    if canonical is None:
        text = (plan or "").strip()
        return text or None
    return PLAN_LABELS.get((product, canonical), canonical)


def default_monthly_usd(product: str, plan: str | None) -> float | None:
    """按默认价表查月费。未知套餐返回 None。"""

    canonical = canonical_plan(product, plan)
    if canonical is None:
        return None
    return DEFAULT_MONTHLY_USD[product][canonical]


def _check_monthly(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SubscriptionPriceError("月费必须是大于 0 的数字")
    amount = float(value)
    if not math.isfinite(amount) or amount <= 0 or amount > MAX_MONTHLY_USD:
        raise SubscriptionPriceError("月费必须是大于 0 的数字")
    return amount


def _settings_path(state_dir: Path) -> Path:
    return state_dir.expanduser() / SETTINGS_FILENAME


def _read_document(path: Path) -> dict[str, object]:
    try:
        raw_payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SubscriptionPriceError("无法读取订阅月费配置") from error
    if not isinstance(raw_payload, dict):
        raise SubscriptionPriceError("无法读取订阅月费配置")
    return dict(raw_payload)


def _write_document(path: Path, document: Mapping[str, object]) -> None:
    """原子写入并限制为当前用户可读写，保留调用方已经放进 document 的键。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    temporary_path = path.with_suffix(".json.tmp")
    temporary_path.write_text(f"{payload}\n", encoding="utf-8")
    temporary_path.chmod(0o600)
    temporary_path.replace(path)
    path.chmod(0o600)


def _account_entries(document: Mapping[str, object]) -> list[dict[str, object]]:
    raw_prices = document.get(SETTINGS_KEY)
    if not isinstance(raw_prices, Mapping):
        return []
    raw_accounts = raw_prices.get("accounts")
    if not isinstance(raw_accounts, list):
        return []
    entries: list[dict[str, object]] = []
    for item in raw_accounts:
        if not isinstance(item, Mapping):
            continue
        product = item.get("product")
        account_key = item.get("account_key")
        if not isinstance(product, str) or product not in COMPARE_PRODUCTS:
            continue
        if not isinstance(account_key, str) or not account_key.strip():
            continue
        plan_value = item.get("plan")
        plan = plan_value.strip() if isinstance(plan_value, str) else None
        monthly_value = item.get("monthly_usd")
        monthly: float | None
        try:
            monthly = None if monthly_value is None else _check_monthly(monthly_value)
        except SubscriptionPriceError:
            monthly = None
        if plan == "":
            plan = None
        entries.append(
            {
                "product": product,
                "account_key": account_key.strip(),
                "plan": canonical_plan(product, plan) if plan else None,
                "monthly_usd": monthly,
            }
        )
    return entries


def load_overrides(state_dir: Path | None) -> dict[tuple[str, str], dict[str, object]]:
    """读取按账号覆盖的套餐和月费。文件不存在时视为没有覆盖。"""

    if state_dir is None:
        return {}
    document = _read_document(_settings_path(state_dir))
    found: dict[tuple[str, str], dict[str, object]] = {}
    for entry in _account_entries(document):
        found[(str(entry["product"]), str(entry["account_key"]))] = entry
    return found


def resolve_price(
    product: str,
    detected_plan: str | None,
    override: Mapping[str, object] | None = None,
) -> tuple[str | None, float | None, bool]:
    """返回 ``(生效套餐, 月费, 是否未知套餐)``。

    覆盖月费优先；其次用覆盖套餐查默认价；再退回检测到的套餐。
    有月费时不再提示未知套餐。
    """

    override_plan = None
    override_monthly: float | None = None
    if override is not None:
        plan_value = override.get("plan")
        if isinstance(plan_value, str) and plan_value.strip():
            override_plan = canonical_plan(product, plan_value)
        monthly_value = override.get("monthly_usd")
        if monthly_value is not None:
            override_monthly = float(monthly_value)
    chosen = override_plan or canonical_plan(product, detected_plan)
    if override_monthly is not None:
        return chosen, override_monthly, False
    monthly = default_monthly_usd(product, chosen)
    unknown = monthly is None
    return chosen, monthly, unknown


def plans_catalog() -> dict[str, list[dict[str, object]]]:
    """设置页用的默认价表。"""

    catalog: dict[str, list[dict[str, object]]] = {}
    for product, plans in DEFAULT_MONTHLY_USD.items():
        catalog[product] = [
            {
                "id": plan,
                "label": PLAN_LABELS[(product, plan)],
                "monthly_usd": amount,
            }
            for plan, amount in plans.items()
        ]
    return catalog


def save_override(
    state_dir: Path,
    *,
    product: str,
    account_key: str,
    plan: str | None,
    monthly_usd: float | None,
) -> None:
    """写入或清除一个账号的覆盖。两者都空时删掉这一行。"""

    if product not in COMPARE_PRODUCTS:
        raise SubscriptionPriceError("未知产品")
    key = account_key.strip()
    if not key:
        raise SubscriptionPriceError("账号不能为空")
    canonical = None
    if plan is not None and str(plan).strip():
        canonical = canonical_plan(product, plan)
        if canonical is None:
            raise SubscriptionPriceError("套餐名无法识别")
    monthly = None if monthly_usd is None else _check_monthly(monthly_usd)
    path = _settings_path(state_dir)
    document = _read_document(path)
    if "schema_version" not in document:
        document["schema_version"] = RetentionSettings.SCHEMA_VERSION
    if "overrides" not in document:
        document["overrides"] = {}
    entries = [
        entry
        for entry in _account_entries(document)
        if (entry["product"], entry["account_key"]) != (product, key)
    ]
    if canonical is not None or monthly is not None:
        entries.append(
            {
                "product": product,
                "account_key": key,
                "plan": canonical,
                "monthly_usd": monthly,
            }
        )
    entries.sort(key=lambda item: (str(item["product"]), str(item["account_key"])))
    document[SETTINGS_KEY] = {"accounts": entries}
    _write_document(path, document)


def snapshot(
    state_dir: Path | None,
    accounts: list[dict[str, object]],
    *,
    writable: bool,
) -> dict[str, object]:
    """设置页要的价表、账号行和是否允许写入。"""

    overrides = load_overrides(state_dir) if state_dir is not None else {}
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for account in accounts:
        product = str(account.get("product") or "")
        account_key = str(account.get("account_key") or "")
        if product not in COMPARE_PRODUCTS or not account_key:
            continue
        identity = (product, account_key)
        if identity in seen:
            continue
        seen.add(identity)
        detected = account.get("detected_plan")
        detected_plan = detected.strip() if isinstance(detected, str) else None
        override = overrides.get(identity)
        plan, monthly, unknown = resolve_price(product, detected_plan, override)
        rows.append(
            {
                "product": product,
                "product_label": PRODUCT_LABELS[product],
                "account_key": account_key,
                "display_name": account.get("display_name") or account_key,
                "detected_plan": detected_plan,
                "detected_plan_label": plan_label(product, detected_plan),
                "plan": plan,
                "plan_label": plan_label(product, plan),
                "monthly_usd": monthly,
                "override_plan": None if override is None else override.get("plan"),
                "override_monthly_usd": (
                    None if override is None else override.get("monthly_usd")
                ),
                "unknown_plan": unknown,
            }
        )
    for identity, override in sorted(overrides.items()):
        if identity in seen:
            continue
        product, account_key = identity
        plan, monthly, unknown = resolve_price(product, None, override)
        rows.append(
            {
                "product": product,
                "product_label": PRODUCT_LABELS[product],
                "account_key": account_key,
                "display_name": account_key,
                "detected_plan": None,
                "detected_plan_label": None,
                "plan": plan,
                "plan_label": plan_label(product, plan),
                "monthly_usd": monthly,
                "override_plan": override.get("plan"),
                "override_monthly_usd": override.get("monthly_usd"),
                "unknown_plan": unknown,
            }
        )
    rows.sort(
        key=lambda item: (
            {"claude": 0, "codex": 1, "grok": 2}.get(str(item["product"]), 9),
            str(item["display_name"]).lower(),
            str(item["account_key"]),
        )
    )
    return {
        "available": state_dir is not None,
        "writable": bool(writable and state_dir is not None),
        "plans": plans_catalog(),
        "accounts": rows,
    }
