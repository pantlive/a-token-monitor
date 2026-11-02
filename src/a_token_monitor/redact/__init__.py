"""发给模型之前的密钥脱敏：规则与占位符、本地代理、各 agent 的 hook。

子模块：

* ``rules``      脱敏规则、确定性占位符、本地密钥表（vault）与配置
* ``streaming``  模型响应（SSE / JSON）里把占位符还原成原值
* ``proxy``      本地 HTTP 代理：请求脱敏后转发上游，响应流式还原
* ``hooks``      Claude Code / Codex / Grok 的 hook 入口
* ``install``    把代理或 hook 写进各 agent 的配置，以及卸载

占位符由本机密钥对原值做 HMAC 得到，同一个值每次都换成同一个占位符，
这样请求前缀不变，prompt cache 不会因为脱敏而失效。
"""

from __future__ import annotations

from .rules import (
    DEFAULT_OFF_RULES,
    PLACEHOLDER_PATTERN,
    Finding,
    RedactConfig,
    RedactStore,
    Redactor,
    default_disabled_rules,
)

__all__ = [
    "DEFAULT_OFF_RULES",
    "PLACEHOLDER_PATTERN",
    "Finding",
    "RedactConfig",
    "RedactStore",
    "Redactor",
    "default_disabled_rules",
]
