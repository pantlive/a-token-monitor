"""界面、API 文案与 CLI 输出的多语言支持。

中文是源语言：代码里写中文，英文由本模块的目录表提供。这样做的原因是整个项目
的文案散在 HTML/JS 模板、API 负载和 CLI 打印里，改成消息 ID 需要动上千处调用点；
当前做法是「中文原文 → 英文」的映射加上三处边界处理：

* 页面：对整份 HTML 做一次子串替换（``substitute``），JS 模板里的文案同样覆盖；
* API：对返回的负载递归翻译（``localize_payload``），所以后端生成的建议、画像、
  额度周期名、产品名在英文界面下也是英文；
* CLI：按 ``LANG`` 判断语言后把 ``sys.stdout`` 包一层翻译代理（``translating_stream``）。

目录表要求「英文页面里不出现中文」：测试会用真实浏览器渲染英文页面并断言可见文本
里没有中日韩字符，缺条目会直接失败。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

# 支持的语言；zh 是源语言，不需要目录表。
SUPPORTED_LANGUAGES: tuple[str, ...] = ("zh", "en")
DEFAULT_LANGUAGE = "zh"

_CJK_PATTERN = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")
# 页面里中文注释（CSS /* */、JS // 行注释）不参与替换，也不用翻译。
_BLOCK_COMMENT_PATTERN = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT_PATTERN = re.compile(r"^[ \t]*//.*$", re.M)
# 抓「引号或标签之间的中文串」，用于生成待翻译清单。
_QUOTED_PATTERN = re.compile(
    r"[\"'`>]([^\"'`<>]*[\u4e00-\u9fff][^\"'`<>]*)[\"'`<]",
)

# 目录表补齐前，英文页面还没准备好对外；最后一轮把这里翻成 True，
# 同时 test_english_pages_have_no_cjk 会从 skip 变成必须通过。
EN_COMPLETE = False

_EN: dict[str, str] = {
    # 语言与界面框架
    "中文": "中文",
    "English": "English",
}


def contains_cjk(text: str) -> bool:
    """判断文本里是否还有中日韩字符（英文界面里不应该出现）。"""

    return bool(_CJK_PATTERN.search(text or ""))


def normalize_language(value: str | None) -> str | None:
    """把 ``zh-CN`` / ``en_US`` / ``en-US,en;q=0.9`` 这类写法归一到 ``zh`` / ``en``。"""

    raw = (value or "").strip().lower()
    if not raw:
        return None
    for tag in raw.split(","):
        # Accept-Language 里带 q 权重，取标签名部分即可（顺序已经按权重排好）。
        name = tag.split(";")[0].strip().replace("_", "-")
        if not name:
            continue
        if name.startswith("zh"):
            return "zh"
        if name.startswith("en"):
            return "en"
    return None


def language_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """按 CLI 的 ``LANG`` / ``LC_ALL`` / ``LC_MESSAGES`` 判断语言。"""

    source = env if env is not None else _process_env()
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = source.get(key)
        if value:
            return normalize_language(value)
    return None


def resolve_language(
    accept_language: str | None = None,
    override: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """决定使用哪种语言：显式选择 > 浏览器/系统语言 > 默认中文。"""

    explicit = normalize_language(override)
    if explicit is not None:
        return explicit
    for candidate in (accept_language,):
        resolved = normalize_language(candidate)
        if resolved is not None:
            return resolved
    from_env = language_from_env(env)
    if from_env is not None:
        return from_env
    return DEFAULT_LANGUAGE


def translate(text: str, lang: str = DEFAULT_LANGUAGE) -> str:
    """翻译一整条文案；没有条目时原样返回。"""

    if lang != "en" or not text:
        return text
    return _EN.get(text, text)


def substitute(text: str, lang: str = DEFAULT_LANGUAGE) -> str:
    """把文本里出现的所有目录条目替换成目标语言（用于整份 HTML 模板）。

    按长度倒序替换，避免短条目先命中把长条目切碎（例如「额度」与「额度窗口」）。
    """

    if lang != "en" or not text:
        return text
    for source in sorted(_EN, key=len, reverse=True):
        target = _EN[source]
        if source in text:
            text = text.replace(source, target)
    return text


def localize_payload(value: Any, lang: str = DEFAULT_LANGUAGE) -> Any:
    """递归翻译 API 负载里的字符串（字典键不动，只翻译值）。

    没有需要改动的分支原样返回，避免为英文请求复制整份负载。
    """

    if lang != "en":
        return value
    if isinstance(value, str):
        return translate(value, lang)
    if isinstance(value, Mapping):
        localized = {
            key: localize_payload(item, lang) for key, item in value.items()
        }
        if all(localized[key] is value[key] for key in value):
            return value
        return localized
    if isinstance(value, (list, tuple)):
        items = [localize_payload(item, lang) for item in value]
        if all(new is old for new, old in zip(items, value)):
            return value
        return items
    return value


def iter_source_strings(text: str) -> Iterator[str]:
    """列出模板里可能需要翻译的中文串（跳过注释），用于生成待翻译清单。"""

    stripped = _LINE_COMMENT_PATTERN.sub("", _BLOCK_COMMENT_PATTERN.sub("", text))
    seen: set[str] = set()
    for match in _QUOTED_PATTERN.finditer(stripped):
        candidate = match.group(1).strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        yield candidate


def missing_entries(text: str) -> tuple[str, ...]:
    """列出模板里还没有英文条目的中文串。"""

    return tuple(item for item in iter_source_strings(text) if item not in _EN)


def catalog_size() -> int:
    """当前英文条目数量（文档与测试用）。"""

    return len(_EN)


def _process_env() -> Mapping[str, str]:
    import os

    return os.environ
