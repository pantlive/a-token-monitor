"""Code agent 注册表：每个 agent 的目录、命令行参数和活动会话探测只在这里登记一次。

各层（命令行、systemd 服务配置、扫描目录设置、多账号监控、用量索引、Dashboard、
告警详情）不再为每个 agent 单独声明 ``xxx_homes`` 参数，而是传递一个
``{provider key: 目录元组}`` 的映射，并按本表遍历。

新增一个 agent 时：

1. 在对应模块实现目录解析（``default_xxx_home`` / ``resolve_xxx_homes``），
   以及需要的会话解析、活动会话探测；
2. 在下面的 ``PROVIDER_SPECS`` 里登记一条；
3. 在各功能的分派表里接上解析器（用量读取、告警详情、磁盘归档规则），
   ``tests/test_providers.py`` 会逐项检查是否有遗漏。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .accounts import default_codex_home
from .claude import (
    default_claude_home,
    list_claude_active_sessions,
    resolve_claude_homes,
)
from .commandcode import (
    default_commandcode_home,
    list_commandcode_active_sessions,
    resolve_commandcode_homes,
)
from .dsh import default_dsh_home, list_dsh_active_sessions, resolve_dsh_homes
from .grok import default_grok_home, list_grok_active_sessions, resolve_grok_homes
from .kimi import default_kimi_home, list_kimi_active_sessions, resolve_kimi_homes
from .local_agents import (
    default_aider_home,
    default_cursor_home,
    default_gemini_home,
    default_opencode_home,
    default_qwen_home,
    list_aider_active_sessions,
    list_cursor_active_sessions,
    list_gemini_active_sessions,
    list_qwen_active_sessions,
    resolve_aider_homes,
    resolve_cursor_homes,
    resolve_gemini_homes,
    resolve_opencode_homes,
    resolve_qwen_homes,
)

# 中文注释：各层传递的目录映射，键是 provider key，值是规范化后的目录元组。
ProviderHomes = Mapping[str, tuple[Path, ...]]
# 中文注释：入参形式：值为 None 表示「未指定」，由调用方约定是自动探测还是禁用。
ProviderHomesInput = Mapping[str, Sequence[Path] | None]


@dataclass(frozen=True)
class ProviderSpec:
    """一个 code agent 的登记信息。

    ``key`` 是配置键：scan-dirs.json、service.json 里的 ``<key>_homes``、命令行
    ``--<key>-home`` 和目录映射都用它；``product`` 是会话、告警、用量记录里的
    产品 ID，缺省与 ``key`` 相同（Command Code 例外：配置键 ``commandcode``，
    产品 ID ``command-code``）。
    """

    key: str
    display_name: str
    cli_option: str
    # 中文注释：任一标记存在即认为目录符合该 provider 的结构。
    markers: tuple[str, ...]
    default_home: Callable[[], Path]
    resolver: Callable[[Sequence[Path] | None], tuple[Path, ...]]
    product: str = ""
    cli_help: str = ""
    # 中文注释：列出某个数据目录下仍在运行的会话；没有活动会话概念的 provider 为 None。
    active_sessions: Callable[[Path], Iterable[Any]] | None = None

    @property
    def product_id(self) -> str:
        """会话、告警、用量记录里使用的产品 ID。"""

        return self.product or self.key

    @property
    def homes_field(self) -> str:
        """命令行 dest 与 service.json 字段名：``<key>_homes``。"""

        return f"{self.key}_homes"


def _resolve_codex_homes(homes: Sequence[Path] | None) -> tuple[Path, ...]:
    """解析 Codex 登录目录;未传入时仅在默认目录存在时使用它。"""

    if homes is None:
        default_home = default_codex_home().expanduser()
        return (default_home,) if default_home.exists() else ()
    return _unique_normalized(homes)


def _unique_normalized(paths: Sequence[Path]) -> tuple[Path, ...]:
    """按传入顺序去重并规范化为稳定绝对路径。"""

    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        normalized = _normalize(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def _normalize(path: Path) -> Path:
    """展开用户目录并尽量生成稳定的绝对路径。"""

    expanded = path.expanduser()
    try:
        return expanded.resolve()
    except (OSError, RuntimeError):
        return expanded.absolute()


# 中文注释：顺序即设置页、命令行帮助和磁盘统计里的展示顺序。Codex 按账号管理
# （每个 CODEX_HOME 一个账号监控器），不进入 home_providers() 的目录映射。
PROVIDER_SPECS: dict[str, ProviderSpec] = {
    spec.key: spec
    for spec in (
        ProviderSpec(
            key="codex",
            display_name="Codex",
            cli_option="--codex-home",
            markers=("sessions", "auth.json"),
            default_home=default_codex_home,
            resolver=_resolve_codex_homes,
            cli_help=(
                "Codex 登录目录，可重复传入多个账号（默认: ~/.codex；例如 ~/.codex-work）"
            ),
        ),
        ProviderSpec(
            key="claude",
            display_name="Claude Code",
            cli_option="--claude-home",
            markers=("projects",),
            default_home=default_claude_home,
            resolver=resolve_claude_homes,
            cli_help=(
                "Claude Code 数据目录，可重复传入；"
                "默认在存在时使用 ~/.claude 或 CLAUDE_CONFIG_DIR"
            ),
            active_sessions=list_claude_active_sessions,
        ),
        ProviderSpec(
            key="commandcode",
            display_name="Command Code",
            cli_option="--commandcode-home",
            markers=("projects", "auth.json"),
            default_home=default_commandcode_home,
            resolver=resolve_commandcode_homes,
            product="command-code",
            cli_help=(
                "Command Code 数据目录，可重复传入；"
                "默认在存在时使用 ~/.commandcode 或 COMMANDCODE_HOME"
            ),
            active_sessions=list_commandcode_active_sessions,
        ),
        ProviderSpec(
            key="dsh",
            display_name="DeepSeek Harness",
            cli_option="--dsh-home",
            markers=("sessions", "storages"),
            default_home=default_dsh_home,
            resolver=resolve_dsh_homes,
            cli_help=(
                "DeepSeek Harness 数据目录，可重复传入；"
                "默认在存在时使用 ~/.dsh 或 DSH_HOME"
            ),
            active_sessions=list_dsh_active_sessions,
        ),
        ProviderSpec(
            key="grok",
            display_name="Grok",
            cli_option="--grok-home",
            markers=("logs", "sessions", "auth.json"),
            default_home=default_grok_home,
            resolver=resolve_grok_homes,
            cli_help="Grok 登录目录，可重复传入；默认在存在时使用 ~/.grok 或 GROK_HOME",
            active_sessions=list_grok_active_sessions,
        ),
        ProviderSpec(
            key="kimi",
            display_name="Kimi Code",
            cli_option="--kimi-home",
            markers=("sessions",),
            default_home=default_kimi_home,
            resolver=resolve_kimi_homes,
            cli_help=(
                "Kimi Code 数据目录，可重复传入；"
                "默认在存在时使用 ~/.kimi-code 或 KIMI_CODE_HOME"
            ),
            active_sessions=list_kimi_active_sessions,
        ),
        ProviderSpec(
            key="opencode",
            display_name="OpenCode",
            cli_option="--opencode-home",
            markers=("opencode.db",),
            default_home=default_opencode_home,
            resolver=resolve_opencode_homes,
            cli_help=(
                "OpenCode 数据目录，可重复传入；"
                "默认在存在时使用 ~/.local/share/opencode 或 OPENCODE_DB"
            ),
        ),
        ProviderSpec(
            key="cursor",
            display_name="Cursor",
            cli_option="--cursor-home",
            markers=("projects",),
            default_home=default_cursor_home,
            resolver=resolve_cursor_homes,
            cli_help=(
                "Cursor 数据目录，可重复传入；"
                "默认在存在时使用 ~/.cursor 或 CURSOR_CONFIG_DIR"
            ),
            active_sessions=list_cursor_active_sessions,
        ),
        ProviderSpec(
            key="gemini",
            display_name="Gemini CLI",
            cli_option="--gemini-home",
            markers=("tmp", "projects.json"),
            default_home=default_gemini_home,
            resolver=resolve_gemini_homes,
            cli_help=(
                "Gemini CLI 数据目录，可重复传入；"
                "默认在存在时使用 ~/.gemini 或 GEMINI_CLI_HOME"
            ),
            active_sessions=list_gemini_active_sessions,
        ),
        ProviderSpec(
            key="qwen",
            display_name="Qwen Code",
            cli_option="--qwen-home",
            markers=("projects", "tmp"),
            default_home=default_qwen_home,
            resolver=resolve_qwen_homes,
            cli_help=(
                "Qwen Code 数据目录，可重复传入；"
                "默认在存在时使用 ~/.qwen、QWEN_HOME 或 QWEN_CODE_HOME"
            ),
            active_sessions=list_qwen_active_sessions,
        ),
        ProviderSpec(
            key="aider",
            display_name="Aider",
            cli_option="--aider-home",
            markers=(".aider.chat.history.md", "analytics.json"),
            default_home=default_aider_home,
            resolver=resolve_aider_homes,
            cli_help=(
                "Aider 数据目录，可重复传入；"
                "默认在存在时使用 ~/.aider 或 AIDER_HOME"
            ),
            active_sessions=list_aider_active_sessions,
        ),
    )
}


def home_providers() -> tuple[ProviderSpec, ...]:
    """按目录映射管理的 provider（除 Codex 外的全部 agent）。"""

    return tuple(spec for key, spec in PROVIDER_SPECS.items() if key != "codex")


def home_keys() -> tuple[str, ...]:
    """目录映射的全部键。"""

    return tuple(spec.key for spec in home_providers())


def resolve_provider_homes(
    homes: ProviderHomesInput | None,
    *,
    auto_detect: bool,
) -> dict[str, tuple[Path, ...]]:
    """把入参映射解析成完整的目录映射（每个 provider 都有键）。

    给出的值（包括空元组）一律经该 provider 的 resolver 规范化去重；未给出或为
    None 的 provider：``auto_detect=True`` 时探测本机默认目录，否则视为禁用。
    未知的键直接报错，避免拼错键名（例如 ``command-code``）后静默不扫描。
    """

    given = dict(homes or {})
    unknown = set(given) - set(home_keys())
    if unknown:
        raise ValueError(f"未知的 provider: {', '.join(sorted(unknown))}")
    resolved: dict[str, tuple[Path, ...]] = {}
    for spec in home_providers():
        value = given.get(spec.key)
        if value is None:
            resolved[spec.key] = spec.resolver(None) if auto_detect else ()
        else:
            resolved[spec.key] = spec.resolver(value)
    return resolved


def update_provider_homes(
    current: ProviderHomes,
    changes: ProviderHomesInput | None,
) -> dict[str, tuple[Path, ...]]:
    """热更新：给出的键（含空元组）替换并规范化，未给出或为 None 的键保持不变。"""

    given = {key: value for key, value in (changes or {}).items() if value is not None}
    unknown = set(given) - set(home_keys())
    if unknown:
        raise ValueError(f"未知的 provider: {', '.join(sorted(unknown))}")
    updated = dict(current)
    for key, value in given.items():
        updated[key] = PROVIDER_SPECS[key].resolver(value)
    return updated
