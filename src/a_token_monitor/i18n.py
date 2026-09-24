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
import sys
from collections.abc import Iterator, Mapping
from typing import Any

from .i18n_catalog import EN as _CATALOG
from .i18n_catalog import PATTERNS as _CATALOG_PATTERNS

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

# 主页 / 设置页模板、API 负载与 CLI 输出都已英文化，这里打开后
# test_english_pages_have_no_cjk 不再 skip，成为强制验收线。
EN_COMPLETE = True

# 目录表放在 i18n_catalog.py 里，便于分批维护与 review。
_EN: dict[str, str] = dict(_CATALOG)
_EN.setdefault("中文", "中文")
_EN.setdefault("English", "English")
# 兼容旧名字：工具与测试里两种叫法都出现过。
_EN_PAGE = _EN
_EN_VALUE = _EN
# 带插值的句子（「会话 X 已进行 N 轮」这类）没法精确匹配，用正则模式翻译。
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(source), target) for source, target in _CATALOG_PATTERNS
)


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
    """翻译一整条文案：先精确匹配，再套模式规则，都没有就原样返回。

    模式规则用于带插值的句子（``会话 <id> 已进行 12 轮``），它们在负载里是拼好的
    字符串，精确目录表不可能覆盖。
    """

    if lang != "en" or not text:
        return text
    # 负载里的句子可能是「拼出来的」：先由模板拼标题，再拼进更长的说明。
    # 所以这里反复套用（最多 4 轮），让外层模式套完后内层残余也能翻到。
    for _ in range(4):
        exact = _EN.get(text)
        if exact is not None:
            return exact
        updated = text
        for pattern, replacement in _PATTERNS:
            if pattern.match(updated):
                updated = pattern.sub(replacement, updated)
                break
        if updated == text:
            return text
        text = updated
    return text


def substitute(text: str, lang: str = DEFAULT_LANGUAGE) -> str:
    """把文本里出现的目录条目替换成目标语言（用于整份 HTML/JS 模板）。

    两条规则保证不会翻坏：

    1. **按长度倒序**替换，长条目先命中（「额度窗口」优先于「额度」）；
    2. **中文边界断言**——只有当条目两侧不是其它中文字符时才替换。中文没有词边界，
       否则 ``可用`` 会命中 ``不可用`` 里，替换完就是 ``不Available`` 这种中英混杂。
       有了这条断言，覆盖不全的结果只会是「这句还没翻译」，不会是「翻译坏了」。
    """

    if lang != "en" or not text:
        return text
    for source in sorted(_EN, key=len, reverse=True):
        target = _EN[source]
        if source not in text:
            continue
        pattern = re.compile(
            r"(?<![\u4e00-\u9fff])" + re.escape(source) + r"(?![\u4e00-\u9fff])"
        )
        text = pattern.sub(lambda _match, value=target: value, text)
    return text


def localize_line(text: str, lang: str = DEFAULT_LANGUAGE) -> str:
    """CLI 输出用：精确匹配与模式规则之后，再补一遍带边界的子串替换。

    和 API 负载不同，CLI 的输出是一行行拼出来的（表头、状态行、帮助文本），允许子串
    替换；中文边界断言保证 ``可用`` 不会咬进 ``不可用`` 里。API 负载不能用这个函数，
    否则用户数据里的中文可能被误伤。
    """

    if lang != "en" or not text:
        return text
    return substitute(translate(text, lang), lang)


class TranslatedStream:
    """把写到某个流上的文本先翻译一遍（CLI 的 stdout/stderr 用）。

    放在流这一层是为了「一处接入」：CLI 与各模块都用 ``print`` / ``sys.stdout.write``
    输出，包一层就不用改上百处调用点。
    """

    def __init__(self, stream: Any, lang: str) -> None:
        self._stream = stream
        self._lang = lang
        self._buffer = ""

    def write(self, text: str) -> int:
        # CLI 会把一句话分几次 write（print 的多个参数、f-string 拼接），逐段翻译会
        # 把句子切断、模式规则匹配不上，所以先按行攒起来再整体翻译。
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._stream.write(localize_line(line, self._lang) + "\n")
        if "\r" in self._buffer:
            head, _, tail = self._buffer.rpartition("\r")
            self._stream.write(localize_line(head, self._lang) + "\r")
            self._buffer = tail
        if len(self._buffer) > 8192:
            self._stream.write(localize_line(self._buffer, self._lang))
            self._buffer = ""
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            self._stream.write(localize_line(self._buffer, self._lang))
            self._buffer = ""
        self._stream.flush()

    def detach(self) -> None:
        """把缓冲里剩下的内容写出去，并返回被包装的原始流。"""

        self.flush()
        return self._stream

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


