"""多个 Codex 登录账号的监控编排。"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from .accounts import CodexAccount
from .dashboard import DashboardConfig, DashboardServer
from .dsh import resolve_dsh_homes
from .grok import resolve_grok_homes
from .kimi import resolve_kimi_homes
from .monitor import MonitorConfig, MultiSessionMonitor
from .registry import MultiSessionRegistry
from .storage import StateStore
from .traffic import TrafficMonitor, TrafficThresholds
from .usage import UsageAggregator


@dataclass(frozen=True)
class AccountMonitor:
    """一个账号配置、注册表和单账号监控器的绑定。"""

    account: CodexAccount
    registry: MultiSessionRegistry
    monitor: MultiSessionMonitor


class MultiAccountMonitor:
    """并行管理多个 ``CODEX_HOME``，并隔离额度与会话上下文。"""

    def __init__(
        self,
        accounts: tuple[CodexAccount, ...],
        config: MonitorConfig | None = None,
        state_dir: Path | None = None,
        logger: logging.Logger | None = None,
        grok_homes: tuple[Path, ...] | None = None,
        kimi_homes: tuple[Path, ...] | None = None,
        dsh_homes: tuple[Path, ...] | None = None,
    ) -> None:
        """创建多个单账号监控器。"""

        if not accounts:
            raise ValueError("至少需要一个 Codex 账号")
        self.accounts = accounts
        self.config = config or MonitorConfig()
        self.state_dir = (
            state_dir.expanduser()
            if state_dir is not None
            else min(
                (account.state_dir for account in accounts),
                key=lambda path: len(path.parts),
            )
        )
        self.logger = logger or logging.getLogger(__name__)
        self.account_monitors = tuple(
            self._create_account_monitor(account) for account in accounts
        )
        self.grok_homes = resolve_grok_homes(grok_homes or None)
        self.kimi_homes = resolve_kimi_homes(kimi_homes or None)
        self.dsh_homes = resolve_dsh_homes(dsh_homes or None)
        self.traffic_monitor = TrafficMonitor(
            thresholds=TrafficThresholds.from_mb(
                burst_warn_mb=self.config.upload_burst_warn_mb,
                burst_danger_mb=self.config.upload_burst_danger_mb,
                window_warn_mb=self.config.upload_window_warn_mb,
                window_danger_mb=self.config.upload_window_danger_mb,
            )
        )
        self._dashboard: DashboardServer | None = None
        self._stop_event = threading.Event()
        self._started = False

    @property
    def registries(self) -> Mapping[str, MultiSessionRegistry]:
        """返回 Dashboard 使用的账号名称到注册表映射。"""

        return {item.account.name: item.registry for item in self.account_monitors}

    def start(self) -> None:
        """启动所有账号；单个账号 App Server 失败不阻断其他账号。"""

        if self._started:
            return
        for item in self.account_monitors:
            item.monitor.start(allow_app_server_failure=True)
            self.logger.info(
                "账号 %s 已加入监控: CODEX_HOME=%s",
                item.account.name,
                item.account.home,
            )
        self._started = True

    def run_once(self, now: float | None = None) -> None:
        """对所有账号各执行一轮发现、额度刷新和用量索引。"""

        if not self._started:
            self.start()
        for item in self.account_monitors:
            item.monitor.run_once(now=now)
        snapshot = self.traffic_monitor.poll(now=now)
        if snapshot.alerts:
            for alert in snapshot.alerts:
                if alert.observed_at != snapshot.observed_at:
                    continue
                log = (
                    self.logger.warning
                    if alert.level == "warn"
                    else self.logger.error
                )
                log("%s", alert.message)

    def close(self) -> None:
        """关闭 Dashboard 和所有账号的 App Server，不终止用户任务。"""

        self._stop_event.set()
        dashboard = self._dashboard
        self._dashboard = None
        if dashboard is not None:
            dashboard.close()
        for item in self.account_monitors:
            item.monitor.close()
        self._started = False

    def request_stop(self) -> None:
        """请求主循环安全退出，供后台服务处理 SIGTERM。"""

        self._stop_event.set()

    def run(self) -> int:
        """持续监控全部账号直到收到 Ctrl-C。"""

        lock_store = StateStore(self.state_dir)
        try:
            with lock_store.lock():
                if self.config.dashboard:
                    self._dashboard = DashboardServer(
                        registries=self.registries,
                        account_metadata=self.dashboard_account_metadata,
                        config=DashboardConfig(
                            host=self.config.dashboard_host,
                            port=self.config.dashboard_port,
                            budget_usd=self.config.budget_usd,
                        ),
                        logger=self.logger,
                        usage_aggregator=UsageAggregator(
                            cache_path=self.state_dir / "usage-index.sqlite3",
                            background_indexing=True,
                            grok_homes=self.grok_homes,
                            kimi_homes=self.kimi_homes,
                            dsh_homes=self.dsh_homes,
                        ),
                        grok_homes=self.grok_homes,
                        kimi_homes=self.kimi_homes,
                        dsh_homes=self.dsh_homes,
                        traffic_monitor=self.traffic_monitor,
                    )
                    self._dashboard.start()
                    host, port = self._dashboard.address
                    self.logger.info(
                        "Dashboard 已启动: http://%s:%d/",
                        host,
                        port,
                    )
                # 中文注释：Dashboard 只读取持久状态，可以先于耗时的 App Server
                # 初始化启动，避免首次额度请求超时时页面长时间不可访问。
                self.start()
                while not self._stop_event.is_set():
                    self.run_once()
                    self._stop_event.wait(self.config.scan_interval)
        except KeyboardInterrupt:
            self.logger.info("收到中断，保留所有账号状态并停止监控")
        finally:
            self.close()
        return 0

    def _create_account_monitor(self, account: CodexAccount) -> AccountMonitor:
        """为单个账号创建隔离的注册表和监控器。"""

        registry = MultiSessionRegistry(account.state_dir)
        monitor_config = replace(
            self.config,
            account_name=account.name,
            account_id=account.account_id,
            codex_home=account.home,
            dashboard=False,
        )
        monitor = MultiSessionMonitor(
            registry=registry,
            config=monitor_config,
            session_root=account.session_root,
            logger=self.logger,
        )
        return AccountMonitor(
            account=account,
            registry=registry,
            monitor=monitor,
        )

    @property
    def dashboard_account_metadata(self) -> Mapping[str, Mapping[str, str | None]]:
        """返回 Dashboard 用来按真实账号归组的 profile 元数据。"""

        return {
            item.account.name: {
                "account_id": item.account.account_id,
                "profile_name": item.account.name,
                "codex_home": str(item.account.home),
            }
            for item in self.account_monitors
        }
