"""Web 端可配置的 provider 扫描目录:校验、持久化和优先级解析。

优先级:Web 配置(scan-dirs.json)> 命令行参数 > 自动探测默认目录。
所有路径在保存前必须存在、可读,且位于用户主目录之内;模块不提供任何
目录列表能力,避免通过 Web 任意浏览服务器文件系统。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from .accounts import default_codex_home
from .claude import default_claude_home, resolve_claude_homes
from .commandcode import default_commandcode_home, resolve_commandcode_homes
from .dsh import default_dsh_home, resolve_dsh_homes
from .grok import default_grok_home, resolve_grok_homes
from .kimi import default_kimi_home, resolve_kimi_homes


SCAN_DIRS_FILENAME = "scan-dirs.json"

# 中文注释:即使用户显式提交也拒绝监控的敏感目录(相对主目录)。
_DENIED_HOME_SUBDIRS = (".ssh", ".gnupg", ".aws", ".kube")


class ScanDirsError(ValueError):
    """扫描目录配置或校验失败。"""


@dataclass(frozen=True)
class ProviderSpec:
    """一个 provider 的扫描目录元数据。"""

    key: str
    display_name: str
    cli_option: str
    # 中文注释:任一标记存在即认为目录符合该 provider 的结构。
    markers: tuple[str, ...]
    default_home: Callable[[], Path]
    resolver: Callable[[Sequence[Path] | None], tuple[Path, ...]]


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
        ),
        ProviderSpec(
            key="claude",
            display_name="Claude Code",
            cli_option="--claude-home",
            markers=("projects",),
            default_home=default_claude_home,
            resolver=resolve_claude_homes,
        ),
        ProviderSpec(
            key="commandcode",
            display_name="Command Code",
            cli_option="--commandcode-home",
            markers=("projects", "auth.json"),
            default_home=default_commandcode_home,
            resolver=resolve_commandcode_homes,
        ),
        ProviderSpec(
            key="dsh",
            display_name="DeepSeek Harness",
            cli_option="--dsh-home",
            markers=("sessions", "storages"),
            default_home=default_dsh_home,
            resolver=resolve_dsh_homes,
        ),
        ProviderSpec(
            key="grok",
            display_name="Grok",
            cli_option="--grok-home",
            markers=("logs", "sessions", "auth.json"),
            default_home=default_grok_home,
            resolver=resolve_grok_homes,
        ),
        ProviderSpec(
            key="kimi",
            display_name="Kimi Code",
            cli_option="--kimi-home",
            markers=("sessions",),
            default_home=default_kimi_home,
            resolver=resolve_kimi_homes,
        ),
    )
}


@dataclass(frozen=True)
class DirectoryValidation:
    """单个扫描目录的校验结果。"""

    path: Path
    exists: bool
    readable: bool
    structure_ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """不存在硬错误时目录可用(结构不符只算警告)。"""

        return not self.errors


def validate_directory(
    provider: str,
    path: Path,
    *,
    home_dir: Path | None = None,
    state_dir: Path | None = None,
) -> DirectoryValidation:
    """校验目录是否存在、可读、符合 provider 结构,且不越出允许范围。"""

    spec = PROVIDER_SPECS.get(provider)
    if spec is None:
        raise ScanDirsError(f"未知的 provider: {provider}")
    resolved = _normalize(path)
    home = _normalize(home_dir) if home_dir is not None else _normalize(Path.home())
    errors: list[str] = []
    warnings: list[str] = []

    # 中文注释:只允许配置主目录之内的路径,防止通过 Web 探测服务器其他位置。
    if not _is_within(resolved, home) or resolved == home:
        errors.append("目录必须位于当前用户主目录之内")
    for denied in _DENIED_HOME_SUBDIRS:
        denied_path = home / denied
        if resolved == denied_path or _is_within(resolved, denied_path):
            errors.append(f"不允许扫描敏感目录: ~/{denied}")
            break
    if state_dir is not None:
        normalized_state = _normalize(state_dir)
        if resolved == normalized_state or _is_within(resolved, normalized_state):
            errors.append("不允许把监控状态目录本身作为扫描目录")

    try:
        exists = resolved.is_dir()
    except OSError:
        exists = False
    readable = False
    if not exists:
        errors.append("目录不存在")
    else:
        readable = os.access(resolved, os.R_OK | os.X_OK)
        if not readable:
            errors.append("目录不可读")

    # 中文注释:不可读目录上的 exists() 会抛 PermissionError,只在可读时检查结构。
    structure_ok = False
    if exists and readable:
        try:
            structure_ok = any(
                (resolved / marker).exists() for marker in spec.markers
            )
        except OSError:
            structure_ok = False
    if exists and readable and not structure_ok:
        markers = " 或 ".join(spec.markers)
        warnings.append(f"目录存在但缺少 {spec.display_name} 的典型结构({markers})")

    return DirectoryValidation(
        path=resolved,
        exists=exists,
        readable=readable,
        structure_ok=structure_ok,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def _is_within(path: Path, parent: Path) -> bool:
    """判断规范化后的路径是否位于某个目录之内。"""

    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class ScanDirsConfig:
    """持久化到状态目录的 Web 扫描目录覆盖配置。"""

    SCHEMA_VERSION: ClassVar[int] = 1

    # 中文注释:只记录 Web 显式设置过的 provider;空元组表示显式禁用。
    overrides: Mapping[str, tuple[Path, ...]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """转换为 JSON 字典;只保存设置过覆盖的 provider。"""

        return {
            "schema_version": self.SCHEMA_VERSION,
            "overrides": {
                key: [str(path) for path in self.overrides[key]]
                for key in sorted(self.overrides)
            },
        }

    def save(self, path: Path) -> None:
        """原子保存配置并限制为当前用户可读写。"""

        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        temporary_path = path.with_suffix(".json.tmp")
        temporary_path.write_text(f"{payload}\n", encoding="utf-8")
        temporary_path.chmod(0o600)
        temporary_path.replace(path)
        path.chmod(0o600)

    @classmethod
    def load(cls, path: Path) -> ScanDirsConfig:
        """从磁盘读取并严格校验配置;文件不存在时视为没有覆盖。"""

        try:
            raw_payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ScanDirsError(f"无法读取扫描目录配置 {path}: {error}") from error
        if not isinstance(raw_payload, Mapping):
            raise ScanDirsError("扫描目录配置根节点必须是 JSON 对象")
        schema_version = raw_payload.get("schema_version")
        if schema_version != cls.SCHEMA_VERSION:
            raise ScanDirsError(f"不支持的扫描目录配置版本: {schema_version}")
        overrides_value = raw_payload.get("overrides")
        if overrides_value is None:
            return cls()
        if not isinstance(overrides_value, Mapping):
            raise ScanDirsError("扫描目录配置 overrides 必须是对象")
        overrides: dict[str, tuple[Path, ...]] = {}
        for key, value in overrides_value.items():
            if key not in PROVIDER_SPECS:
                raise ScanDirsError(f"扫描目录配置包含未知 provider: {key}")
            if not isinstance(value, list):
                raise ScanDirsError(f"扫描目录配置 overrides.{key} 必须是列表")
            paths: list[Path] = []
            for index, item in enumerate(value):
                if not isinstance(item, str) or not item.strip():
                    raise ScanDirsError(
                        f"扫描目录配置 overrides.{key}[{index}] 必须是非空字符串"
                    )
                paths.append(_normalize(Path(item)))
            overrides[key] = tuple(paths)
        return cls(overrides=overrides)

    def with_added(self, provider: str, path: Path) -> ScanDirsConfig:
        """返回追加了目录的新配置;已存在时不变。"""

        self._check_provider(provider)
        normalized = _normalize(path)
        current = list(self._base_dirs(provider))
        if normalized in current:
            return self
        current.append(normalized)
        return self._with_override(provider, tuple(current))

    def with_removed(self, provider: str, path: Path) -> ScanDirsConfig:
        """返回移除了目录的新配置;目录不存在时不变。"""

        self._check_provider(provider)
        normalized = _normalize(path)
        current = list(self._base_dirs(provider))
        if normalized not in current:
            return self
        current.remove(normalized)
        return self._with_override(provider, tuple(current))

    def without_provider(self, provider: str) -> ScanDirsConfig:
        """返回删除某 provider 覆盖的新配置(回退命令行/自动探测)。"""

        self._check_provider(provider)
        if provider not in self.overrides:
            return self
        overrides = dict(self.overrides)
        del overrides[provider]
        return ScanDirsConfig(overrides=overrides)

    def _base_dirs(self, provider: str) -> tuple[Path, ...]:
        """返回该 provider 覆盖列表的当前值。"""

        return tuple(self.overrides.get(provider, ()))

    def _with_override(
        self,
        provider: str,
        paths: tuple[Path, ...],
    ) -> ScanDirsConfig:
        overrides = dict(self.overrides)
        overrides[provider] = paths
        return ScanDirsConfig(overrides=overrides)

    @staticmethod
    def _check_provider(provider: str) -> None:
        if provider not in PROVIDER_SPECS:
            raise ScanDirsError(f"未知的 provider: {provider}")


@dataclass(frozen=True)
class ProviderDirsState:
    """一个 provider 的最终生效目录及其来源。"""

    provider: str
    # 中文注释:source ∈ {"web", "cli", "auto"};effective 为空表示未启用。
    source: str
    effective: tuple[Path, ...]
    cli_dirs: tuple[Path, ...] | None
    override_dirs: tuple[Path, ...] | None
    default_dir: Path

    @property
    def enabled(self) -> bool:
        """是否有生效的扫描目录。"""

        return bool(self.effective)


@dataclass(frozen=True)
class EffectiveScanDirs:
    """所有 provider 的优先级解析结果。"""

    states: tuple[ProviderDirsState, ...]

    def state(self, provider: str) -> ProviderDirsState:
        """返回单个 provider 的解析结果。"""

        for item in self.states:
            if item.provider == provider:
                return item
        raise ScanDirsError(f"未知的 provider: {provider}")

    def homes(self, provider: str) -> tuple[Path, ...]:
        """返回最终生效的目录列表。"""

        return self.state(provider).effective


def resolve_effective(
    config: ScanDirsConfig,
    cli_homes: Mapping[str, Sequence[Path] | None],
) -> EffectiveScanDirs:
    """按 Web 配置 > 命令行参数 > 自动探测的优先级解析每个 provider。"""

    states: list[ProviderDirsState] = []
    for key, spec in PROVIDER_SPECS.items():
        cli_value = cli_homes.get(key)
        cli_dirs = tuple(_normalize(path) for path in cli_value) if cli_value is not None else None
        override_dirs = config.overrides.get(key)
        if override_dirs is not None:
            source = "web"
            effective = override_dirs
        elif cli_dirs is not None:
            source = "cli"
            effective = cli_dirs
        else:
            source = "auto"
            effective = spec.resolver(None)
        states.append(
            ProviderDirsState(
                provider=key,
                source=source,
                effective=tuple(effective),
                cli_dirs=cli_dirs,
                override_dirs=(tuple(override_dirs) if override_dirs is not None else None),
                default_dir=_normalize(spec.default_home()),
            )
        )
    return EffectiveScanDirs(states=tuple(states))


class ScanDirsController:
    """Dashboard 使用的扫描目录状态与修改入口(线程安全)。"""

    def __init__(
        self,
        state_dir: Path,
        cli_homes: Mapping[str, Sequence[Path] | None],
        *,
        reload_callback: Callable[[Mapping[str, tuple[Path, ...]]], None] | None = None,
        config_path: Path | None = None,
        home_dir: Path | None = None,
    ) -> None:
        """加载持久化配置并记录命令行层目录(可能为 None 表示未指定)。"""

        self.state_dir = _normalize(state_dir)
        self.config_path = config_path or self.state_dir / SCAN_DIRS_FILENAME
        self._home_dir = home_dir
        self._cli_homes: dict[str, tuple[Path, ...] | None] = {
            key: (tuple(value) if value is not None else None)
            for key, value in cli_homes.items()
        }
        for key in PROVIDER_SPECS:
            self._cli_homes.setdefault(key, None)
        self._config = ScanDirsConfig.load(self.config_path)
        self._reload_callback = reload_callback
        self._lock = threading.Lock()

    @property
    def reload_callback(
        self,
    ) -> Callable[[Mapping[str, tuple[Path, ...]]], None] | None:
        """修改生效后调用的热重载回调。"""

        return self._reload_callback

    @reload_callback.setter
    def reload_callback(
        self,
        callback: Callable[[Mapping[str, tuple[Path, ...]]], None] | None,
    ) -> None:
        self._reload_callback = callback

    def effective(self) -> EffectiveScanDirs:
        """返回当前生效的优先级解析结果。"""

        with self._lock:
            return resolve_effective(self._config, self._cli_homes)

    def snapshot(self) -> dict[str, object]:
        """返回 GET /api/scan-dirs 使用的完整状态,附带每个目录的校验结果。"""

        effective = self.effective()
        providers: list[dict[str, object]] = []
        for state in effective.states:
            spec = PROVIDER_SPECS[state.provider]
            directories = [
                self._directory_payload(state.provider, path)
                for path in state.effective
            ]
            providers.append(
                {
                    "key": state.provider,
                    "name": spec.display_name,
                    "cli_option": spec.cli_option,
                    "source": state.source,
                    "enabled": state.enabled,
                    "default_dir": str(state.default_dir),
                    "cli_dirs": (
                        [str(path) for path in state.cli_dirs]
                        if state.cli_dirs is not None
                        else None
                    ),
                    "override_dirs": (
                        [str(path) for path in state.override_dirs]
                        if state.override_dirs is not None
                        else None
                    ),
                    "directories": directories,
                }
            )
        return {
            "providers": providers,
            "priority": ["web", "cli", "auto"],
        }

    def apply(
        self,
        action: str,
        provider: str,
        path: Path | None = None,
    ) -> dict[str, object]:
        """校验并应用一次修改,持久化后触发热重载,返回最新状态。"""

        if provider not in PROVIDER_SPECS:
            raise ScanDirsError(f"未知的 provider: {provider}")
        with self._lock:
            if action == "add":
                if path is None:
                    raise ScanDirsError("add 操作需要 path")
                validation = validate_directory(
                    provider,
                    path,
                    home_dir=self._home_dir,
                    state_dir=self.state_dir,
                )
                if not validation.ok:
                    raise ScanDirsError(";".join(validation.errors))
                config = self._config.with_added(provider, validation.path)
            elif action == "remove":
                if path is None:
                    raise ScanDirsError("remove 操作需要 path")
                config = self._config.with_removed(provider, path)
            elif action == "reset":
                config = self._config.without_provider(provider)
            else:
                raise ScanDirsError(f"不支持的操作: {action}")
            # 中文注释:先落盘再重载;任何一步失败都不会留下半更新状态,
            # 因为 reload 只消费已经持久化的生效配置。
            config.save(self.config_path)
            self._config = config
            effective = resolve_effective(config, self._cli_homes)
        if self._reload_callback is not None:
            self._reload_callback(
                {state.provider: state.effective for state in effective.states}
            )
        return self.snapshot()

    def _directory_payload(self, provider: str, path: Path) -> dict[str, object]:
        """返回单个目录的状态;校验失败只降级该目录的展示。"""

        validation = validate_directory(
            provider,
            path,
            home_dir=self._home_dir,
            state_dir=self.state_dir,
        )
        return {
            "path": str(validation.path),
            "ok": validation.ok,
            "exists": validation.exists,
            "readable": validation.readable,
            "structure_ok": validation.structure_ok,
            "errors": list(validation.errors),
            "warnings": list(validation.warnings),
        }


def default_scan_dirs_path(state_dir: Path) -> Path:
    """返回扫描目录配置的固定保存位置。"""

    return state_dir.expanduser() / SCAN_DIRS_FILENAME


def load_effective_scan_dirs(
    state_dir: Path,
    cli_homes: Mapping[str, Sequence[Path] | None],
    *,
    config_path: Path | None = None,
) -> EffectiveScanDirs:
    """一次性命令使用的便捷入口:读配置并解析优先级。"""

    config = ScanDirsConfig.load(config_path or default_scan_dirs_path(state_dir))
    return resolve_effective(config, cli_homes)
