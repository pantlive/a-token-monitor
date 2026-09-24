"""通过 systemd 用户服务长期运行额度监控器。

服务配置单独保存为权限受限的 JSON。systemd 单元只携带 Python 解释器和
状态目录两个稳定参数。
"""

from __future__ import annotations

import json
import logging
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, ClassVar
from xml.sax.saxutils import escape

from .accounts import build_account_specs
from .alerts import DEFAULT_RETENTION_DAYS as DEFAULT_ALERT_RETENTION_DAYS
from .housekeeping import DEFAULT_SINGLE_WARN_GIB, DEFAULT_TOTAL_WARN_GIB
from .monitor import MonitorConfig
from .multi_account import MultiAccountMonitor
from .retention import (
    DEFAULT_SESSION_RETENTION_DAYS,
    DEFAULT_USAGE_RETENTION_DAYS,
)
from .scan_dirs import ProviderDirsState, ScanDirsController
from .usage import (
    DEFAULT_SESSION_CONTEXT_WARN_TOKENS,
    DEFAULT_SESSION_TURN_WARN,
)


SERVICE_NAME = "a-token-monitor.service"
# 两次改名前的单元名：只用于识别旧安装，便于清理和兼容查询，新安装一律用 SERVICE_NAME。
LEGACY_SERVICE_NAMES = ("token-monitor.service", "codex-reset-monitor.service")


class ServiceError(RuntimeError):
    """后台服务配置或 systemd 操作失败。"""


