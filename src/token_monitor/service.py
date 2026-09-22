"""通过 systemd 用户服务长期运行额度监控器。

服务配置单独保存为权限受限的 JSON。systemd 单元只携带 Python 解释器和
状态目录两个稳定参数。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, ClassVar

from .accounts import build_account_specs
from .monitor import MonitorConfig
from .multi_account import MultiAccountMonitor


SERVICE_NAME = "codex-reset-monitor.service"


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
    budget_usd: float | None = None
    upload_burst_warn_mb: float = 8.0
    upload_burst_danger_mb: float = 32.0
    upload_window_warn_mb: float = 64.0
    upload_window_danger_mb: float = 256.0

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
        if self.session_root is not None:
            object.__setattr__(
                self,
                "session_root",
                _absolute_path(self.session_root),
            )
        if not self.codex_homes:
            raise ValueError("后台服务至少需要一个 CODEX_HOME")
        if not self.codex_path.strip():
            raise ValueError("Codex 可执行文件不能为空")
        if self.session_root is not None and len(self.codex_homes) != 1:
            raise ValueError("--session-root 只能和一个 --codex-home 一起使用")
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
            "budget_usd": self.budget_usd,
            "upload_burst_warn_mb": self.upload_burst_warn_mb,
            "upload_burst_danger_mb": self.upload_burst_danger_mb,
            "upload_window_warn_mb": self.upload_window_warn_mb,
            "upload_window_danger_mb": self.upload_window_danger_mb,
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
        if not isinstance(homes_value, list) or not homes_value:
            raise ServiceError("服务配置 codex_homes 必须是非空列表")
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
        )

    def build_monitor(self) -> MultiAccountMonitor:
        """根据持久配置创建多账号监控器。"""

        accounts = build_account_specs(
            homes=self.codex_homes,
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
        )
        return MultiAccountMonitor(
            accounts=accounts,
            state_dir=self.state_dir,
            config=monitor_config,
            grok_homes=self.grok_homes,
            kimi_homes=self.kimi_homes,
            dsh_homes=self.dsh_homes,
        )


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
            "codex_reset_monitor",
            "--state-dir",
            str(self.state_dir),
            "service",
            "run",
        )
        exec_start = " ".join(_systemd_quote(item) for item in arguments)
        return (
            "[Unit]\n"
            "Description=Codex Reset Monitor background daemon\n"
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

    def status(self) -> int:
        """显示 systemd 状态并返回 systemctl 状态码。"""

        return self._systemctl(
            "status",
            SERVICE_NAME,
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
            SERVICE_NAME,
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
