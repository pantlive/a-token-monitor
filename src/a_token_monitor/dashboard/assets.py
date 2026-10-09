"""页面资源：读取 static/ 下的 HTML / CSS / JS，拼装主页与设置页并按语言本地化。"""

from __future__ import annotations

import functools
from importlib import resources

from ..i18n import substitute
from .favicon import (
    _FAVICON_LINK,
    _brand_mark_svg,
)


# 中文注释：页面的 HTML / CSS / JS 放在包内 static/ 目录，导入时读入内存；
# 运行时零依赖，也不在请求路径上读磁盘。
_STATIC = resources.files(__package__).joinpath("static")


def _asset(name: str) -> str:
    """读取 static/ 下的页面资源。"""

    return _STATIC.joinpath(name).read_text(encoding="utf-8")


def _page(template: str, styles: str, script: str) -> str:
    """把样式和主脚本填进页面模板，再注入主题、语言等共享片段。"""

    html = _asset(template).replace("__PAGE_STYLES__", styles)
    return _apply_theme_parts(html.replace("__PAGE_SCRIPT__", _asset(script)))


# 中文注释：主页与设置页共用的基础样式。
_BASE_CSS = _asset("base.css")


# 中文注释：主页独有的样式（KPI、账号卡片、用量、告警、习惯分析、磁盘管理等）。
_DASHBOARD_CSS = _asset("dashboard.css")


# 中文注释：两页共用的响应式覆盖；必须放在各页独有样式之后，否则媒体查询里的
# 覆盖会被后面的同名非媒体规则压回去。
_RESPONSIVE_CSS = _asset("responsive.css")


# 中文注释：设置页独有的样式；设置子块平级排列，之后可直接追加新的设置项。
_SETTINGS_CSS = _asset("settings.css")


# 中文注释：先给 ICO（老浏览器与 Safari 稳），再给 SVG（大小屏幕都清晰）。
_LANGUAGE_COOKIE_NAME = "a-token-monitor-language"


# 中文注释：页面是导入时就固定的常量，英文替换一次要 1 秒以上，按（页面, 语言）
# 缓存后每次请求只是一次字典查找；页面只有两个、语言只有两种，缓存上限足够。
@functools.lru_cache(maxsize=8)
def _localize_page(html: str, language: str) -> str:
    """按语言出页面：英文走目录表替换，并改 <html lang>。"""

    if language != "en":
        return html
    return substitute(html, "en").replace('<html lang="zh-CN">', '<html lang="en">', 1)


# 中文注释：主题实现由 Dashboard 与设置页共享，避免两个页面各写一份。
_LANGUAGE_BOOT_SCRIPT = _asset("language-boot.html")


_THEME_BOOT_SCRIPT = _asset("theme-boot.html")


_LANGUAGE_TOGGLE_HTML = _asset("language-toggle.html")


_THEME_TOGGLE_HTML = _asset("theme-toggle.html")


# 中文注释：顶栏右侧的作者 GitHub 链接，两页共用。
_GITHUB_LINK_HTML = _asset("github-link.html")


_LANGUAGE_SCRIPT = _asset("language.js")


_THEME_SCRIPT = _asset("theme.js")


_PAGE_THEME_REPLACEMENTS = (
    ("__THEME_BOOT__", _THEME_BOOT_SCRIPT + _LANGUAGE_BOOT_SCRIPT),
    ("__THEME_TOGGLE__", _LANGUAGE_TOGGLE_HTML + _THEME_TOGGLE_HTML + _GITHUB_LINK_HTML),
    ("__THEME_SCRIPT__", _THEME_SCRIPT + _LANGUAGE_SCRIPT),
    ("__FAVICON__", _FAVICON_LINK),
    ("__BRAND_MARK__", _brand_mark_svg()),
)


def _apply_theme_parts(html: str) -> str:
    """把共享的主题实现和页面标签注入模板，并确认占位符都已替换。"""

    for marker, snippet in _PAGE_THEME_REPLACEMENTS:
        html = html.replace(marker, snippet)
    return html


_DASHBOARD_HTML = _page("dashboard.html", _BASE_CSS + _DASHBOARD_CSS + _RESPONSIVE_CSS, "dashboard.js")


# 中文注释：独立设置页与主页共用 _BASE_CSS / _RESPONSIVE_CSS，只追加设置页独有样式；
# 设置子块（如扫描目录）平级放在 #settings-body 内，之后可直接追加新的设置项。
_SETTINGS_HTML = _page("settings.html", _BASE_CSS + _SETTINGS_CSS + _RESPONSIVE_CSS, "settings.js")


def warm_localized_pages() -> None:
    """预先生成英文页面，避免第一个英文访问者等待约 1 秒的整页翻译。"""

    for page in (_DASHBOARD_HTML, _SETTINGS_HTML):
        _localize_page(page, "en")