@dataclass(frozen=True)
class ServiceConfig:
    """systemd 后台进程需要持久保存的完整 daemon 配置。"""

    SCHEMA_VERSION: ClassVar[int] = 1

    state_dir: Path
    codex_homes: tuple[Path, ...]
    session_root: Path | None
    verbose: bool
    codex_path: str
    scan_interval: float
    reconcile_interval: float
    quota_interval: float
    dashboard: bool
    dashboard_host: str
    dashboard_port: int
    grok_homes: tuple[Path, ...]
    kimi_homes: tuple[Path, ...] = ()
    dsh_homes: tuple[Path, ...] = ()
    commandcode_homes: tuple[Path, ...] = ()
    claude_homes: tuple[Path, ...] = ()
    budget_usd: float | None = None
    upload_burst_warn_mb: float = 8.0
    upload_burst_danger_mb: float = 32.0
    upload_window_warn_mb: float = 64.0
    upload_window_danger_mb: float = 256.0
    alert_retention_days: float = DEFAULT_ALERT_RETENTION_DAYS
    usage_retention_days: float = DEFAULT_USAGE_RETENTION_DAYS
    session_retention_days: float = DEFAULT_SESSION_RETENTION_DAYS
    session_turn_warn: int = DEFAULT_SESSION_TURN_WARN
    session_context_warn_tokens: int = DEFAULT_SESSION_CONTEXT_WARN_TOKENS
    disk_warn_gb: float = DEFAULT_SINGLE_WARN_GIB
    disk_total_warn_gb: float = DEFAULT_TOTAL_WARN_GIB

    def __post_init__(self) -> None:
        """校验服务配置，并复用运行时配置的边界检查。"""

        object.__setattr__(self, "state_dir", _absolute_path(self.state_dir))
        object.__setattr__(
            self,
            "codex_homes",
            tuple(_absolute_path(path) for path in self.codex_homes),
        )
        object.__setattr__(
            self,
            "grok_homes",
            tuple(_absolute_path(path) for path in self.grok_homes),
        )
        object.__setattr__(
            self,
            "kimi_homes",
            tuple(_absolute_path(path) for path in self.kimi_homes),
        )
        object.__setattr__(
            self,
            "dsh_homes",
            tuple(_absolute_path(path) for path in self.dsh_homes),
        )
        object.__setattr__(
            self,
            "commandcode_homes",
            tuple(_absolute_path(path) for path in self.commandcode_homes),
        )
        object.__setattr__(
            self,
            "claude_homes",
            tuple(_absolute_path(path) for path in self.claude_homes),
        )
        if self.session_root is not None:
            object.__setattr__(
                self,
                "session_root",
                _absolute_path(self.session_root),
            )
        # 中文注释：没有 CODEX_HOME 也允许安装后台服务（只监控其他 provider）。
        if not self.codex_path.strip():
            raise ValueError("Codex 可执行文件不能为空")
        if self.session_root is not None and len(self.codex_homes) != 1:
            raise ValueError("--session-root 只能和一个 --codex-home 一起使用")
        # 中文注释：保留天数先拒绝布尔等非数值类型，范围交给 MonitorConfig 校验。
        for name in ("usage_retention_days", "session_retention_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} 必须是数值")
        MonitorConfig(
            codex_path=self.codex_path,
            scan_interval=self.scan_interval,
            reconcile_interval=self.reconcile_interval,
            quota_interval=self.quota_interval,
            auto_resume=False,
            dashboard=self.dashboard,
            dashboard_host=self.dashboard_host,
            dashboard_port=self.dashboard_port,
            budget_usd=self.budget_usd,
            upload_burst_warn_mb=self.upload_burst_warn_mb,
            upload_burst_danger_mb=self.upload_burst_danger_mb,
            upload_window_warn_mb=self.upload_window_warn_mb,
            upload_window_danger_mb=self.upload_window_danger_mb,
            alert_retention_days=self.alert_retention_days,
            usage_retention_days=self.usage_retention_days,
            session_retention_days=self.session_retention_days,
            session_turn_warn=self.session_turn_warn,
            session_context_warn_tokens=self.session_context_warn_tokens,
            disk_warn_gb=self.disk_warn_gb,
            disk_total_warn_gb=self.disk_total_warn_gb,
        )

    @property
    def config_path(self) -> Path:
        """返回此服务配置的固定保存位置。"""

        return self.state_dir / "service.json"

    def to_dict(self) -> dict[str, object]:
        """转换为不包含认证令牌的 JSON 字典。"""

        return {
            "schema_version": self.SCHEMA_VERSION,
            "state_dir": str(self.state_dir),
            "codex_homes": [str(path) for path in self.codex_homes],
            "session_root": (
                str(self.session_root) if self.session_root is not None else None
            ),
            "verbose": self.verbose,
            "codex_path": self.codex_path,
            "scan_interval": self.scan_interval,
            "reconcile_interval": self.reconcile_interval,
            "quota_interval": self.quota_interval,
            "dashboard": self.dashboard,
            "dashboard_host": self.dashboard_host,
            "dashboard_port": self.dashboard_port,
            "grok_homes": [str(path) for path in self.grok_homes],
            "kimi_homes": [str(path) for path in self.kimi_homes],
            "dsh_homes": [str(path) for path in self.dsh_homes],
            "commandcode_homes": [
                str(path) for path in self.commandcode_homes
            ],
            "claude_homes": [str(path) for path in self.claude_homes],
            "budget_usd": self.budget_usd,
            "upload_burst_warn_mb": self.upload_burst_warn_mb,
            "upload_burst_danger_mb": self.upload_burst_danger_mb,
            "upload_window_warn_mb": self.upload_window_warn_mb,
            "upload_window_danger_mb": self.upload_window_danger_mb,
            "alert_retention_days": self.alert_retention_days,
            "usage_retention_days": self.usage_retention_days,
            "session_retention_days": self.session_retention_days,
            "session_turn_warn": self.session_turn_warn,
            "session_context_warn_tokens": self.session_context_warn_tokens,
            "disk_warn_gb": self.disk_warn_gb,
            "disk_total_warn_gb": self.disk_total_warn_gb,
        }

    def save(self) -> None:
        """原子保存配置并限制为当前用户可读写。"""

        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)
        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        temporary_path = self.config_path.with_suffix(".json.tmp")
        temporary_path.write_text(f"{payload}\n", encoding="utf-8")
        temporary_path.chmod(0o600)
        temporary_path.replace(self.config_path)
        self.config_path.chmod(0o600)

    @classmethod
    def load(cls, path: Path) -> ServiceConfig:
        """从磁盘读取并严格校验服务配置。"""

        try:
            raw_payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ServiceError(
                f"服务配置不存在: {path}；请先运行 service install"
            ) from error
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ServiceError(f"无法读取服务配置 {path}: {error}") from error
        if not isinstance(raw_payload, Mapping):
            raise ServiceError("服务配置根节点必须是 JSON 对象")
        schema_version = _required_int(raw_payload, "schema_version")
        if schema_version != cls.SCHEMA_VERSION:
            raise ServiceError(f"不支持的服务配置版本: {schema_version}")
        homes_value = raw_payload.get("codex_homes")
        if not isinstance(homes_value, list):
            raise ServiceError("服务配置 codex_homes 必须是列表")
        codex_homes = tuple(
            _absolute_path(Path(_list_string(homes_value, index)))
            for index in range(len(homes_value))
        )
        session_root_value = raw_payload.get("session_root")
        if session_root_value is not None and not isinstance(
            session_root_value,
            str,
        ):
            raise ServiceError("服务配置 session_root 必须是字符串或 null")
        return cls(
            state_dir=_absolute_path(Path(_required_string(raw_payload, "state_dir"))),
            codex_homes=codex_homes,
            session_root=(
                _absolute_path(Path(session_root_value))
                if isinstance(session_root_value, str)
                else None
            ),
            verbose=_required_bool(raw_payload, "verbose"),
            codex_path=_required_string(raw_payload, "codex_path"),
            scan_interval=_required_float(raw_payload, "scan_interval"),
            reconcile_interval=_required_float(
                raw_payload,
                "reconcile_interval",
            ),
            quota_interval=_required_float(raw_payload, "quota_interval"),
            dashboard=_required_bool(raw_payload, "dashboard"),
            dashboard_host=_required_string(
                raw_payload,
                "dashboard_host",
            ),
            dashboard_port=_required_int(raw_payload, "dashboard_port"),
            grok_homes=_optional_path_tuple(raw_payload, "grok_homes"),
            kimi_homes=_optional_path_tuple(raw_payload, "kimi_homes"),
            dsh_homes=_optional_path_tuple(raw_payload, "dsh_homes"),
            commandcode_homes=_optional_path_tuple(
                raw_payload, "commandcode_homes"
            ),
            claude_homes=_optional_path_tuple(raw_payload, "claude_homes"),
            budget_usd=_optional_float(raw_payload, "budget_usd"),
            upload_burst_warn_mb=_optional_float(
                raw_payload, "upload_burst_warn_mb"
            )
            or 8.0,
            upload_burst_danger_mb=_optional_float(
                raw_payload, "upload_burst_danger_mb"
            )
            or 32.0,
            upload_window_warn_mb=_optional_float(
                raw_payload, "upload_window_warn_mb"
            )
            or 64.0,
            upload_window_danger_mb=_optional_float(
                raw_payload, "upload_window_danger_mb"
            )
            or 256.0,
            alert_retention_days=_optional_float(
                raw_payload, "alert_retention_days"
            )
            or DEFAULT_ALERT_RETENTION_DAYS,
            usage_retention_days=_optional_float(
                raw_payload, "usage_retention_days"
            )
            or DEFAULT_USAGE_RETENTION_DAYS,
            session_retention_days=_optional_float(
                raw_payload, "session_retention_days"
            )
            or DEFAULT_SESSION_RETENTION_DAYS,
            session_turn_warn=int(
                _optional_float(raw_payload, "session_turn_warn")
                or DEFAULT_SESSION_TURN_WARN
            ),
            session_context_warn_tokens=int(
                _optional_float(raw_payload, "session_context_warn_tokens")
                or DEFAULT_SESSION_CONTEXT_WARN_TOKENS
            ),
            disk_warn_gb=_optional_float(raw_payload, "disk_warn_gb")
            or DEFAULT_SINGLE_WARN_GIB,
            disk_total_warn_gb=_optional_float(
                raw_payload, "disk_total_warn_gb"
            )
            or DEFAULT_TOTAL_WARN_GIB,
        )

    def build_monitor(self) -> MultiAccountMonitor:
        """根据持久配置创建多账号监控器。"""

        # 中文注释：持久化的空元组表示「未配置」，转成 None 才能让旧
        # service.json 保持自动探测语义；显式禁用只由 Web 覆盖配置表达。
        # 扫描目录损坏的 scan-dirs.json 会让这里的 ScanDirsError 直接抛出，
        # daemon 启动不应静默忽略配置错误。
        cli_homes: dict[str, tuple[Path, ...] | None] = {
            "codex": self.codex_homes or None,
            "claude": self.claude_homes or None,
            "commandcode": self.commandcode_homes or None,
            "dsh": self.dsh_homes or None,
            "grok": self.grok_homes or None,
            "kimi": self.kimi_homes or None,
        }
        controller = ScanDirsController(self.state_dir, cli_homes)
        effective_dirs = controller.effective()
        codex_state = effective_dirs.state("codex")
        accounts = build_account_specs(
            homes=(None if codex_state.source == "auto" else codex_state.effective),
            state_dir=self.state_dir,
            session_root=self.session_root,
        )
        monitor_config = MonitorConfig(
            codex_path=self.codex_path,
            scan_interval=self.scan_interval,
            reconcile_interval=self.reconcile_interval,
            quota_interval=self.quota_interval,
            dashboard=self.dashboard,
            dashboard_host=self.dashboard_host,
            dashboard_port=self.dashboard_port,
            budget_usd=self.budget_usd,
            upload_burst_warn_mb=self.upload_burst_warn_mb,
            upload_burst_danger_mb=self.upload_burst_danger_mb,
            upload_window_warn_mb=self.upload_window_warn_mb,
            upload_window_danger_mb=self.upload_window_danger_mb,
            alert_retention_days=self.alert_retention_days,
            usage_retention_days=self.usage_retention_days,
            session_retention_days=self.session_retention_days,
            session_turn_warn=self.session_turn_warn,
            session_context_warn_tokens=self.session_context_warn_tokens,
            disk_warn_gb=self.disk_warn_gb,
            disk_total_warn_gb=self.disk_total_warn_gb,
        )
        return MultiAccountMonitor(
            accounts=accounts,
            state_dir=self.state_dir,
            config=monitor_config,
            grok_homes=_daemon_homes(effective_dirs.state("grok")),
            kimi_homes=_daemon_homes(effective_dirs.state("kimi")),
            dsh_homes=_daemon_homes(effective_dirs.state("dsh")),
            commandcode_homes=_daemon_homes(effective_dirs.state("commandcode")),
            claude_homes=_daemon_homes(effective_dirs.state("claude")),
            scan_dirs_controller=controller,
        )