_ACTIVE_LANGUAGE = DEFAULT_LANGUAGE


def active_language() -> str:
    """当前进程选定的语言（CLI 用；页面/接口按请求判定，不看这个）。"""

    return _ACTIVE_LANGUAGE


def use_language(lang: str) -> None:
    """按语言包装 stdout/stderr；英文以外的语言不做任何包装。"""

    global _ACTIVE_LANGUAGE
    _ACTIVE_LANGUAGE = lang
    if lang != "en":
        # 回到源语言：之前包过翻译代理就拆掉（同进程内反复切换时不留残留）。
        if isinstance(sys.stdout, TranslatedStream):
            sys.stdout = sys.stdout.detach()
        if isinstance(sys.stderr, TranslatedStream):
            sys.stderr = sys.stderr.detach()
        return
    stdout = sys.stdout
    stderr = sys.stderr
    if not isinstance(stdout, TranslatedStream):
        sys.stdout = TranslatedStream(stdout, lang)
    if not isinstance(stderr, TranslatedStream):
        sys.stderr = TranslatedStream(stderr, lang)


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


def iter_string_literals(source: str) -> Iterator[str]:
    """切出完整的字符串字面量（含模板字符串与 ``${}`` 嵌套），只保留含中文的。

    页面模板是 HTML + CSS + JS 混排，而且模板字符串里还会嵌引号（``'未知'``），
    用正则切会被误判成超长片段。这里做一次简单但正确的扫描：跳过注释，遇到引号
    就读到配对的收尾引号，模板字符串里的 ``${...}`` 按括号配对整体跳过（表达式里
    还能再嵌字符串与模板）。
    """

    index = 0
    length = len(source)
    seen: set[str] = set()
    while index < length:
        char = source[index]
        if char == "/" and index + 1 < length and source[index + 1] == "*":
            end = source.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "/":
            end = source.find("\n", index)
            index = length if end < 0 else end + 1
            continue
        if char in "\"'`":
            literal, index = _read_literal(source, index)
            if literal and _CJK_PATTERN.search(literal) and literal not in seen:
                seen.add(literal)
                yield literal
            continue
        index += 1


def _read_literal(source: str, start: int) -> tuple[str | None, int]:
    """从 ``start`` 处的引号读到配对收尾，返回 (字面量, 下一个位置)。"""

    quote = source[start]
    index = start + 1
    length = len(source)
    buffer: list[str] = []
    while index < length:
        char = source[index]
        if char == "\\":
            buffer.append(source[index : index + 2])
            index += 2
            continue
        if quote == "`":
            if char == "`":
                return "".join(buffer), index + 1
            if char == "$" and index + 1 < length and source[index + 1] == "{":
                expression, index = _read_expression(source, index + 2)
                buffer.append("${" + expression + "}")
                continue
        elif char == quote:
            return "".join(buffer), index + 1
        elif char == "\n":
            # 普通引号不跨行：说明这里的引号本来就是误判，放弃这一段。
            return None, index + 1
        buffer.append(char)
        index += 1
    return None, index


def _read_expression(source: str, start: int) -> tuple[str, int]:
    """读到与 ``${`` 配对的 ``}``，内部的字符串与模板整体保留。"""

    index = start
    length = len(source)
    depth = 1
    buffer: list[str] = []
    while index < length:
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return "".join(buffer), index + 1
        elif char in "\"'`":
            literal, index = _read_literal(source, index)
            if literal is not None:
                buffer.append(quote_of(char) + literal + quote_of(char))
            continue
        buffer.append(char)
        index += 1
    return "".join(buffer), index


