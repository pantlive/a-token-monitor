"""Dashboard HTTP 服务：配置、启动与账号集合热替换。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Mapping, Sequence

from ..alerts import (
    TrafficAlertStore,
)
from ..housekeeping import (
    HousekeepingMonitor,
)
from ..claude import (
    resolve_claude_homes,
)
from ..commandcode import (
    resolve_commandcode_homes,
)
from ..dsh import (
    resolve_dsh_homes,
)
from ..grok import (
    resolve_grok_homes,
)
from ..health import HealthTracker
from ..kimi import (
    resolve_kimi_homes,
)
from ..local_agents import (
    resolve_aider_homes,
    resolve_cursor_homes,
    resolve_gemini_homes,
    resolve_opencode_homes,
    resolve_qwen_homes,
)
from ..registry import MultiSessionRegistry
from ..retention import HistoryDataManager, RetentionController
from ..scan_dirs import ScanDirsController
from ..traffic import TrafficMonitor
from ..usage import (
    SessionSwitchThresholds,
    UsageAggregator,
)
from .handler import (
    _make_handler,
)
from .state import (
    _AccountSet,
)


@dataclass(frozen=True)
class DashboardConfig:
    """Dashboard HTTP 服务配置。"""

    host: str = "127.0.0.1"
    port: int = 8765
    budget_usd: float | None = None
    alert_context_content: bool = False

    def __post_init__(self) -> None:
        """校验监听地址和端口。"""

        if not self.host.strip():
            raise ValueError("dashboard host 不能为空")
        if not 0 <= self.port <= 65535:
            raise ValueError("dashboard port 必须在 0 到 65535 之间")
        if self.alert_context_content and self.host not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise ValueError("内容摘要只能在本机监听地址启用")
        if self.budget_usd is not None and self.budget_usd <= 0:
            raise ValueError("budget_usd 必须大于 0")


class _DashboardHTTPServer(ThreadingHTTPServer):
    """允许快速重启且不让请求线程阻塞主监控退出的 HTTP 服务。"""

    allow_reuse_address = True
    daemon_threads = True


class DashboardServer:
    """提供 Dashboard 状态、用量数据和告警历史的本地 HTTP 服务。"""

    def __init__(
        self,
        registry: MultiSessionRegistry | None = None,
        config: DashboardConfig | None = None,
        logger: logging.Logger | None = None,
        registries: Mapping[str, MultiSessionRegistry] | None = None,
        account_metadata: Mapping[str, Mapping[str, str | None]] | None = None,
        usage_aggregator: UsageAggregator | None = None,
        grok_homes: Sequence[Path] | None = None,
        kimi_homes: Sequence[Path] | None = None,
        dsh_homes: Sequence[Path] | None = None,
        commandcode_homes: Sequence[Path] | None = None,
        claude_homes: Sequence[Path] | None = None,
        opencode_homes: Sequence[Path] | None = None,
        cursor_homes: Sequence[Path] | None = None,
        gemini_homes: Sequence[Path] | None = None,
        qwen_homes: Sequence[Path] | None = None,
        aider_homes: Sequence[Path] | None = None,
        traffic_monitor: TrafficMonitor | None = None,
        alert_store: TrafficAlertStore | None = None,
        housekeeping: HousekeepingMonitor | None = None,
        session_thresholds: SessionSwitchThresholds | None = None,
        scan_dirs: ScanDirsController | None = None,
        health: HealthTracker | None = None,
        history: HistoryDataManager | None = None,
        retention: RetentionController | None = None,
    ) -> None:
        if registries is not None and registry is not None:
            raise ValueError("registry 和 registries 只能传入一个")
        if registry is not None:
            registries = {"codex": registry}
        # 中文注释：没有 Codex 账号时允许空注册表，Dashboard 仍然展示
        # Grok / Kimi / DSH / Claude Code / Command Code 的状态。
        self.registries = dict(registries or {})
        self.account_metadata = dict(account_metadata or {})
        self.registry = next(iter(self.registries.values()), None)
        self.config = config or DashboardConfig()
        self.logger = logger or logging.getLogger(__name__)
        self.grok_homes = resolve_grok_homes(grok_homes)
        self.kimi_homes = resolve_kimi_homes(kimi_homes)
        self.dsh_homes = resolve_dsh_homes(dsh_homes)
        self.commandcode_homes = resolve_commandcode_homes(commandcode_homes)
        self.claude_homes = resolve_claude_homes(claude_homes)
        self.opencode_homes = resolve_opencode_homes(opencode_homes)
        self.cursor_homes = resolve_cursor_homes(cursor_homes)
        self.gemini_homes = resolve_gemini_homes(gemini_homes)
        self.qwen_homes = resolve_qwen_homes(qwen_homes)
        self.aider_homes = resolve_aider_homes(aider_homes)
        self.traffic_monitor = traffic_monitor
        self.alert_store = alert_store
        self.housekeeping = housekeeping
        self.scan_dirs = scan_dirs
        self.health = health
        self.history = history
        self.retention = retention
        self.session_thresholds = session_thresholds or SessionSwitchThresholds()
        self.usage_aggregator = usage_aggregator or UsageAggregator(
            grok_homes=self.grok_homes,
            kimi_homes=self.kimi_homes,
            dsh_homes=self.dsh_homes,
            claude_homes=self.claude_homes,
            commandcode_homes=self.commandcode_homes,
            opencode_homes=self.opencode_homes,
            cursor_homes=self.cursor_homes,
            gemini_homes=self.gemini_homes,
            qwen_homes=self.qwen_homes,
            aider_homes=self.aider_homes,
        )
        # 中文注释：handler 闭包只持有这个容器；扫描目录变化导致账号增减时
        # 由 update_accounts 热替换内容，无需重启 HTTP 服务。
        self._accounts = _AccountSet(self.registries, self.account_metadata)
        self._server: _DashboardHTTPServer | None = None
        self._thread: Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        """返回实际监听地址；端口为 0 时返回系统分配的端口。"""

        if self._server is None:
            return self.config.host, self.config.port
        raw_host, raw_port = self._server.server_address[:2]
        return str(raw_host), int(raw_port)

    def update_accounts(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        account_metadata: Mapping[str, Mapping[str, str | None]] | None,
    ) -> None:
        """热替换注册表与账号元数据；扫描目录调整后账号集合会随之变化。"""

        self.registries = dict(registries)
        self.account_metadata = dict(account_metadata or {})
        self.registry = next(iter(self.registries.values()), None)
        self._accounts.update(self.registries, self.account_metadata)

    def start(self) -> None:
        """绑定地址并启动 Dashboard 请求线程。"""

        if self._server is not None:
            return
        handler = _make_handler(
            self._accounts,
            self.logger,
            self.usage_aggregator,
            self.grok_homes,
            self.kimi_homes,
            self.dsh_homes,
            self.commandcode_homes,
            self.claude_homes,
            self.opencode_homes,
            self.cursor_homes,
            self.gemini_homes,
            self.qwen_homes,
            self.aider_homes,
            budget_usd=self.config.budget_usd,
            alert_context_content=self.config.alert_context_content,
            traffic_monitor=self.traffic_monitor,
            alert_store=self.alert_store,
            housekeeping=self.housekeeping,
            session_thresholds=self.session_thresholds,
            scan_dirs=self.scan_dirs,
            health=self.health,
            history=self.history,
            retention=self.retention,
        )
        server = _DashboardHTTPServer(
            (self.config.host, self.config.port),
            handler,
        )
        self._server = server
        self._thread = Thread(
            target=server.serve_forever,
            name="a-token-monitor-dashboard",
            daemon=True,
        )
        self._thread.start()

    def update_homes(
        self,
        *,
        grok_homes: Sequence[Path],
        kimi_homes: Sequence[Path],
        dsh_homes: Sequence[Path],
        commandcode_homes: Sequence[Path],
        claude_homes: Sequence[Path],
        opencode_homes: Sequence[Path] | None = None,
        cursor_homes: Sequence[Path] | None = None,
        gemini_homes: Sequence[Path] | None = None,
        qwen_homes: Sequence[Path] | None = None,
        aider_homes: Sequence[Path] | None = None,
    ) -> None:
        """热更新额度、活动会话和告警详情请求使用的数据目录。"""

        self.grok_homes = resolve_grok_homes(grok_homes)
        self.kimi_homes = resolve_kimi_homes(kimi_homes)
        self.dsh_homes = resolve_dsh_homes(dsh_homes)
        self.commandcode_homes = resolve_commandcode_homes(commandcode_homes)
        self.claude_homes = resolve_claude_homes(claude_homes)
        if opencode_homes is not None:
            self.opencode_homes = resolve_opencode_homes(opencode_homes)
        if cursor_homes is not None:
            self.cursor_homes = resolve_cursor_homes(cursor_homes)
        if gemini_homes is not None:
            self.gemini_homes = resolve_gemini_homes(gemini_homes)
        if qwen_homes is not None:
            self.qwen_homes = resolve_qwen_homes(qwen_homes)
        if aider_homes is not None:
            self.aider_homes = resolve_aider_homes(aider_homes)
        self.usage_aggregator.update_homes(
            opencode_homes=self.opencode_homes,
            cursor_homes=self.cursor_homes,
            gemini_homes=self.gemini_homes,
            qwen_homes=self.qwen_homes,
            aider_homes=self.aider_homes,
            grok_homes=self.grok_homes,
            kimi_homes=self.kimi_homes,
            dsh_homes=self.dsh_homes,
            claude_homes=self.claude_homes,
            commandcode_homes=self.commandcode_homes,
        )
        self._accounts.update_homes(
            {
                "grok": self.grok_homes,
                "kimi": self.kimi_homes,
                "dsh": self.dsh_homes,
                "commandcode": self.commandcode_homes,
                "claude": self.claude_homes,
                "opencode": self.opencode_homes,
                "cursor": self.cursor_homes,
                "gemini": self.gemini_homes,
                "qwen": self.qwen_homes,
                "aider": self.aider_homes,
            }
        )

    def close(self) -> None:
        """停止 HTTP 服务并等待请求线程退出。"""

        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
            if thread is not None:
                thread.join(timeout=2)
        self.usage_aggregator.close()

    def __enter__(self) -> "DashboardServer":
        """进入上下文并启动服务。"""

        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        """退出上下文并关闭服务。"""

        self.close()