def _daemon_homes(state: ProviderDirsState) -> tuple[Path, ...] | None:
    """自动探测来源传 None 交给监控器探测，其余来源按生效列表原样传入。"""

    return None if state.source == "auto" else state.effective


class UserServiceManager:
    """安装和控制当前用户的 systemd 服务。"""

    def __init__(
        self,
        state_dir: Path,
        unit_dir: Path | None = None,
        python_executable: Path | None = None,
    ) -> None:
        """创建服务管理器，并规范化所有运行路径。"""

        self.state_dir = _absolute_path(state_dir)
        self.unit_dir = _absolute_path(unit_dir or _default_unit_dir())
        self.unit_path = self.unit_dir / SERVICE_NAME
        self.python_executable = _absolute_path(
            python_executable or Path(sys.executable)
        )

    @property
    def config_path(self) -> Path:
        """返回 systemd 服务读取的配置文件。"""

        return self.state_dir / "service.json"

    def install(self, config: ServiceConfig, start: bool = True) -> None:
        """写入配置和用户单元，并选择是否立即启用运行。"""

        if _absolute_path(config.state_dir) != self.state_dir:
            raise ServiceError("服务配置与管理器的 state_dir 不一致")
        if not self.python_executable.is_file():
            raise ServiceError(f"Python 解释器不存在: {self.python_executable}")
        config.save()
        self.unit_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        unit_content = self.render_unit()
        temporary_path = self.unit_path.with_suffix(".service.tmp")
        temporary_path.write_text(unit_content, encoding="utf-8")
        temporary_path.chmod(0o644)
        temporary_path.replace(self.unit_path)
        self.unit_path.chmod(0o644)
        self._systemctl("daemon-reload")
        if start:
            # 中文注释：install 也用于更新现有配置，必须 restart 才能让
            # 已经运行的服务重新读取 service.json。
            self._systemctl("enable", SERVICE_NAME)
            self._systemctl("restart", SERVICE_NAME)
        else:
            self._systemctl("enable", SERVICE_NAME)

    def render_unit(self) -> str:
        """生成只包含稳定启动参数的 systemd 用户单元。"""

        arguments = (
            str(self.python_executable),
            "-m",
            "a_token_monitor",
            "--state-dir",
            str(self.state_dir),
            "service",
            "run",
        )
        exec_start = " ".join(_systemd_quote(item) for item in arguments)
        return (
            "[Unit]\n"
            "Description=Token Monitor background daemon\n"
            "StartLimitIntervalSec=60\n"
            "StartLimitBurst=5\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={exec_start}\n"
            "Environment=PYTHONUNBUFFERED=1\n"
            "UMask=0077\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "TimeoutStopSec=30\n"
            "KillMode=control-group\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def start(self) -> None:
        """启动已安装服务。"""

        self._systemctl("start", SERVICE_NAME)

    def stop(self) -> None:
        """停止服务但保留开机启用状态。"""

        self._systemctl("stop", SERVICE_NAME)

    def restart(self) -> None:
        """重新加载单元并重启服务。"""

        self._systemctl("daemon-reload")
        self._systemctl("restart", SERVICE_NAME)

    @property
    def active_service_name(self) -> str:
        """返回当前生效的单元名，改名后仍能查询旧的单元。"""

        if self.unit_path.exists():
            return SERVICE_NAME
        for legacy_name in LEGACY_SERVICE_NAMES:
            if (self.unit_dir / legacy_name).exists():
                return legacy_name
        return SERVICE_NAME

    def status(self) -> int:
        """显示 systemd 状态并返回 systemctl 状态码。"""

        return self._systemctl(
            "status",
            self.active_service_name,
            "--no-pager",
            check=False,
        )

    def logs(self, lines: int, follow: bool) -> int:
        """显示服务日志；follow 为真时持续跟踪。"""

        if lines <= 0:
            raise ValueError("日志行数必须大于 0")
        command = [
            "journalctl",
            "--user",
            "--unit",
            self.active_service_name,
            "--lines",
            str(lines),
            "--no-pager",
        ]
        if follow:
            command.append("--follow")
        try:
            return _run_command(command, check=False)
        except KeyboardInterrupt:
            # 中文注释：用户只是在退出日志跟踪，后台服务本身不受影响。
            return 130

    def uninstall(self) -> None:
        """停止并移除服务定义；监控数据库和历史记录继续保留。"""

        if not self.unit_path.exists():
            for legacy_name in LEGACY_SERVICE_NAMES:
                legacy_unit = self.unit_dir / legacy_name
                if not legacy_unit.exists():
                    continue
                # 改名前的安装：停掉并移除旧单元，避免遗留后台进程。
                self._systemctl("disable", "--now", legacy_name, check=False)
                try:
                    legacy_unit.unlink(missing_ok=True)
                except OSError as error:
                    raise ServiceError(f"无法移除旧版后台服务文件: {error}") from error
                self._systemctl("daemon-reload")
                self._systemctl("reset-failed", legacy_name, check=False)
                return
        self._systemctl(
            "disable",
            "--now",
            SERVICE_NAME,
            check=False,
        )
        try:
            self.unit_path.unlink(missing_ok=True)
            self.config_path.unlink(missing_ok=True)
        except OSError as error:
            raise ServiceError(f"无法移除后台服务文件: {error}") from error
        self._systemctl("daemon-reload")
        self._systemctl("reset-failed", SERVICE_NAME, check=False)

    def _systemctl(self, *arguments: str, check: bool = True) -> int:
        """执行当前用户的 systemctl，并统一错误语义。"""

        return _run_command(
            ["systemctl", "--user", *arguments],
            check=check,
        )


def resolve_executable(command: str) -> Path:
    """把 CLI 中的命令解析为 systemd 可稳定使用的绝对路径。"""

    candidate = Path(command).expanduser()
    if candidate.parent != Path("."):
        resolved = _absolute_path(candidate)
        if not resolved.is_file():
            raise ServiceError(f"可执行文件不存在: {resolved}")
        return resolved
    located = shutil.which(command)
    if located is None:
        raise ServiceError(f"找不到可执行文件: {command}")
    return _absolute_path(Path(located))


def run_saved_service(state_dir: Path) -> int:
    """读取持久配置并运行 daemon，供 systemd 的 ExecStart 调用。"""

    config_path = _absolute_path(state_dir) / "service.json"
    config = ServiceConfig.load(config_path)
    logging.getLogger().setLevel(logging.DEBUG if config.verbose else logging.INFO)
    logging.getLogger(__name__).info("读取后台服务配置: %s", config_path)
    monitor = config.build_monitor()
    previous_handler = signal.getsignal(signal.SIGTERM)

    def request_stop(signum: int, frame: FrameType | None) -> None:
        """把 systemd 的停止信号转换成监控循环的安全停止请求。"""

        del signum, frame
        logging.getLogger(__name__).info("收到后台服务停止信号")
        monitor.request_stop()

    signal.signal(signal.SIGTERM, request_stop)
    try:
        return monitor.run()
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def _default_unit_dir() -> Path:
    """按照 XDG 规范返回当前用户的 systemd 单元目录。"""

    configured = os.environ.get("XDG_CONFIG_HOME")
    config_home = (
        Path(configured).expanduser() if configured else Path.home() / ".config"
    )
    return config_home / "systemd" / "user"


def _run_command(command: Sequence[str], check: bool) -> int:
    """运行不经过 shell 的服务管理命令。"""

    try:
        result = subprocess.run(list(command), check=False)
    except OSError as error:
        raise ServiceError(f"无法执行 {command[0]}: {error}") from error
    if check and result.returncode != 0:
        joined = " ".join(command)
        raise ServiceError(f"命令执行失败（退出码 {result.returncode}）: {joined}")
    return result.returncode


def _systemd_quote(value: str) -> str:
    """按 systemd.exec 规则转义单个参数并阻止变量或 specifier 展开。"""

    if "\x00" in value or "\n" in value or "\r" in value:
        raise ServiceError("systemd 参数不能包含换行或空字符")
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "$$")
        .replace("%", "%%")
    )
    return f'"{escaped}"'


