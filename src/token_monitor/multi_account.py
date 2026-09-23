"""多个 Codex 登录账号的监控编排。"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from .accounts import CodexAccount
from .alerts import AlertStoreError, TrafficAlertStore
from .claude import resolve_claude_homes
from .commandcode import resolve_commandcode_homes
from .dashboard import DashboardConfig, DashboardServer
from .dsh import resolve_dsh_homes
from .grok import resolve_grok_homes
from .housekeeping import AuditTarget, DiskThresholds, HousekeepingMonitor
from .kimi import resolve_kimi_homes
from .monitor import MonitorConfig, MultiSessionMonitor
from .registry import MultiSessionRegistry
from .storage import StateStore
from .traffic import TrafficAlert, TrafficMonitor, TrafficThresholds
from .usage import SessionSwitchThresholds, UsageAggregator


# 中文注释：长会话和磁盘提醒的检查间隔与重复提醒冷却时间。
_ADVICE_INTERVAL_SECONDS = 60.0
_ADVICE_COOLDOWN_SECONDS = 1800.0
_ADVICE_LOG_LIMIT = 500


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
        commandcode_homes: tuple[Path, ...] | None = None,
        claude_homes: tuple[Path, ...] | None = None,
    ) -> None:
        """创建多个单账号监控器。"""

        # 中文注释：没有 Codex 账号也是合法配置——daemon 仍要监控流量、
        # Kimi、DSH、Grok 等 provider，此时状态目录必须显式给出。
        self.accounts = accounts
        self.config = config or MonitorConfig()
        if state_dir is not None:
            self.state_dir = state_dir.expanduser()
        elif accounts:
            self.state_dir = min(
                (account.state_dir for account in accounts),
                key=lambda path: len(path.parts),
            )
        else:
            raise ValueError("没有 Codex 账号时必须指定 state_dir")
        self.logger = logger or logging.getLogger(__name__)
        self.account_monitors = tuple(
            self._create_account_monitor(account) for account in accounts
        )
        self.grok_homes = resolve_grok_homes(grok_homes or None)
        self.kimi_homes = resolve_kimi_homes(kimi_homes or None)
        self.dsh_homes = resolve_dsh_homes(dsh_homes or None)
        self.commandcode_homes = resolve_commandcode_homes(
            commandcode_homes or None
        )
        self.claude_homes = resolve_claude_homes(claude_homes or None)
        self.alert_store = TrafficAlertStore(
            self.state_dir,
            retention_days=self.config.alert_retention_days,
            logger=self.logger,
        )
        self.traffic_monitor = TrafficMonitor(
            thresholds=TrafficThresholds.from_mb(
                burst_warn_mb=self.config.upload_burst_warn_mb,
                burst_danger_mb=self.config.upload_burst_danger_mb,
                window_warn_mb=self.config.upload_window_warn_mb,
                window_danger_mb=self.config.upload_window_danger_mb,
            ),
            alert_sink=self._record_alerts,
        )
        self.session_thresholds = SessionSwitchThresholds(
            turn_warn=self.config.session_turn_warn,
            context_warn_tokens=self.config.session_context_warn_tokens,
        )
        self.housekeeping = HousekeepingMonitor(
            targets=self._housekeeping_targets(),
            thresholds=DiskThresholds.from_gb(
                single_warn_gb=self.config.disk_warn_gb,
                total_warn_gb=self.config.disk_total_warn_gb,
            ),
            archive_dir=self.state_dir / "archives",
            active_paths=self._active_session_paths,
            logger=self.logger,
        )
        self._advice_checked_at = 0.0
        self._advice_logged: dict[str, float] = {}
        self._advice_aggregator: UsageAggregator | None = None
        self._index_thread: threading.Thread | None = None
        self._dashboard: DashboardServer | None = None
        self._stop_event = threading.Event()
        self._started = False

    def _housekeeping_targets(self) -> tuple[AuditTarget, ...]:
        """返回需要统计占用的 agent 数据目录。"""

        targets = [
            AuditTarget(
                label=f"Codex ({item.account.name})",
                product="codex",
                path=item.account.home,
                sessions_root=item.account.home / "sessions",
            )
            for item in self.account_monitors
        ]
        for home in self.grok_homes:
            targets.append(AuditTarget("Grok", "grok", home))
        for home in self.kimi_homes:
            targets.append(AuditTarget("Kimi Code", "kimi", home))
        for home in self.dsh_homes:
            targets.append(AuditTarget("DeepSeek Harness", "dsh", home))
        for home in self.commandcode_homes:
            targets.append(AuditTarget("Command Code", "command-code", home))
        for home in self.claude_homes:
            targets.append(AuditTarget("Claude Code", "claude", home))
        targets.append(AuditTarget("监控状态目录", "state", self.state_dir))
        return tuple(targets)

    def _active_session_paths(self) -> set[str]:
        """返回仍在运行的会话 JSONL 路径，归档和清理时必须跳过。"""

        paths: set[str] = set()
        for item in self.account_monitors:
            for session in item.registry.list_sessions(active_only=True):
                if session.pids and session.jsonl_path:
                    paths.add(str(Path(session.jsonl_path)))
        return paths

    def _should_log_advice(self, key: str, now: float) -> bool:
        """按冷却时间判断同一条提醒是否应该写入日志。"""

        last = self._advice_logged.get(key)
        if last is not None and now - last < _ADVICE_COOLDOWN_SECONDS:
            return False
        self._advice_logged[key] = now
        if len(self._advice_logged) > _ADVICE_LOG_LIMIT:
            for stale in sorted(
                self._advice_logged,
                key=lambda item: self._advice_logged[item],
            )[: len(self._advice_logged) - _ADVICE_LOG_LIMIT]:
                self._advice_logged.pop(stale, None)
        return True

    def _session_advice(self, now: float) -> list[dict[str, object]]:
        """返回活动会话中需要提醒切换新会话的条目。"""

        paths: list[str] = []
        for item in self.account_monitors:
            for session in item.registry.list_sessions(active_only=True):
                if session.jsonl_path:
                    paths.append(str(session.jsonl_path))
        if not paths:
            return []
        aggregator = self._advice_aggregator
        if aggregator is None:
            aggregator = UsageAggregator(
                cache_path=self.state_dir / "usage-index.sqlite3"
            )
            self._advice_aggregator = aggregator
        # 中文注释：没有 Dashboard 时由后台线程保持用量索引可用，
        # 主循环只读内存/索引，不做磁盘 I/O。
        if self._dashboard is None:
            self._refresh_index_in_background(aggregator, now)
        try:
            usages = aggregator.session_usages(paths)
        except (OSError, ValueError):
            return []
        reminders = [
            usage.reminder(self.session_thresholds) for usage in usages.values()
        ]
        return [item for item in reminders if item is not None]

    def _refresh_index_in_background(
        self,
        aggregator: UsageAggregator,
        now: float,
    ) -> None:
        """在后台线程刷新一轮用量索引，避免监控主循环阻塞在磁盘读取上。"""

        thread = self._index_thread
        if thread is not None and thread.is_alive():
            return

        def run() -> None:
            try:
                aggregator.refresh_index(
                    self.registries,
                    self.dashboard_account_metadata,
                    now,
                )
            except (OSError, ValueError) as error:
                self.logger.debug("后台用量索引刷新失败: %s", error)

        thread = threading.Thread(
            target=run,
            name="token-monitor-usage-index",
            daemon=True,
        )
        self._index_thread = thread
        thread.start()

    def _check_advice(self, now: float) -> None:
        """按节流间隔检查磁盘占用和过长会话，并写日志提醒。"""

        if now - self._advice_checked_at < _ADVICE_INTERVAL_SECONDS:
            return
        self._advice_checked_at = now
        report = self.housekeeping.refresh(now)
        for reminder in report.get("reminders", ()):
            key = f"disk:{reminder.get('path') or 'total'}"
            if not self._should_log_advice(key, now):
                continue
            log = (
                self.logger.error
                if reminder.get("level") == "danger"
                else self.logger.warning
            )
            log("磁盘占用提醒：%s", reminder.get("message"))
        for reminder in self._session_advice(now):
            key = f"session:{reminder.get('path')}"
            if not self._should_log_advice(key, now):
                continue
            self.logger.warning("长会话提醒：%s", reminder.get("message"))

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
        # 中文注释：新告警的落盘和日志都由 TrafficMonitor 的 alert_sink 处理，
        # 这里只负责推进流量采样，避免重复记录同一条告警。
        self.traffic_monitor.poll(now=now)
        self._check_advice(time.time() if now is None else float(now))

    def _record_alerts(self, alerts: Sequence[TrafficAlert]) -> None:
        """把新产生的异常流量告警落盘并写日志；落盘失败不影响监控主循环。"""

        try:
            self.alert_store.record(alerts)
        except AlertStoreError as error:
            self.logger.error("异常流量告警落盘失败: %s", error)
        for alert in alerts:
            log = (
                self.logger.warning
                if alert.level == "warn"
                else self.logger.error
            )
            log("%s", alert.message)

    def close(self) -> None:
        """关闭 Dashboard 和所有账号的 App Server，不终止用户任务。"""

        self._stop_event.set()
        thread = self._index_thread
        self._index_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
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
                            claude_homes=self.claude_homes,
                        ),
                        grok_homes=self.grok_homes,
                        kimi_homes=self.kimi_homes,
                        dsh_homes=self.dsh_homes,
                        commandcode_homes=self.commandcode_homes,
                        claude_homes=self.claude_homes,
                        traffic_monitor=self.traffic_monitor,
                        alert_store=self.alert_store,
                        housekeeping=self.housekeeping,
                        session_thresholds=self.session_thresholds,
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
