"""多个 Codex 登录账号的监控编排。"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from .accounts import (
    CodexAccount,
    _normalize_path,
    build_additional_account_spec,
)
from .alerts import AlertStoreError, TrafficAlertStore
from .claude import resolve_claude_homes
from .commandcode import resolve_commandcode_homes
from .dashboard import DashboardConfig, DashboardServer
from .dsh import resolve_dsh_homes
from .grok import resolve_grok_homes
from .health import HealthTracker
from .housekeeping import AuditTarget, DiskThresholds, HousekeepingMonitor
from .kimi import resolve_kimi_homes
from .monitor import MonitorConfig, MultiSessionMonitor
from .registry import MultiSessionRegistry
from .retention import HistoryDataManager, RetentionController
from .scan_dirs import ScanDirsController
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
        scan_dirs_controller: ScanDirsController | None = None,
    ) -> None:
        """创建多个单账号监控器。

        各 provider 的 ``*_homes`` 遵循同一约定：None 表示自动探测默认目录，
        显式空元组表示禁用该 provider。
        """

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
        # 中文注释：原样透传，None = 自动探测，() = 显式禁用；
        # resolve_*_homes 已实现这一约定。
        self.grok_homes = resolve_grok_homes(grok_homes)
        self.kimi_homes = resolve_kimi_homes(kimi_homes)
        self.dsh_homes = resolve_dsh_homes(dsh_homes)
        self.commandcode_homes = resolve_commandcode_homes(commandcode_homes)
        self.claude_homes = resolve_claude_homes(claude_homes)
        self.scan_dirs_controller = scan_dirs_controller
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
        self._dashboard_aggregator: UsageAggregator | None = None
        self._index_thread: threading.Thread | None = None
        self._dashboard: DashboardServer | None = None
        self._stop_event = threading.Event()
        self._scan_lock = threading.Lock()
        self._started = False
        # 中文注释：daemon 级健康登记表，Dashboard 通过 /api/state 读取；
        # 各组件的过期阈值按各自的检查节奏计算。
        self.health = HealthTracker()
        scan_stale_after = max(2 * self.config.scan_interval, 120)
        self.health.register(
            "main-loop",
            "监控主循环",
            critical=True,
            stale_after=scan_stale_after,
        )
        self.health.register(
            "traffic",
            "异常流量采集",
            stale_after=scan_stale_after,
        )
        self.health.register(
            "housekeeping",
            "磁盘与长会话巡检",
            stale_after=max(2 * _ADVICE_INTERVAL_SECONDS, 180),
        )
        self.health.register(
            "usage-indexer",
            "用量索引",
            stale_after=max(2 * self.config.scan_interval, 300),
        )
        self.health.register(
            "history-cleanup",
            "历史数据清理",
            stale_after=2 * 86400,
        )
        for item in self.account_monitors:
            self._register_account_health(item.account.name)
        # 中文注释:保留期取值优先级为 Web 配置 > 命令行 > 默认值;控制器
        # 负责持久化覆盖,管理器负责预览和清理,reload 回调做热生效。
        self.retention_controller = RetentionController(
            self.state_dir,
            {
                "usage_days": self.config.usage_retention_days,
                "session_days": self.config.session_retention_days,
            },
        )
        effective_retention = self.retention_controller.effective()
        self._history_manager = HistoryDataManager(
            self.state_dir,
            registries=lambda: self.registries,
            alert_store=self.alert_store,
            usage_days=effective_retention["usage_days"],
            session_days=effective_retention["session_days"],
            alert_days=self.config.alert_retention_days,
            logger=self.logger,
        )
        self.retention_controller.reload_callback = (
            lambda effective: self._history_manager.update_retention(
                usage_days=effective["usage_days"],
                session_days=effective["session_days"],
            )
        )
        self._last_history_cleanup_at: float | None = None

    def _register_account_health(self, name: str) -> None:
        """登记一个账号组件；额度查询有自己的节奏，过期阈值按 quota 间隔算。"""

        self.health.register(
            f"account:{name}",
            f"Codex 账号 {name}",
            stale_after=max(2 * self.config.quota_interval, 300),
        )

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
                self.health.record_failure("usage-indexer", error)
            else:
                self.health.record_success("usage-indexer")

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
        try:
            report = self.housekeeping.refresh(now)
        except Exception as error:  # noqa: BLE001 - 巡检失败不中断主循环
            self.health.record_failure("housekeeping", error)
            self.logger.error("磁盘巡检失败: %s", error)
            report = {"reminders": []}
        else:
            self.health.record_success("housekeeping")
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
        self._maybe_cleanup_history(now)

    def _maybe_cleanup_history(self, now: float) -> None:
        """按天执行一次历史数据清理;daemon 启动后首次巡检即执行一轮。

        中文注释:cleanup 内部已逐库容错,抛出异常表示至少一个库删除失败,
        此时 last_cleanup 仍保留部分结果,健康组件记为 failed。
        """

        if (
            self._last_history_cleanup_at is not None
            and now - self._last_history_cleanup_at < 86400.0
        ):
            return
        self._last_history_cleanup_at = now
        try:
            result = self._history_manager.cleanup(now)
        except Exception as error:  # noqa: BLE001 - 清理失败不中断主循环
            self.health.record_failure("history-cleanup", error)
            self.logger.error("历史数据清理失败: %s", error)
            return
        self.health.record_success(
            "history-cleanup",
            deleted=result["deleted"],
            freed_bytes=result["freed_bytes"],
        )

    @property
    def registries(self) -> Mapping[str, MultiSessionRegistry]:
        """返回 Dashboard 使用的账号名称到注册表映射。"""

        return {item.account.name: item.registry for item in self.account_monitors}

    def apply_scan_dirs(self, effective: Mapping[str, tuple[Path, ...]]) -> None:
        """按生效扫描目录热更新各 provider 的监控范围。

        由扫描目录控制器的 reload 回调（Dashboard HTTP 线程）调用。Codex 账号
        按规范化目录 diff：未变化的账号复用原有 AccountMonitor，保留注册表和
        sqlite 检查点；移除的账号只关闭不删除磁盘状态；新增的账号按
        ``build_additional_account_spec`` 的约定命名并分配状态目录。
        """

        with self._scan_lock:
            # 中文注释：effective 由控制器给出全部六个 provider；透传给
            # resolve_*_homes 做规范化去重，空元组保持显式禁用。
            self.grok_homes = resolve_grok_homes(effective.get("grok") or ())
            self.kimi_homes = resolve_kimi_homes(effective.get("kimi") or ())
            self.dsh_homes = resolve_dsh_homes(effective.get("dsh") or ())
            self.commandcode_homes = resolve_commandcode_homes(
                effective.get("commandcode") or ()
            )
            self.claude_homes = resolve_claude_homes(effective.get("claude") or ())

            desired_homes: list[Path] = []
            seen_homes: set[Path] = set()
            for home in effective.get("codex", ()):
                normalized = _normalize_path(home)
                if normalized in seen_homes:
                    continue
                seen_homes.add(normalized)
                desired_homes.append(normalized)

            current_by_home = {
                item.account.home: item for item in self.account_monitors
            }
            kept = [
                item
                for item in self.account_monitors
                if item.account.home in seen_homes
            ]
            for item in self.account_monitors:
                if item.account.home in seen_homes:
                    continue
                try:
                    item.monitor.close()
                except Exception as error:  # noqa: BLE001 - 关闭失败不阻断重载
                    self.logger.error(
                        "关闭账号 %s 的监控失败: %s", item.account.name, error
                    )
                self.health.unregister(f"account:{item.account.name}")
                self.logger.info(
                    "账号 %s 已移出监控: CODEX_HOME=%s",
                    item.account.name,
                    item.account.home,
                )
            existing_accounts = [item.account for item in kept]
            added: list[AccountMonitor] = []
            for home in desired_homes:
                if home in current_by_home:
                    continue
                spec = build_additional_account_spec(
                    home,
                    self.state_dir,
                    existing_accounts,
                )
                item = self._create_account_monitor(spec)
                if self._started:
                    try:
                        item.monitor.start(allow_app_server_failure=True)
                    except Exception as error:  # noqa: BLE001 - 单个账号失败不影响其他目录
                        self.logger.error(
                            "新增账号 %s 启动失败，已跳过: CODEX_HOME=%s: %s",
                            spec.name,
                            spec.home,
                            error,
                        )
                        continue
                    self.logger.info(
                        "账号 %s 已加入监控: CODEX_HOME=%s",
                        spec.name,
                        spec.home,
                    )
                existing_accounts.append(spec)
                added.append(item)
                # 中文注释:start 失败走上面的 continue,不会登记健康组件。
                self._register_account_health(spec.name)
            self.account_monitors = tuple(kept + added)
            self.accounts = tuple(item.account for item in self.account_monitors)

            self.housekeeping.update_targets(self._housekeeping_targets())
            for aggregator in (
                self._dashboard_aggregator,
                self._advice_aggregator,
            ):
                if aggregator is not None:
                    aggregator.update_homes(
                        grok_homes=self.grok_homes,
                        kimi_homes=self.kimi_homes,
                        dsh_homes=self.dsh_homes,
                        claude_homes=self.claude_homes,
                    )
            if self._dashboard is not None:
                self._dashboard.update_accounts(
                    self.registries,
                    self.dashboard_account_metadata,
                )

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
        """对所有账号各执行一轮发现、额度刷新和用量索引。

        中文注释：单个账号的异常只记入该账号的健康组件并继续其他账号，
        不再中断整轮（行为变更：原来是直接传播）；整轮级别的异常记入
        ``main-loop`` 组件后重抛。
        """

        try:
            if not self._started:
                self.start()
            for item in self.account_monitors:
                self._run_account_once(item, now=now)
            self._poll_traffic(now=now)
            self._check_advice(time.time() if now is None else float(now))
        except Exception as error:
            self.health.record_failure("main-loop", error)
            raise
        self.health.record_success("main-loop")

    def _run_account_once(
        self,
        item: AccountMonitor,
        now: float | None = None,
    ) -> None:
        """执行单账号的一轮监控，并按额度链路状态记录账号组件健康。"""

        key = f"account:{item.account.name}"
        try:
            item.monitor.run_once(now=now)
        except Exception as error:  # noqa: BLE001 - 单账号失败不中断其他账号
            self.health.record_failure(key, error)
            self.logger.error(
                "账号 %s 本轮监控失败: %s",
                item.account.name,
                error,
            )
            return
        quota = item.monitor.quota_health()
        if not quota["app_server_available"]:
            self.health.record_success(
                key,
                degraded=True,
                reason="app-server 不可用,使用进程与 JSONL 证据",
            )
        elif quota["last_error"] is not None:
            self.health.record_success(key, quota_error=quota["last_error"])
        else:
            self.health.record_success(key)

    def _poll_traffic(self, now: float | None = None) -> None:
        """推进流量采样并按来源可用性记录 traffic 组件健康。"""

        # 中文注释：新告警的落盘和日志都由 TrafficMonitor 的 alert_sink 处理，
        # 这里只负责推进流量采样，避免重复记录同一条告警。
        try:
            snapshot = self.traffic_monitor.poll(now=now)
        except Exception as error:  # noqa: BLE001 - 流量采集失败不中断主循环
            self.health.record_failure("traffic", error)
            self.logger.error("流量采集失败: %s", error)
            return
        if snapshot.source == "unavailable":
            self.health.record_success(
                "traffic",
                degraded=True,
                reason="流量采集不可用",
            )
        else:
            self.health.record_success("traffic")

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
        self._dashboard_aggregator = None
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
                if self.scan_dirs_controller is not None:
                    self.scan_dirs_controller.reload_callback = self.apply_scan_dirs
                if self.config.dashboard:
                    usage_aggregator = UsageAggregator(
                        cache_path=self.state_dir / "usage-index.sqlite3",
                        background_indexing=True,
                        grok_homes=self.grok_homes,
                        kimi_homes=self.kimi_homes,
                        dsh_homes=self.dsh_homes,
                        claude_homes=self.claude_homes,
                    )
                    self._dashboard_aggregator = usage_aggregator
                    self._dashboard = DashboardServer(
                        registries=self.registries,
                        account_metadata=self.dashboard_account_metadata,
                        config=DashboardConfig(
                            host=self.config.dashboard_host,
                            port=self.config.dashboard_port,
                            budget_usd=self.config.budget_usd,
                        ),
                        logger=self.logger,
                        usage_aggregator=usage_aggregator,
                        grok_homes=self.grok_homes,
                        kimi_homes=self.kimi_homes,
                        dsh_homes=self.dsh_homes,
                        commandcode_homes=self.commandcode_homes,
                        claude_homes=self.claude_homes,
                        traffic_monitor=self.traffic_monitor,
                        alert_store=self.alert_store,
                        housekeeping=self.housekeeping,
                        session_thresholds=self.session_thresholds,
                        scan_dirs=self.scan_dirs_controller,
                        health=self.health,
                        history=self._history_manager,
                        retention=self.retention_controller,
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