def _absolute_path(path: Path) -> Path:
    """返回展开后的稳定绝对路径。"""

    expanded = path.expanduser()
    try:
        return expanded.resolve()
    except (OSError, RuntimeError):
        return expanded.absolute()


def _optional_path_tuple(
    payload: Mapping[str, Any],
    key: str,
) -> tuple[Path, ...]:
    """读取可选路径列表；缺省或 null 视为空。"""

    value = payload.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ServiceError(f"服务配置 {key} 必须是字符串列表")
    paths: list[Path] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ServiceError(f"服务配置 {key}[{index}] 必须是非空字符串")
        paths.append(_absolute_path(Path(item)))
    return tuple(paths)


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    """读取非空字符串字段。"""

    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(f"服务配置 {key} 必须是非空字符串")
    return value


def _required_bool(payload: Mapping[str, Any], key: str) -> bool:
    """读取严格布尔字段。"""

    value = payload.get(key)
    if not isinstance(value, bool):
        raise ServiceError(f"服务配置 {key} 必须是布尔值")
    return value


def _required_float(payload: Mapping[str, Any], key: str) -> float:
    """读取不接受布尔值的数值字段。"""

    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ServiceError(f"服务配置 {key} 必须是数值")
    return float(value)


def _optional_float(payload: Mapping[str, Any], key: str) -> float | None:
    """读取可选数值字段；缺省或 null 视为未配置。"""

    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ServiceError(f"服务配置 {key} 必须是数值")
    return float(value)


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    """读取不接受布尔值的整数字段。"""

    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServiceError(f"服务配置 {key} 必须是整数")
    return value


