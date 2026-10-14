"""数字的紧凑显示：token 数量用 K / M / B 压缩，不写全量数字。

token 动辄七八位，表格里要一位位去数；这里与网页端 ``dashboard.js`` 的
``formatTokens`` 保持同一套规则（3 位有效数字左右），需要精确值时看网页端的
悬浮提示或 CLI 的 ``--json``。
"""

from __future__ import annotations

# 中文注释：单位阶梯与阈值；1e3 起才压缩，999 仍原样显示。
_UNITS: tuple[tuple[float, str], ...] = (
    (1e12, "T"),
    (1e9, "B"),
    (1e6, "M"),
    (1e3, "K"),
)
# 中文注释：小数点位数与网页端一致（B/T 两位、M 两位、K 一位）。
_DECIMALS = {"T": 2, "B": 2, "M": 2, "K": 1}


def format_tokens(value: object) -> str:
    """把 token 数量压成 ``1.23B`` / ``12.3M`` / ``1.2K``；小于 1000 原样显示。

    不是数字的值原样返回字符串，避免调用方为了显示再做一次类型判断。
    """

    try:
        amount = float(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    for threshold, unit in _UNITS:
        if amount >= threshold:
            return f"{amount / threshold:.{_DECIMALS[unit]}f}{unit}"
    return str(int(amount))
