"""告警上下文：把流量告警映射回本地会话文件，回答「当时在上传什么」。

流量监控只按内核 TCP 计数器统计字节数，看不到也存不下具体内容；但 agent
发给 API 的内容（用户消息、工具调用、工具输出）完整落在本地会话 JSONL 里。
本包按告警的 ``cwd`` 和时间窗定位会话文件，提取窗口内的事件明细。

子模块划分：

* ``common``  窗口常量、候选与提取结果类型、记录扫描和文本摘要工具
* ``lookup``  入口 ``load_alert_context``：按产品分派候选定位与事件提取
* 每个 agent 一个模块（``codex``、``claude``、``kimi``、``commandcode``、
  ``grok``、``dsh``、``opencode``、``cursor``、``chat``、``aider``），
  各自负责会话定位和记录解析

这里重新导出包外（含测试）使用的名字。
"""

from __future__ import annotations

from .codex import (
    _extract_codex,
)
from .common import (
    AlertContextRoots,
    _excerpt,
)
from .kimi import (
    _extract_kimi,
)
from .lookup import (
    SUPPORTED_PRODUCTS,
    _SOURCES,
    configured_alert_context_roots,
    default_alert_context_roots,
    load_alert_context,
)

__all__ = [
    "AlertContextRoots",
    "SUPPORTED_PRODUCTS",
    "_SOURCES",
    "_excerpt",
    "_extract_codex",
    "_extract_kimi",
    "configured_alert_context_roots",
    "default_alert_context_roots",
    "load_alert_context",
]