def _list_string(values: list[Any], index: int) -> str:
    """读取字符串列表中的一个非空元素。"""

    value = values[index]
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(f"服务配置 codex_homes[{index}] 必须是非空字符串")
    return value


# 中文注释：macOS 用 launchd 的 Label 作为 plist 文件名和 launchctl 目标名。
LAUNCHD_LABEL = "com.a-token-monitor.daemon"


class LaunchdServiceManager:
    """安装和控制当前用户的 macOS LaunchAgent 服务。"""

    def __init__(
        self,
        state_dir: Path,
        unit_dir: Path | None = None,
        python_executable: Path | None = None,
        label: str = LAUNCHD_LABEL,
        domain: str | None = None,
    ) -> None:
        """创建 launchd 服务管理器，并规范化所有运行路径。"""

        self.state_dir = _absolute_path(state_dir)
        self.unit_dir = _absolute_path(unit_dir or _default_launchd_unit_dir())
        self.label = label
        self.domain = domain or f"gui/{_current_uid()}"
        self.plist_path = self.unit_dir / f"{label}.plist"
        self.log_path = self.state_dir / "launchd.log"
        self.python_executable = _absolute_path(
            python_executable or Path(sys.executable)
        )

    @property
    def config_path(self) -> Path:
        """返回 LaunchAgent 读取的配置文件。"""

        return self.state_dir / "service.json"

    @property
    def service_target(self) -> str:
        """返回 launchctl 使用的 domain/label 目标名。"""

        return f"{self.domain}/{self.label}"

    def install(self, config: ServiceConfig, start: bool = True) -> None:
        """写入配置和 LaunchAgent plist，并选择是否立即启动。"""

        if _absolute_path(config.state_dir) != self.state_dir:
            raise ServiceError("服务配置与管理器的 state_dir 不一致")
        config.save()
        self.unit_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        plist_content = self.render_plist()
        temporary_path = self.plist_path.with_suffix(".plist.tmp")
        temporary_path.write_text(plist_content, encoding="utf-8")
        temporary_path.chmod(0o600)
        temporary_path.replace(self.plist_path)
        self.plist_path.chmod(0o600)
        # 中文注释：重复安装时先卸载旧服务，bootstrap 才会重新读取
        # plist；首次安装时 bootout 必然失败，因此忽略它的退出码。
        self._launchctl("bootout", self.service_target, check=False)
        self._launchctl("bootstrap", self.domain, str(self.plist_path))
        if start:
            self._launchctl("kickstart", "-k", self.service_target)

    def render_plist(self) -> str:
        """生成只包含稳定启动参数的 LaunchAgent plist。"""

        arguments = (
            str(self.python_executable),
            "-m",
            "a_token_monitor",
            "--state-dir",
            str(self.state_dir),
            "service",
            "run",
        )
        payload: dict[str, Any] = {
            "Label": self.label,
            "ProgramArguments": list(arguments),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "WorkingDirectory": str(self.state_dir),
            "StandardOutPath": str(self.log_path),
            "StandardErrorPath": str(self.log_path),
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
            "ProcessType": "Background",
        }
        # 中文注释：用 plistlib 生成合法 XML，避免手写转义规则。
        return plistlib.dumps(payload, sort_keys=False).decode("utf-8")

    def start(self) -> None:
        """启动已安装服务。"""

        self._launchctl("kickstart", "-k", self.service_target)

    def stop(self) -> None:
        """停止服务但保留已安装的 plist。"""

        status = self._launchctl(
            "kill",
            "SIGTERM",
            self.service_target,
            check=False,
        )
        if status != 0:
            # 中文注释：服务未运行时 kill 会失败，退回 bootout 保证停止生效。
            self._launchctl("bootout", self.service_target, check=False)

    def restart(self) -> None:
        """重新启动服务，让进程重新读取 service.json。"""

        self._launchctl("kickstart", "-k", self.service_target)

    def status(self) -> int:
        """显示 launchd 状态并返回 launchctl 退出码，未安装时为非 0。"""

        return self._launchctl("print", self.service_target, check=False)

    def logs(self, lines: int = 50, follow: bool = False) -> int:
        """显示服务日志；follow 为真时用 tail 持续跟踪。"""

        if lines <= 0:
            raise ValueError("日志行数必须大于 0")
        if follow:
            try:
                return _run_command(
                    ["tail", "-n", str(lines), "-f", str(self.log_path)],
                    check=False,
                )
            except KeyboardInterrupt:
                # 中文注释：用户只是在退出日志跟踪，后台服务不受影响。
                return 130
        if not self.log_path.exists():
            print(f"日志文件不存在: {self.log_path}；服务还没有写入日志")
            return 0
        try:
            with self.log_path.open(
                "r",
                encoding="utf-8",
                errors="replace",
            ) as handle:
                for line in deque(handle, maxlen=lines):
                    print(line, end="")
        except OSError as error:
            raise ServiceError(
                f"无法读取日志文件 {self.log_path}: {error}"
            ) from error
        return 0

    def uninstall(self) -> None:
        """停止并移除 LaunchAgent 定义；监控数据继续保留。"""

        self._launchctl("bootout", self.service_target, check=False)
        try:
            self.plist_path.unlink(missing_ok=True)
            # 中文注释：与 systemd 版一致，同时删除 service.json。
            self.config_path.unlink(missing_ok=True)
        except OSError as error:
            raise ServiceError(f"无法移除后台服务文件: {error}") from error

    def _launchctl(self, *arguments: str, check: bool = True) -> int:
        """执行 launchctl，并统一错误语义。"""

        return _run_command(["launchctl", *arguments], check=check)


