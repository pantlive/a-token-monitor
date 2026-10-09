"""本地 Dashboard：展示额度、活动会话、本地用量和历史告警。

子模块划分：

* ``assets``   读取 ``static/`` 下的 HTML / CSS / JS，拼装并本地化页面
* ``favicon``  生成站点图标（SVG / PNG / ICO）
* ``state``    汇总 ``/api/state`` 的监控状态
* ``payloads`` 各接口的参数解析与响应负载
* ``handler``  HTTP 请求处理与路由
* ``server``   服务配置、启动与账号集合热替换

这里重新导出包外（含测试）使用的名字，``from a_token_monitor.dashboard import X``
保持可用。
"""

from __future__ import annotations

from .assets import (
    _BASE_CSS,
    _DASHBOARD_CSS,
    _DASHBOARD_HTML,
    _RESPONSIVE_CSS,
    _SETTINGS_HTML,
)
from .favicon import (
    _FAVICON_GLYPHS,
    _brand_mark_svg,
    _favicon_ico,
    _favicon_svg,
    _favicon_token,
    favicon_response,
)
from .server import (
    DashboardConfig,
    DashboardServer,
)
from .state import (
    _quota_summary,
    build_multi_dashboard_state,
)

__all__ = [
    "DashboardConfig",
    "DashboardServer",
    "_BASE_CSS",
    "_DASHBOARD_CSS",
    "_DASHBOARD_HTML",
    "_FAVICON_GLYPHS",
    "_RESPONSIVE_CSS",
    "_SETTINGS_HTML",
    "_brand_mark_svg",
    "_favicon_ico",
    "_favicon_svg",
    "_favicon_token",
    "_quota_summary",
    "build_multi_dashboard_state",
    "favicon_response",
]