def quote_of(char: str) -> str:
    """给表达式里遇到的引号补回收尾字符，保持字面量原样。"""

    return char


def unsafe_keys(
    source: str,
    keys: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """列出会「部分命中」的目录条目。

    中文没有词边界，短条目（例如 ``可用``）会命中更长文本（``不可用``）里，
    替换完就是 ``不Available`` 这种中英混杂。判定规则：条目 K 安全，当且仅当
    源码里每次出现 K 都落在「K 自己」或「另一个也收录了的更长片段」里——
    后者由 :func:`substitute` 的最长优先保证。
    """

    catalog = _EN_PAGE if keys is None else keys
    candidates = set(iter_text_runs(source)) | set(iter_string_literals(source))
    unsafe: set[str] = set()
    for key in catalog:
        if not _CJK_PATTERN.search(key) or key not in source:
            continue
        for host in candidates:
            if host == key or key not in host:
                continue
            if host in catalog:
                continue
            unsafe.add(key)
            break
    return tuple(sorted(unsafe))


def uncovered_runs(source: str, lang: str = "en") -> tuple[str, ...]:
    """列出还没收录进目录表的「最大中文片段」。"""

    if lang != "en":
        return ()
    english = substitute(source, lang)
    return tuple(
        run
        for run in iter_text_runs(source)
        if run in english and run not in _EN_PAGE
    )


def uncovered_literals(source: str, lang: str = "en") -> tuple[str, ...]:
    """列出替换后仍然原样保留的中文字面量，也就是还没处理的 JS/HTML 文案。"""

    english = substitute(source, lang)
    return tuple(item for item in iter_string_literals(source) if item in english)


# 中文片段的分隔符：标签、引号、模板插值、换行等语法边界。
_RUN_DELIMITER = re.compile(r'(?:\$\{|[<>"\'`\n\r])')


def iter_text_runs(source: str) -> Iterator[str]:
    """列出源码里的「最大中文片段」。

    片段以语法边界（标签、引号、``${`` 插值、换行）切分，所以像
    ``接近额度上限，注意剩余用量`` 这样整句是一条，而 ``不可用`` 不会被
    ``可用`` 这类短词条咬到——目录表只收这种最大片段，替换才不会产生中英混杂。
    """

    segments: list[str] = []
    for chunk in _RUN_DELIMITER.split(_strip_comments(source)):
        for piece in str(chunk).split("\n"):
            pieces = re.split(r"(?:\}[^\s]*|[A-Za-z_$][\w$.]*\()", piece)
            segments.extend(pieces)
    seen: set[str] = set()
    for segment in segments:
        candidate = segment.strip(" \t,;:.")
        if not candidate or not _CJK_PATTERN.search(candidate) or candidate in seen:
            continue
        seen.add(candidate)
        yield candidate


def _strip_comments(source: str) -> str:
    """去掉 CSS/JS 注释，避免注释里的中文进清单。"""

    return _LINE_COMMENT_PATTERN.sub("", _BLOCK_COMMENT_PATTERN.sub("", source))


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


def uncovered_strings(text: str, lang: str = "en") -> tuple[str, ...]:
    """列出替换后仍然「原样保留」的中文串，也就是真正还没处理的文案。

    比 :func:`missing_entries` 更贴近目标：碎片词条可能把句子替换成中英混杂，
    那种情况整句已经不再原样出现，得靠「替换结果里还有没有中文」来兜底。
    """

    if lang == "en":
        already = substitute(text, lang)
        return tuple(
            item for item in iter_source_strings(text) if item in already
        )
    return ()


def missing_entries(text: str) -> tuple[str, ...]:
    """列出模板里还没有英文条目的中文串。"""

    return tuple(item for item in iter_source_strings(text) if item not in _EN_PAGE)


def catalog_size() -> int:
    """当前英文条目数量（文档与测试用）。"""

    return len(_EN)


def _process_env() -> Mapping[str, str]:
    import os

    return os.environ