def create_service_manager(
    state_dir: Path,
    unit_dir: Path | None = None,
    python_executable: Path | None = None,
    platform: str | None = None,
) -> UserServiceManager | LaunchdServiceManager | TaskSchedulerServiceManager:
    """按平台创建后台服务管理器：macOS 用 launchd，Windows 用计划任务。"""

    resolved_platform = sys.platform if platform is None else platform
    if resolved_platform == "darwin":
        return LaunchdServiceManager(
            state_dir=state_dir,
            unit_dir=unit_dir,
            python_executable=python_executable,
        )
    if resolved_platform in ("win32", "windows"):
        return TaskSchedulerServiceManager(
            state_dir=state_dir,
            unit_dir=unit_dir,
            python_executable=python_executable,
        )
    return UserServiceManager(
        state_dir=state_dir,
        unit_dir=unit_dir,
        python_executable=python_executable,
    )


def launchd_available() -> bool:
    """判断当前系统是否支持 launchd 用户服务。"""

    return sys.platform == "darwin" and shutil.which("launchctl") is not None


def windows_service_available() -> bool:
    """判断当前系统是否支持 Windows 计划任务服务。"""

    return sys.platform == "win32" and shutil.which("schtasks") is not None


def _default_launchd_unit_dir() -> Path:
    """返回当前用户的 LaunchAgents 目录。"""

    return Path.home() / "Library" / "LaunchAgents"


def _current_uid() -> int:
    """返回当前用户 ID；缺少 getuid 的平台回退到 0。"""

    getuid = getattr(os, "getuid", None)
    return int(getuid()) if callable(getuid) else 0


# 中文注释：Windows 用计划任务（Task Scheduler）代替 systemd / launchd。
# 注册的 XML 必须是 UTF-16（schtasks 对中文路径尤其挑剔），日志由 cmd.exe
# 的重定向直接写入 daemon.log，因此不需要额外的日志轮询进程。
TASK_SCHEDULER_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>a-token-monitor</Author>
    <Description>{description}</Description>
    <URI>\\{task_name}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
{principal_user}      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{working_directory}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


class TaskSchedulerServiceManager:
    """安装和控制当前用户的 Windows 计划任务服务。"""

    def __init__(
        self,
        state_dir: Path,
        unit_dir: Path | None = None,
        python_executable: Path | None = None,
        task_name: str = "ATokenMonitor",
        user: str | None = None,
    ) -> None:
        """创建计划任务管理器，并规范化所有运行路径。

        unit_dir 只为与其它平台的管理器保持相同构造签名；计划任务的 XML
        固定放在状态目录，因此这里忽略它。``user`` 缺省表示“当前登录用户”，
        此时 XML 里不写 ``UserId`` 元素——空元素在部分 Windows 版本上会让
        ``schtasks /Create /XML`` 直接报格式错误。
        """

        self.state_dir = _absolute_path(state_dir)
        self.task_name = task_name
        self.user = user.strip() if user else None
        self.task_path = self.state_dir / "a-token-monitor-task.xml"
        self.log_path = self.state_dir / "daemon.log"
        self.python_executable = _absolute_path(
            python_executable or Path(sys.executable)
        )

    @property
    def config_path(self) -> Path:
        """返回计划任务启动的 daemon 读取的配置文件。"""

        return self.state_dir / "service.json"

    def render_task_xml(self) -> str:
        """生成登录时自动启动、失败重启且不限时的计划任务 XML。"""

        principal_user = (
            f"      <UserId>{_xml_escape(self.user)}</UserId>\n"
            if self.user
            else ""
        )
        return TASK_SCHEDULER_XML.format(
            description=_xml_escape("Token Monitor 后台额度监控服务"),
            task_name=_xml_escape(self.task_name),
            arguments=_xml_escape(self.render_arguments()),
            working_directory=_xml_escape(str(self.state_dir)),
            principal_user=principal_user,
        )

    def render_arguments(self) -> str:
        """生成 cmd.exe /c 使用的嵌套引号参数串。"""

        # 中文注释：cmd.exe /c 会把最外层引号当作定界符，因此命令整体再包一
        # 层引号，内部路径各自单独加引号，右尖括号重定向到 daemon.log。
        return (
            f'/c ""{self.python_executable}"'
            " -m a_token_monitor"
            f' --state-dir "{self.state_dir}"'
            " service run"
            f' >> "{self.log_path}" 2>&1"'
        )

    def install(self, config: ServiceConfig, start: bool = True) -> None:
        """写入配置和计划任务 XML，并选择是否立即启动。"""

        if _absolute_path(config.state_dir) != self.state_dir:
            raise ServiceError("服务配置与管理器的 state_dir 不一致")
        config.save()
        self.task_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.task_path.write_text(self.render_task_xml(), encoding="utf-16")
        self._schtasks(
            "/Create",
            "/TN",
            self.task_name,
            "/XML",
            str(self.task_path),
            "/F",
        )
        if start:
            self._schtasks("/Run", "/TN", self.task_name)

    def start(self) -> None:
        """启动已注册的计划任务。"""

        self._schtasks("/Run", "/TN", self.task_name)

    def stop(self) -> None:
        """停止计划任务但保留注册信息。"""

        self._schtasks("/End", "/TN", self.task_name, check=False)

    def restart(self) -> None:
        """先结束再启动计划任务，让进程重新读取 service.json。"""

        self._schtasks("/End", "/TN", self.task_name, check=False)
        self._schtasks("/Run", "/TN", self.task_name)

    def status(self) -> int:
        """返回 schtasks 查询退出码，任务未注册时为非 0 且不抛异常。"""

        return self._schtasks("/Query", "/TN", self.task_name, check=False)

    def logs(self, lines: int = 50, follow: bool = False) -> int:
        """显示 daemon 日志；follow 为真时持续跟踪新增内容。"""

        if lines <= 0:
            raise ValueError("日志行数必须大于 0")
        if not follow:
            self._print_log_tail(lines)
            return 0
        try:
            position = self._print_log_tail(lines)
            while True:
                time.sleep(0.5)
                try:
                    size = self.log_path.stat().st_size
                except OSError:
                    # 中文注释：日志还没创建时继续等待，不当作错误。
                    position = 0
                    continue
                if size < position:
                    # 中文注释：日志被截断或轮转，从文件开头重新读取。
                    position = 0
                if size == position:
                    continue
                with self.log_path.open("rb") as handle:
                    handle.seek(position)
                    chunk = handle.read()
                    position = handle.tell()
                sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                sys.stdout.flush()
        except KeyboardInterrupt:
            # 中文注释：用户只是在退出日志跟踪，计划任务本身不受影响。
            return 0

    def uninstall(self) -> None:
        """注销计划任务并删除本地服务文件；监控数据继续保留。"""

        self._schtasks("/Delete", "/TN", self.task_name, "/F", check=False)
        try:
            self.task_path.unlink(missing_ok=True)
            # 中文注释：与 systemd / launchd 版一致，同时删除 service.json。
            self.config_path.unlink(missing_ok=True)
        except OSError as error:
            raise ServiceError(f"无法移除后台服务文件: {error}") from error

    def _print_log_tail(self, lines: int) -> int:
        """打印日志最后若干行，并返回后续读取的字节偏移。"""

        try:
            position = self.log_path.stat().st_size
        except FileNotFoundError:
            print(f"日志文件不存在: {self.log_path}；服务还没有写入日志")
            return 0
        except OSError as error:
            raise ServiceError(
                f"无法读取日志文件 {self.log_path}: {error}"
            ) from error
        try:
            with self.log_path.open(
                "r",
                encoding="utf-8",
                errors="replace",
            ) as handle:
                for line in deque(handle, maxlen=lines):
                    print(line, end="")
        except OSError as error:
            raise ServiceError(
                f"无法读取日志文件 {self.log_path}: {error}"
            ) from error
        return position

    def _schtasks(self, *arguments: str, check: bool = True) -> int:
        """执行 schtasks，并统一错误语义。"""

        return _run_command(["schtasks", *arguments], check=check)


def _xml_escape(value: str) -> str:
    """转义计划任务 XML 文本节点和属性里不能直接出现的字符。"""

    return escape(value, {'"': "&quot;", "'": "&apos;"})
