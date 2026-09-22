"""所有活动 Codex JSONL 会话、额度和本地用量的被动监控。"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .app_server import AppServerClient, AppServerConfig, AppServerError
from .alerts import TrafficAlertStore
from .dashboard import DashboardConfig, DashboardServer
from .housekeeping import DEFAULT_SINGLE_WARN_GIB, DEFAULT_TOTAL_WARN_GIB
from .discovery import (
    JsonlSessionReader,
    ProcessObservation,
    ProcessScanner,
    SessionMetadata,
    SessionTail,
    default_session_root,
)
from .events import EventObservation
from .multi_models import (
    DetectionConfidence,
    SessionStatus,
    TrackedSession,
)
from .quota import QuotaSnapshot, merge_sparse_update
from .quota_fallback import JsonlQuotaFallbackReader, recent_session_paths
from .registry import MultiSessionRegistry, RegistryError
from .storage import StateStore
from .usage import (
    DEFAULT_SESSION_CONTEXT_WARN_TOKENS,
    DEFAULT_SESSION_TURN_WARN,
    UsageAggregator,
)


@dataclass(frozen=True)
class MonitorConfig:
    """多会话监控参数，时间单位为秒。"""

    codex_path: str = "codex"
    scan_interval: float = 2.0
    reconcile_interval: float = 30.0
    quota_interval: float = 300.0
    reset_grace: float = 30.0
    unknown_reset_wait: float = 900.0
    fallback_max_age: float = 600.0
    max_concurrent_resumes: int = 1
    continuation_prompt: str = (
        "请从当前会话和仓库状态继续完成原任务。先检查已经完成的改动和未完成部分，"
        "不要撤销已完成的工作；如果上一次在中断点附近重复执行，请先确认当前状态，再继续。"
    )
    # 中文注释：daemon 和 systemd 服务会显式传入 False；保留默认值兼容
    # 旧的离线 MonitorConfig 调用和历史状态测试。
    auto_resume: bool = True
    dry_run: bool = False
    dashboard: bool = False
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    budget_usd: float | None = None
    upload_burst_warn_mb: float = 8.0
    upload_burst_danger_mb: float = 32.0
    upload_window_warn_mb: float = 64.0
    upload_window_danger_mb: float = 256.0
    alert_retention_days: float = 30.0
    session_turn_warn: int = DEFAULT_SESSION_TURN_WARN
    session_context_warn_tokens: int = DEFAULT_SESSION_CONTEXT_WARN_TOKENS
    disk_warn_gb: float = DEFAULT_SINGLE_WARN_GIB
    disk_total_warn_gb: float = DEFAULT_TOTAL_WARN_GIB
    codex_home: Path | None = None
    account_name: str = "codex"
    account_id: str | None = None

    def __post_init__(self) -> None:
        """校验不会导致忙等或并发续跑的配置。"""

        if self.scan_interval <= 0:
            raise ValueError("scan_interval 必须大于 0")
        if self.reconcile_interval <= 0:
            raise ValueError("reconcile_interval 必须大于 0")
        if self.quota_interval <= 0:
            raise ValueError("quota_interval 必须大于 0")
        if self.reset_grace < 0:
            raise ValueError("reset_grace 不能小于 0")
        if self.unknown_reset_wait <= 0:
            raise ValueError("unknown_reset_wait 必须大于 0")
        if self.fallback_max_age <= 0:
            raise ValueError("fallback_max_age 必须大于 0")
        if self.max_concurrent_resumes <= 0:
            raise ValueError("max_concurrent_resumes 必须大于 0")
        if not self.account_name.strip():
            raise ValueError("account_name 不能为空")
        if not self.dashboard_host.strip():
            raise ValueError("dashboard_host 不能为空")
        if not 0 <= self.dashboard_port <= 65535:
            raise ValueError("dashboard_port 必须在 0 到 65535 之间")
        if self.budget_usd is not None and self.budget_usd <= 0:
            raise ValueError("budget_usd 必须大于 0")
        for name in (
            "upload_burst_warn_mb",
            "upload_burst_danger_mb",
            "upload_window_warn_mb",
            "upload_window_danger_mb",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须大于 0")
        if self.upload_burst_danger_mb < self.upload_burst_warn_mb:
            raise ValueError("upload_burst_danger_mb 不能小于 upload_burst_warn_mb")
        if self.upload_window_danger_mb < self.upload_window_warn_mb:
            raise ValueError(
                "upload_window_danger_mb 不能小于 upload_window_warn_mb"
            )
        if self.alert_retention_days <= 0:
            raise ValueError("alert_retention_days 必须大于 0")
        if self.session_turn_warn <= 0:
            raise ValueError("session_turn_warn 必须大于 0")
        if self.session_context_warn_tokens <= 0:
            raise ValueError("session_context_warn_tokens 必须大于 0")
        if self.disk_warn_gb <= 0:
            raise ValueError("disk_warn_gb 必须大于 0")
        if self.disk_total_warn_gb <= 0:
            raise ValueError("disk_total_warn_gb 必须大于 0")


@dataclass(frozen=True)
class AppServerThread:
    """从 ``thread/list`` 规范化出的运行时会话摘要。"""

    thread_id: str
    session_id: str
    status: str
    active: bool
    waiting_for_approval: bool
    cwd: Path | None = None
    jsonl_path: Path | None = None
    source: str = "app-server"
    parent_thread_id: str | None = None
    root_thread_id: str | None = None


@dataclass
class _ResumeWorker:
    """一个后台 resume 进程的内存句柄。"""

    thread: threading.Thread
    started_at: float


class MultiSessionMonitor:
    """监控账户下所有活动 JSONL、额度和本地用量。"""

    # 中文注释：活动监听每轮最多读取 256 KiB。旧注册记录若仍是 0 偏移，
    # 落后超过 1 MiB 时直接跳到文件尾，避免每 2 秒吞掉巨型 JSONL。
    _ACTIVE_SESSION_READ_BYTES = 256 * 1024
    _ACTIVE_CATCHUP_SKIP_BYTES = 1 * 1024 * 1024
    _PROCESS_STOP_TIMEOUT = 10.0
    _PROCESS_STOP_POLL = 0.2

    def __init__(
        self,
        registry: MultiSessionRegistry,
        config: MonitorConfig | None = None,
        session_root: Path | None = None,
        logger: logging.Logger | None = None,
        app_server: AppServerClient | None = None,
        process_scanner: ProcessScanner | None = None,
        jsonl_reader: JsonlSessionReader | None = None,
    ) -> None:
        self.registry = registry
        self.config = config or MonitorConfig()
        self.logger = logger or logging.getLogger(__name__)
        configured_session_root = session_root
        if configured_session_root is None and self.config.codex_home is not None:
            configured_session_root = self.config.codex_home / "sessions"
        self.session_root = (
            configured_session_root or default_session_root()
        ).expanduser()
        self.process_scanner = process_scanner or ProcessScanner(
            session_root=self.session_root,
        )
        self.jsonl_reader = jsonl_reader or JsonlSessionReader()
        self.quota_fallback_reader = JsonlQuotaFallbackReader(
            reader=self.jsonl_reader,
        )
        self.app_server = app_server or AppServerClient(
            AppServerConfig(
                codex_path=self.config.codex_path,
                codex_home=self.config.codex_home,
            ),
            logger=self.logger,
            notification_handler=self._handle_app_server_notification,
        )
        self._quota: QuotaSnapshot | None = self.registry.load_quota()
        self._quota_lock = threading.Lock()
        self._quota_is_current = False
        self._last_quota_at: float | None = None
        self._last_reconcile_at: float | None = None
        self._active_app_threads: set[str] = set()
        self._app_thread_status: dict[str, AppServerThread] = {}
        self._resume_workers: dict[str, _ResumeWorker] = {}
        self._dashboard: DashboardServer | None = None
        self._stop_event = threading.Event()
        self._started = False

    @property
    def quota(self) -> QuotaSnapshot | None:
        """返回最近一次额度快照。"""

        with self._quota_lock:
            return self._quota

    def start(self, allow_app_server_failure: bool = False) -> None:
        """启动 App Server 连接并立即主动查询额度。

        多账号监控时，某个账号未登录或连接失败不应阻断其他账号；调用方可以
        使用 ``allow_app_server_failure`` 让本地 JSONL 进程证据继续生效。
        """

        if self._started:
            return
        try:
            self.app_server.start()
        except AppServerError as error:
            if not allow_app_server_failure:
                raise
            self.logger.warning(
                "账号 %s 的 App Server 不可用，将继续使用进程和 JSONL 证据: %s",
                self.config.account_name,
                error,
            )
        self._started = True
        self.refresh_quota(force=True)

    def close(self) -> None:
        """停止主循环使用的 App Server；不强杀用户的 Codex 进程。"""

        self._stop_event.set()
        for worker in tuple(self._resume_workers.values()):
            worker.thread.join(timeout=0.2)
        self._resume_workers.clear()
        dashboard = self._dashboard
        self._dashboard = None
        if dashboard is not None:
            dashboard.close()
        self.app_server.close()
        self._started = False

    def run(self) -> int:
        """持续监控直到收到 Ctrl-C。"""

        lock_store = StateStore(self.registry.state_dir)
        try:
            with lock_store.lock():
                if self.config.dashboard:
                    self._dashboard = DashboardServer(
                        registry=self.registry,
                        account_metadata={
                            self.config.account_name: {
                                "account_id": self.config.account_id,
                                "profile_name": self.config.account_name,
                                "codex_home": (
                                    str(self.config.codex_home)
                                    if self.config.codex_home is not None
                                    else None
                                ),
                            }
                        },
                        config=DashboardConfig(
                            host=self.config.dashboard_host,
                            port=self.config.dashboard_port,
                            budget_usd=self.config.budget_usd,
                        ),
                        logger=self.logger,
                        usage_aggregator=UsageAggregator(
                            cache_path=self.registry.state_dir / "usage-index.sqlite3",
                            background_indexing=True,
                        ),
                        # 中文注释：单账号进程不扫描流量，但仍展示同一状态目录里
                        # 已落盘的历史告警，避免和 daemon 的视图不一致。
                        alert_store=TrafficAlertStore(
                            self.registry.state_dir,
                            retention_days=self.config.alert_retention_days,
                        ),
                    )
                    self._dashboard.start()
                    host, port = self._dashboard.address
                    self.logger.info(
                        "Dashboard 已启动: http://%s:%d/",
                        host,
                        port,
                    )
                # 中文注释：网页只读取 SQLite，可在额度接口初始化前先提供历史状态。
                self.start()
                while not self._stop_event.is_set():
                    self.run_once()
                    self._stop_event.wait(self.config.scan_interval)
        except KeyboardInterrupt:
            self.logger.info("收到中断，保留数据库状态并停止监控")
        finally:
            self.close()
        return 0

    def run_once(self, now: float | None = None) -> None:
        """执行一次发现、增量解析和额度刷新。"""

        current_time = now if now is not None else time.time()
        if not self._started:
            self.start()
        self.app_server.drain_notifications()
        if (
            self._last_quota_at is None
            or current_time - self._last_quota_at >= self.config.quota_interval
        ):
            self.refresh_quota(now=current_time)
        if (
            self._last_reconcile_at is None
            or current_time - self._last_reconcile_at >= self.config.reconcile_interval
        ):
            self.reconcile_threads(now=current_time)
        self.scan_processes(now=current_time)
        self._recheck_completed_quota(now=current_time)
        self.finalize_sessions(now=current_time)

    def refresh_quota(
        self,
        force: bool = False,
        now: float | None = None,
    ) -> QuotaSnapshot | None:
        """主动读取账户额度；读取本身不启动模型 turn。"""

        current_time = now if now is not None else time.time()
        if (
            not force
            and self._last_quota_at is not None
            and current_time - self._last_quota_at < self.config.quota_interval
        ):
            return self.quota
        try:
            snapshot = self.app_server.read_rate_limits(now=current_time)
        except AppServerError as error:
            fallback = self._read_fallback_quota(current_time)
            if fallback is not None:
                self._set_quota(fallback)
                self._quota_is_current = True
                self.logger.warning(
                    "App Server 额度查询失败，使用最近本地 JSONL 快照: %s",
                    error,
                )
                self._last_quota_at = current_time
                return fallback
            self.logger.warning("主动查询额度失败，保留上次快照: %s", error)
            self._quota_is_current = False
            self._last_quota_at = current_time
            return self.quota
        self._set_quota(snapshot)
        self._quota_is_current = True
        self._last_quota_at = current_time
        self.logger.debug(
            "额度快照已更新：%d 个窗口，来源 %s",
            len(snapshot.windows),
            snapshot.source,
        )
        return snapshot

    def reconcile_threads(self, now: float | None = None) -> list[AppServerThread]:
        """从 App Server 对账所有来源的运行时会话。"""

        current_time = now if now is not None else time.time()
        try:
            raw_threads = self.app_server.list_threads()
        except AppServerError as error:
            self.logger.warning("thread/list 对账失败，将继续使用进程扫描: %s", error)
            self._active_app_threads = set()
            self._app_thread_status = {}
            self._last_reconcile_at = current_time
            return []

        summaries = [
            summary
            for item in raw_threads
            if (summary := self._parse_app_thread(item)) is not None
        ]
        self._app_thread_status = {item.thread_id: item for item in summaries}
        self._active_app_threads = {item.thread_id for item in summaries if item.active}
        for summary in summaries:
            if not summary.active:
                continue
            self._upsert_app_thread(summary, current_time)
        self._last_reconcile_at = current_time
        return summaries

    def scan_processes(
        self,
        now: float | None = None,
    ) -> tuple[TrackedSession, ...]:
        """扫描所有打开 JSONL 的进程并增量读取对应文件。"""

        current_time = now if now is not None else time.time()
        processes = self.process_scanner.scan()
        existing = self.registry.list_sessions(active_only=False)
        by_path = {
            Path(session.jsonl_path).resolve(): session
            for session in existing
            if session.jsonl_path
        }
        by_thread = {session.thread_id: session for session in existing}
        observed: list[TrackedSession] = []
        observed_paths: set[str] = set()
        holders_by_path: dict[Path, list[ProcessObservation]] = {}
        for process in processes:
            for path in process.open_jsonl_paths:
                resolved_path = self._resolved_path(path)
                holders_by_path.setdefault(resolved_path, []).append(process)
        for resolved_path, holders in holders_by_path.items():
            process = holders[0]
            metadata = self.jsonl_reader.read_metadata(resolved_path)
            session = self._find_session(
                by_path=by_path,
                by_thread=by_thread,
                path=resolved_path,
                metadata=metadata,
            )
            if session is None:
                session = self._new_session(
                    resolved_path,
                    metadata,
                    process,
                    current_time,
                )
            else:
                session = self._refresh_session_from_process(
                    session,
                    resolved_path,
                    metadata,
                    process,
                    current_time,
                )
            session.pids = tuple(sorted({item.pid for item in holders}))
            session.process_start_tokens = tuple(
                sorted({item.start_token for item in holders})
            )
            tail = self.jsonl_reader.read(
                resolved_path,
                offset=self._active_read_offset(
                    resolved_path,
                    session.last_offset,
                    session,
                ),
                now=current_time,
                maximum_bytes=self._ACTIVE_SESSION_READ_BYTES,
            )
            self._apply_tail(session, tail, current_time)
            self.registry.upsert_session(session)
            by_path[resolved_path] = session
            by_thread[session.thread_id] = session
            observed.append(session)
            observed_paths.add(str(resolved_path))

        # 进程扫描是独立于 App Server 的强证据；先清除已结束进程的 PID。
        for session in existing:
            if not session.jsonl_path:
                continue
            path_key = str(self._resolved_path(Path(session.jsonl_path)))
            if path_key in observed_paths:
                continue
            if session.pids:
                session.pids = ()
                session.process_start_tokens = ()
                session.confidence = DetectionConfidence.PERSISTED
                self.registry.upsert_session(session)
        return tuple(observed)

    def _recheck_completed_quota(self, now: float) -> None:
        """重读 task_complete 尾部，捕获包在完成事件里的额度错误。"""

        for session in self.registry.list_sessions(active_only=False):
            if session.last_event_type not in {"task_complete", "task_completed"}:
                continue
            if session.metadata.get("quota_blocked") == "1":
                continue
            if session.metadata.get("terminal_quota_recheck") == "1":
                continue
            if session.last_resume_result in {"failed", "success", "running"}:
                continue
            if session.status == SessionStatus.FAILED:
                continue
            if not session.jsonl_path:
                continue
            path = Path(session.jsonl_path)
            session.metadata["terminal_quota_recheck"] = "1"
            tail = self.jsonl_reader.read(
                path,
                offset=self.jsonl_reader.initial_offset(path),
                now=now,
                maximum_bytes=self._ACTIVE_SESSION_READ_BYTES,
            )
            self._apply_tail(session, tail, now)
            self.registry.upsert_session(session)

    def finalize_sessions(self, now: float | None = None) -> None:
        """额度阻塞且进程还在则排队；用户已退出的不恢复。"""

        current_time = now if now is not None else time.time()
        for session in self.registry.list_sessions(active_only=True):
            if not self._session_may_drop_resume(session):
                # 这个 profile 里可能残留上一次登录的会话；保留原状态，
                # 等用户切回原账号后再由对应的监控器继续处理。
                continue
            worker = self._resume_workers.get(session.thread_id)
            worker_alive = worker is not None and worker.thread.is_alive()
            os_live = worker_alive or self._has_os_process(session)
            quota_blocked = (
                session.status
                in {SessionStatus.QUEUED, SessionStatus.LIMIT_BLOCKED}
                or session.metadata.get("quota_blocked") == "1"
            )
            if quota_blocked:
                if os_live:
                    if self._session_account_matches_config(session):
                        self._queue_for_reset(session, current_time)
                    continue
                if self._is_monitor_resume_cycle(session) and session.auto_resume:
                    if self._session_account_matches_config(session):
                        self._queue_for_reset(session, current_time)
                    continue
                self._drop_resume_queue(session, "进程已退出，不自动恢复")
                continue
            if os_live:
                continue
            if session.metadata.get("approval_waiting") == "1":
                session.status = SessionStatus.WAITING_FOR_APPROVAL
                session.next_attempt_at = None
                self.registry.upsert_session(session)
                continue
            if session.terminal:
                session.status = SessionStatus.COMPLETED
                session.next_attempt_at = None
                self.registry.upsert_session(session)
                continue
            if session.status in {
                SessionStatus.RUNNING,
                SessionStatus.DISCOVERED,
                SessionStatus.RESUMING,
            }:
                session.status = SessionStatus.ORPHANED
                session.auto_resume = False
                session.last_error = "Codex 进程已消失且没有明确的完成或额度失败事件"
                self.registry.upsert_session(session)

    def _protected_pids(self) -> set[int]:
        """监控器自身和 App Server 不能被当成用户会话进程结束。"""

        protected = {os.getpid()}
        process = getattr(self.app_server, "process", None)
        pid = getattr(process, "pid", None)
        if isinstance(pid, int) and pid > 0:
            protected.add(pid)
        return protected

    def _pid_is_alive(self, pid: int) -> bool:
        """以零信号检查进程是否存在，不向进程发送实际信号。"""

        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _signal_pid(self, pid: int, sig: int) -> None:
        """向用户 Codex 进程发送退出信号。"""

        if pid <= 0:
            return
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        except PermissionError as error:
            self.logger.warning("无法向进程 %s 发送信号 %s: %s", pid, sig, error)

    def _session_holder_pids(self, session: TrackedSession) -> set[int]:
        """找出仍占用该会话 JSONL 或仍记录在会话上的用户进程。"""

        protected = self._protected_pids()
        holders: set[int] = set()
        if session.jsonl_path:
            target = str(self._resolved_path(Path(session.jsonl_path)))
            for process in self.process_scanner.scan():
                if process.pid in protected or process.pid <= 0:
                    continue
                if any(
                    str(self._resolved_path(path)) == target
                    for path in process.open_jsonl_paths
                ):
                    holders.add(process.pid)
        for pid in session.pids:
            if pid in protected or pid <= 0 or pid in holders:
                continue
            if self._pid_is_alive(pid):
                holders.add(pid)
        return holders

    def _has_os_process(self, session: TrackedSession) -> bool:
        """判断用户侧是否还有占用该会话的 Codex 进程。"""

        worker = self._resume_workers.get(session.thread_id)
        if worker is not None and worker.thread.is_alive():
            return True
        return bool(self._session_holder_pids(session))

    def _is_monitor_resume_cycle(self, session: TrackedSession) -> bool:
        """监控器自己拉起的 resume 再次额度中断后，允许进程退出后再排队。"""

        if session.last_resume_result in {"quota_blocked", "running"}:
            return True
        return (
            session.resume_attempts > 0
            and session.metadata.get("quota_blocked") == "1"
        )

    def _queue_for_reset(self, session: TrackedSession, now: float) -> None:
        """把仍允许自动恢复的额度中断会话排进到期队列。"""

        if not self.config.auto_resume:
            return
        if (
            session.metadata.get("auto_resume_disabled_reason") == "user_cancelled"
            or self.registry.is_resume_cancelled(session.thread_id)
        ):
            # 中文注释：独立取消表可抵御 Dashboard 点击与监控 upsert 的并发竞争。
            session.status = SessionStatus.LIMIT_BLOCKED
            session.auto_resume = False
            session.next_attempt_at = None
            session.last_resume_result = "cancelled"
            session.metadata["auto_resume_disabled_reason"] = "user_cancelled"
            self.registry.upsert_session(session)
            return
        if session.metadata.get("approval_waiting") == "1":
            return
        if session.metadata.get("auto_resume_disabled_reason") == "approval":
            return
        if self._is_subagent(session.source) or session.parent_thread_id:
            return
        if not session.session_id:
            return
        session.auto_resume = True
        session.terminal = False
        snapshot = self.quota
        if (
            snapshot is not None
            and self._quota_is_current
            and not snapshot.exhausted_windows
        ):
            attempt = now
        else:
            session.quota_reset_at = self._best_reset_at(session) or session.quota_reset_at
            attempt = self._next_attempt_at(session.quota_reset_at, now)
        if (
            session.status == SessionStatus.QUEUED
            and session.next_attempt_at is not None
        ):
            session.next_attempt_at = min(session.next_attempt_at, attempt)
        else:
            session.status = SessionStatus.QUEUED
            session.next_attempt_at = attempt
        self.registry.upsert_session(session)

    def _stop_session_processes(self, session: TrackedSession) -> bool:
        """额度恢复后先结束原 CLI，避免和新的 resume 同时写同一份 JSONL。"""

        holders = self._session_holder_pids(session)
        if not holders:
            return True
        self.logger.info(
            "额度已恢复，先结束会话 %s 的原进程 %s",
            session.session_id,
            sorted(holders),
        )
        for pid in sorted(holders):
            self._signal_pid(pid, signal.SIGTERM)
        deadline = time.time() + self._PROCESS_STOP_TIMEOUT
        while time.time() < deadline and self._session_holder_pids(session):
            time.sleep(self._PROCESS_STOP_POLL)
        holders = self._session_holder_pids(session)
        if not holders:
            return True
        for pid in sorted(holders):
            self._signal_pid(pid, signal.SIGKILL)
        deadline = time.time() + min(2.0, max(self._PROCESS_STOP_TIMEOUT, 0.01))
        while time.time() < deadline and self._session_holder_pids(session):
            time.sleep(self._PROCESS_STOP_POLL)
        remaining = self._session_holder_pids(session)
        if remaining:
            self.logger.warning(
                "会话 %s 的原进程仍未退出: %s",
                session.session_id,
                sorted(remaining),
            )
            return False
        return True

    def _drop_dead_queued_sessions(self) -> None:
        """到期前取消用户已经自己退出的恢复队列。"""

        for session in self.registry.list_sessions(active_only=True):
            if session.status not in {
                SessionStatus.QUEUED,
                SessionStatus.LIMIT_BLOCKED,
            }:
                continue
            if not session.auto_resume and session.status != SessionStatus.QUEUED:
                continue
            if not self._session_may_drop_resume(session):
                continue
            if self._has_os_process(session):
                continue
            if self._is_monitor_resume_cycle(session) and session.auto_resume:
                continue
            self._drop_resume_queue(session, "进程已退出，不自动恢复")

    def _drop_resume_queue(self, session: TrackedSession, reason: str) -> None:
        """取消自动恢复队列，保留额度中断记录。"""

        session.status = SessionStatus.LIMIT_BLOCKED
        session.auto_resume = False
        session.next_attempt_at = None
        if not session.last_error:
            session.last_error = reason
        elif reason not in session.last_error:
            session.last_error = f"{session.last_error}；{reason}"
        self.registry.upsert_session(session)

    def resume_due(self, now: float | None = None) -> int:
        """在账户额度确认可用后，串行启动到期队列项。"""

        if not self.config.auto_resume:
            return 0
        current_time = now if now is not None else time.time()
        self._remove_finished_workers()
        self._drop_dead_queued_sessions()
        available_slots = self.config.max_concurrent_resumes - len(self._resume_workers)
        started = 0
        while available_slots > 0:
            # 没有到期项时不做强制额度查询，保持正常的 quota_interval
            # 节奏；只有真正准备 resume 时才重新确认账户额度。
            if not self.registry.has_due_session(
                current_time,
                account_id=self.config.account_id,
            ):
                break
            if not self._quota_allows_resume(current_time):
                break
            session = self.registry.claim_due_session(
                current_time,
                account_id=self.config.account_id,
            )
            if session is None:
                break
            if not self._has_os_process(session) and not self._is_monitor_resume_cycle(
                session
            ):
                self._drop_resume_queue(
                    session,
                    "进程已退出，不自动恢复",
                )
                continue
            if self.config.dry_run:
                self.logger.info(
                    "dry-run：会话 %s 已到期，原本将结束原进程并执行 resume",
                    session.session_id,
                )
                session.status = SessionStatus.QUEUED
                self.registry.upsert_session(session)
                break
            worker_thread = threading.Thread(
                target=self._resume_worker,
                args=(session,),
                name=f"codex-resume-{session.thread_id[:12]}",
                daemon=True,
            )
            self._resume_workers[session.thread_id] = _ResumeWorker(
                thread=worker_thread,
                started_at=current_time,
            )
            worker_thread.start()
            started += 1
            available_slots -= 1
            # 同一账户的多个队列项不在一个循环里同时放行，避免突发消耗。
            break
        return started

    def _resume_worker(self, session: TrackedSession) -> None:
        """先结束仍占用 JSONL 的原进程，再执行 ``codex exec resume``。"""

        started_at = time.time()
        process: subprocess.Popen[str] | None = None
        quota_event: EventObservation | None = None
        observed_session_id = session.session_id
        last_reason: str | None = None
        try:
            if not self._stop_session_processes(session):
                latest = self.registry.get_session(session.thread_id) or session
                latest.status = SessionStatus.QUEUED
                latest.next_attempt_at = time.time() + max(
                    self.config.reset_grace,
                    15.0,
                )
                latest.last_error = "无法结束原 Codex 进程，暂缓 resume"
                latest.last_resume_result = "waiting_for_reset"
                self.registry.upsert_session(latest)
                return
            session.pids = ()
            session.process_start_tokens = ()
            time.sleep(min(0.5, max(self._PROCESS_STOP_POLL, 0.05)))
            session.last_resume_started_at = started_at
            session.last_resume_finished_at = None
            session.last_resume_result = "running"
            self.registry.upsert_session(session)
            cwd = Path(session.cwd).expanduser() if session.cwd else Path.cwd()
            if not cwd.is_dir():
                raise RuntimeError(f"工作目录不存在: {cwd}")
            command = self._build_resume_command(session)
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=self._environment(),
            )
            session.pids = (process.pid,)
            session.process_start_tokens = ()
            session.status = SessionStatus.RESUMING
            session.cwd = str(cwd)
            session.metadata.pop("quota_blocked", None)
            session.metadata.pop("approval_waiting", None)
            self.registry.upsert_session(session)
            if process.stdout is not None:
                for line in process.stdout:
                    observation = self._apply_resume_line(
                        session,
                        line,
                        observed_session_id,
                    )
                    observed_session_id = observation[0]
                    if observation[1] is not None:
                        quota_event = observation[1]
                    if observation[2] is not None:
                        last_reason = observation[2]
            returncode = process.wait()
            session = self.registry.get_session(session.thread_id) or session
            session.pids = ()
            session.process_start_tokens = ()
            session.last_exit_code = returncode
            session.last_event_type = session.last_event_type or "resume.finished"
            session.last_event_at = time.time()
            if observed_session_id:
                session.session_id = observed_session_id
            finished_at = time.time()
            if quota_event is not None:
                session.status = SessionStatus.QUEUED
                session.auto_resume = True
                session.metadata["quota_blocked"] = "1"
                session.last_error = last_reason or "续跑再次命中额度限制"
                session.quota_blocked_at = (
                    session.quota_blocked_at or session.last_event_at or finished_at
                )
                session.last_resume_finished_at = finished_at
                session.last_resume_result = "quota_blocked"
                if quota_event.reset_at is not None:
                    session.quota_reset_at = quota_event.reset_at
                session.quota_reset_at = self._best_reset_at(session)
                session.next_attempt_at = self._next_attempt_at(
                    session.quota_reset_at,
                    finished_at,
                )
            elif returncode == 0:
                session.status = SessionStatus.COMPLETED
                session.terminal = True
                session.next_attempt_at = None
                session.last_error = None
                session.last_resume_finished_at = finished_at
                session.last_resume_result = "success"
            else:
                session.status = SessionStatus.FAILED
                session.terminal = False
                session.last_error = last_reason or f"resume 退出码为 {returncode}"
                session.next_attempt_at = None
                session.last_resume_finished_at = finished_at
                session.last_resume_result = "failed"
            self.registry.upsert_session(session)
            self.registry.record_resume_attempt(
                thread_id=session.thread_id,
                started_at=started_at,
                finished_at=finished_at,
                returncode=returncode,
                error=session.last_error,
            )
            self.logger.info(
                "会话 %s 的 resume 结束，状态为 %s",
                session.session_id,
                session.status.value,
            )
        except (OSError, RuntimeError, RegistryError) as error:
            latest = self.registry.get_session(session.thread_id) or session
            latest.pids = ()
            latest.process_start_tokens = ()
            latest.status = SessionStatus.FAILED
            latest.terminal = False
            latest.last_error = str(error)
            latest.next_attempt_at = None
            finished_at = time.time()
            latest.last_resume_started_at = latest.last_resume_started_at or started_at
            latest.last_resume_finished_at = finished_at
            latest.last_resume_result = "failed"
            self.registry.upsert_session(latest)
            self.registry.record_resume_attempt(
                thread_id=latest.thread_id,
                started_at=started_at,
                finished_at=finished_at,
                returncode=process.returncode if process is not None else None,
                error=str(error),
            )
            self.logger.error("会话 %s 的 resume 失败: %s", latest.session_id, error)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def _apply_resume_line(
        self,
        session: TrackedSession,
        line: str,
        current_session_id: str,
    ) -> tuple[str, EventObservation | None, str | None]:
        """解析续跑输出并持久化 session/额度错误，不保存原始行。"""

        from .events import parse_event_line

        observation = parse_event_line(line)
        if observation.session_id:
            current_session_id = observation.session_id
            session.session_id = current_session_id
        if observation.event_type:
            session.last_event_type = observation.event_type
        session.last_event_at = time.time()
        if observation.reset_at is not None:
            session.quota_reset_at = observation.reset_at
        if observation.quota_exhausted:
            session.last_error = self._safe_quota_reason(observation)
            session.quota_blocked_at = session.last_event_at or time.time()
            session.last_resume_finished_at = None
            session.last_resume_result = "quota_blocked"
        if observation.rate_limits:
            session.metadata["rate_limits_observed"] = "1"
        self.registry.upsert_session(session)
        return (
            current_session_id,
            observation if observation.quota_exhausted else None,
            self._safe_quota_reason(observation)
            if observation.quota_exhausted
            else None,
        )

    @staticmethod
    def _safe_quota_reason(observation: EventObservation) -> str:
        """生成不包含原始提示词和事件文本的额度错误摘要。"""

        event_type = observation.event_type or "unknown"
        return f"Codex 额度限制事件（{event_type}）"

    def _handle_app_server_notification(
        self,
        message: Mapping[str, Any],
    ) -> None:
        """处理额度实时通知和会话状态变化通知。"""

        method = message.get("method")
        params = message.get("params", {})
        if not isinstance(method, str) or not isinstance(params, Mapping):
            return
        if method == "account/rateLimits/updated":
            update = params
            nested_result = params.get("result")
            if isinstance(nested_result, Mapping):
                update = nested_result
            with self._quota_lock:
                updated = merge_sparse_update(
                    self._quota,
                    update,
                    observed_at=time.time(),
                )
                self._quota = updated
            self._quota_is_current = True
            try:
                self.registry.save_quota(updated)
            except RegistryError as error:
                self.logger.warning("保存额度通知失败: %s", error)
            return
        if method in {"thread/status/changed", "thread/updated"}:
            self._last_reconcile_at = None

    def _set_quota(self, snapshot: QuotaSnapshot) -> None:
        """在线程安全的同时更新内存和持久化额度快照。"""

        with self._quota_lock:
            self._quota = snapshot
        self.registry.save_quota(snapshot)

    def _read_fallback_quota(self, now: float) -> QuotaSnapshot | None:
        """App Server 不可达时读取活动或最近 JSONL 的额度事件。"""

        active_paths = tuple(
            path
            for process in self.process_scanner.scan()
            for path in process.open_jsonl_paths
        )
        known_paths = tuple(
            Path(session.jsonl_path)
            for session in self.registry.list_sessions(active_only=False)
            if session.jsonl_path is not None
        )
        paths = recent_session_paths(
            self.session_root,
            active_paths=active_paths,
            known_paths=known_paths,
        )
        return self.quota_fallback_reader.read(paths, now=now)

    def _upsert_app_thread(
        self,
        summary: AppServerThread,
        now: float,
    ) -> None:
        """把 App Server 的活动摘要合并到会话注册表。"""

        session = self.registry.get_session(summary.thread_id)
        if session is None and summary.jsonl_path is not None:
            for candidate in self.registry.list_sessions(active_only=False):
                if candidate.jsonl_path and self._resolved_path(
                    Path(candidate.jsonl_path)
                ) == self._resolved_path(summary.jsonl_path):
                    session = candidate
                    break
        if session is None:
            for candidate in self.registry.list_sessions(active_only=False):
                if candidate.session_id == summary.session_id:
                    session = candidate
                    break
        if session is not None and summary.active:
            # 文件元数据缺失时，注册表可能暂时使用 file:<hash> 作为内部 ID；
            # 给这个别名也加上 App Server 活动证据，避免错误地 orphan。
            self._active_app_threads.add(session.thread_id)
        if session is None:
            session = TrackedSession(
                thread_id=summary.thread_id,
                session_id=summary.session_id,
                jsonl_path=(
                    str(self._resolved_path(summary.jsonl_path))
                    if summary.jsonl_path is not None
                    else None
                ),
                cwd=str(summary.cwd) if summary.cwd else None,
                source=summary.source,
                status=(
                    SessionStatus.WAITING_FOR_APPROVAL
                    if summary.waiting_for_approval
                    else SessionStatus.RUNNING
                ),
                confidence=DetectionConfidence.APP_SERVER,
                first_seen_at=now,
                last_seen_at=now,
                parent_thread_id=summary.parent_thread_id,
                root_thread_id=summary.root_thread_id,
                auto_resume=not self._is_subagent(summary.source),
                account_id=self.config.account_id,
                last_offset=(
                    self.jsonl_reader.initial_offset(summary.jsonl_path)
                    if summary.jsonl_path is not None
                    else 0
                ),
            )
        else:
            session.last_seen_at = now
            session.session_id = summary.session_id or session.session_id
            session.cwd = str(summary.cwd) if summary.cwd else session.cwd
            session.source = summary.source or session.source
            session.parent_thread_id = (
                summary.parent_thread_id or session.parent_thread_id
            )
            session.root_thread_id = summary.root_thread_id or session.root_thread_id
            if summary.jsonl_path is not None:
                session.jsonl_path = str(self._resolved_path(summary.jsonl_path))
            session.confidence = DetectionConfidence.APP_SERVER
            if session.account_id is None:
                session.account_id = self.config.account_id
            if summary.waiting_for_approval:
                session.status = SessionStatus.WAITING_FOR_APPROVAL
                session.metadata["approval_waiting"] = "1"
                session.metadata["auto_resume_disabled_reason"] = "approval"
            elif session.status not in {
                SessionStatus.LIMIT_BLOCKED,
                SessionStatus.QUEUED,
                SessionStatus.RESUMING,
            }:
                session.status = SessionStatus.RUNNING
                if session.metadata.get("auto_resume_disabled_reason") == "approval":
                    session.metadata.pop("approval_waiting", None)
                    session.metadata.pop("auto_resume_disabled_reason", None)
                    session.auto_resume = not self._is_subagent(session.source)
        if summary.waiting_for_approval:
            session.metadata["approval_waiting"] = "1"
            session.metadata["auto_resume_disabled_reason"] = "approval"
            session.auto_resume = False
        self.registry.upsert_session(session)

    def _active_read_offset(
        self,
        path: Path,
        last_offset: int,
        session: TrackedSession | None = None,
    ) -> int:
        """活动会话只追最近追加内容；落后太多时跳到文件尾。"""

        offset = max(0, last_offset)
        try:
            size = path.stat().st_size
        except OSError:
            return offset
        if offset > size:
            offset = 0
        if (
            session is not None
            and session.last_event_type in {"task_complete", "task_completed"}
            and session.metadata.get("quota_blocked") != "1"
            and session.metadata.get("terminal_quota_recheck") != "1"
        ):
            session.metadata["terminal_quota_recheck"] = "1"
            return self.jsonl_reader.initial_offset(path)
        if size - offset > self._ACTIVE_CATCHUP_SKIP_BYTES:
            return self.jsonl_reader.initial_offset(path)
        return offset

    def _new_session(
        self,
        path: Path,
        metadata: SessionMetadata | None,
        process: ProcessObservation,
        now: float,
    ) -> TrackedSession:
        """为没有历史记录的活动 JSONL 创建会话。"""

        identifier = (
            metadata.thread_id
            if metadata and metadata.thread_id
            else self._file_identifier(path)
        )
        session_id = (
            metadata.session_id
            if metadata and metadata.session_id
            else (metadata.thread_id if metadata and metadata.thread_id else "")
        )
        source = metadata.source if metadata and metadata.source else "process"
        auto_resume = not self._is_subagent(source) and bool(session_id)
        metadata_cwd = metadata.cwd if metadata is not None else None
        session_cwd = metadata_cwd or process.cwd
        return TrackedSession(
            thread_id=identifier,
            session_id=session_id,
            jsonl_path=str(path),
            cwd=str(session_cwd) if session_cwd is not None else None,
            source=source,
            status=SessionStatus.RUNNING,
            confidence=DetectionConfidence.OPEN_FILE,
            first_seen_at=now,
            last_seen_at=now,
            pids=(process.pid,),
            process_start_tokens=(process.start_token,),
            last_offset=self.jsonl_reader.initial_offset(path),
            parent_thread_id=(metadata.parent_thread_id if metadata else None),
            root_thread_id=(metadata.root_thread_id if metadata else None),
            auto_resume=auto_resume,
            account_id=self.config.account_id,
        )

    def _refresh_session_from_process(
        self,
        session: TrackedSession,
        path: Path,
        metadata: SessionMetadata | None,
        process: ProcessObservation,
        now: float,
    ) -> TrackedSession:
        """更新活动进程证据并处理新一轮 resume。"""

        session.jsonl_path = str(path)
        session.last_seen_at = now
        session.pids = (process.pid,)
        session.process_start_tokens = (process.start_token,)
        session.confidence = DetectionConfidence.OPEN_FILE
        if metadata is not None:
            session.session_id = metadata.session_id or session.session_id
            session.cwd = str(metadata.cwd) if metadata.cwd else session.cwd
            session.source = metadata.source or session.source
            session.parent_thread_id = (
                metadata.parent_thread_id or session.parent_thread_id
            )
            session.root_thread_id = metadata.root_thread_id or session.root_thread_id
        if not session.cwd and process.cwd:
            session.cwd = str(process.cwd)
        session.auto_resume = session.auto_resume and not self._is_subagent(
            session.source
        )
        if session.account_id is None:
            session.account_id = self.config.account_id
        return session

    def _apply_tail(
        self,
        session: TrackedSession,
        tail: SessionTail,
        now: float,
    ) -> None:
        """把 JSONL 增量中的事件和偏移量写入会话记录。"""

        active_event_types = {
            "task_started",
            "task.started",
            "turn_started",
            "turn.started",
            "response_started",
            "response.started",
            "thread_started",
            "thread.started",
            "item_started",
        }
        session.last_offset = tail.next_offset
        if tail.metadata is not None:
            metadata = tail.metadata
            session.session_id = metadata.session_id or session.session_id
            session.cwd = str(metadata.cwd) if metadata.cwd else session.cwd
            session.source = metadata.source or session.source
            session.parent_thread_id = (
                metadata.parent_thread_id or session.parent_thread_id
            )
            session.root_thread_id = metadata.root_thread_id or session.root_thread_id
        for event in tail.events:
            session.last_event_type = event.event_type or session.last_event_type
            session.last_event_at = event.timestamp or session.last_event_at or now
            observation = event.observation
            if event.event_type in active_event_types:
                # 新一轮 turn 开始后，旧的额度失败/完成标记不再代表当前 turn。
                session.terminal = False
                session.metadata.pop("quota_blocked", None)
                session.metadata.pop("terminal_quota_recheck", None)
                session.quota_reset_at = None
                session.last_error = None
                session.status = SessionStatus.RUNNING
                if session.metadata.get("auto_resume_disabled_reason") in {
                    "approval",
                    "user_cancelled",
                }:
                    if (
                        session.metadata.get("auto_resume_disabled_reason")
                        == "user_cancelled"
                    ):
                        self.registry.clear_resume_cancellation(session.thread_id)
                    session.metadata.pop("approval_waiting", None)
                    session.metadata.pop("auto_resume_disabled_reason", None)
                    session.auto_resume = not self._is_subagent(session.source)
            if observation.session_id and not session.session_id:
                session.session_id = observation.session_id
            if observation.reset_at is not None:
                session.quota_reset_at = observation.reset_at
            if observation.quota_exhausted:
                user_cancelled = (
                    session.metadata.get("auto_resume_disabled_reason")
                    == "user_cancelled"
                )
                session.metadata["quota_blocked"] = "1"
                session.metadata.pop("terminal_quota_recheck", None)
                session.last_error = self._safe_quota_reason(observation)
                session.quota_blocked_at = session.last_event_at or now
                session.last_resume_started_at = None
                session.last_resume_finished_at = None
                session.last_resume_result = (
                    "cancelled" if user_cancelled else "waiting_for_reset"
                )
                session.status = SessionStatus.LIMIT_BLOCKED
                # 额度失败意味着这一轮 turn 结束，但根 session 仍可 resume。
                session.terminal = False
                if session.metadata.get("auto_resume_disabled_reason") not in {
                    "approval",
                    "user_cancelled",
                }:
                    session.auto_resume = not self._is_subagent(session.source)
            if tail.approval_waiting:
                session.metadata["approval_waiting"] = "1"
                session.metadata["auto_resume_disabled_reason"] = "approval"
                session.auto_resume = False
        if tail.terminal_event:
            session.terminal = not bool(tail.quota_events)
        if tail.approval_waiting:
            session.status = SessionStatus.WAITING_FOR_APPROVAL
        elif session.metadata.get("approval_waiting") == "1":
            session.status = SessionStatus.WAITING_FOR_APPROVAL
        elif session.status not in {
            SessionStatus.LIMIT_BLOCKED,
            SessionStatus.QUEUED,
            SessionStatus.RESUMING,
        }:
            session.status = SessionStatus.RUNNING

    def _quota_allows_resume(self, now: float) -> bool:
        """只有主动刷新确认窗口已恢复，才允许启动新的模型 turn。"""

        snapshot = self.refresh_quota(force=True, now=now)
        if snapshot is None or not self._quota_is_current or not snapshot.windows:
            self.logger.warning("本轮没有拿到有效额度窗口，暂停自动 resume")
            self.registry.defer_due_sessions(
                now,
                now + self.config.unknown_reset_wait,
                account_id=self.config.account_id,
            )
            return False
        if (
            snapshot.source == "session-jsonl-fallback"
            and now - snapshot.observed_at > self.config.fallback_max_age
        ):
            self.logger.warning("本地 JSONL 额度快照已过期，暂停自动 resume")
            self.registry.defer_due_sessions(
                now,
                now + self.config.unknown_reset_wait,
                account_id=self.config.account_id,
            )
            return False
        exhausted = snapshot.exhausted_windows
        if not exhausted:
            return True
        future_resets = [
            window.resets_at
            for window in exhausted
            if window.resets_at is not None and window.resets_at > now
        ]
        if future_resets:
            self.registry.defer_due_sessions(
                now,
                max(future_resets) + self.config.reset_grace,
                account_id=self.config.account_id,
            )
            return False
        # 服务端仍标记 exhausted 但没有 reset 时间时不猜测，等待下一轮查询。
        if all(window.resets_at is not None for window in exhausted):
            return True
        self.registry.defer_due_sessions(
            now,
            now + self.config.unknown_reset_wait,
            account_id=self.config.account_id,
        )
        return False

    def _best_reset_at(self, session: TrackedSession) -> float | None:
        """取会话事件和账户快照中可用的最晚阻塞 reset。"""

        snapshot = self.quota
        values = [session.quota_reset_at]
        if snapshot is not None:
            values.append(snapshot.latest_exhausted_reset_at)
        known = [value for value in values if value is not None]
        return max(known) if known else None

    def _session_account_matches_config(self, session: TrackedSession) -> bool:
        """确认会话身份与当前 profile 可用于同一轮自动处理。"""

        return session.account_id == self.config.account_id

    def _session_may_drop_resume(self, session: TrackedSession) -> bool:
        """取消恢复可以覆盖本 profile 未识别会话，但不能改写其他账号队列。"""

        if session.account_id is None:
            return True
        return self._session_account_matches_config(session)

    def _next_attempt_at(self, reset_at: float | None, now: float) -> float:
        """计算队列首次安全尝试时间。"""

        if reset_at is None:
            return now + self.config.unknown_reset_wait
        return max(now, reset_at) + self.config.reset_grace

    @staticmethod
    def _parse_app_thread(item: Mapping[str, Any]) -> AppServerThread | None:
        """兼容 App Server 不同版本的 thread 摘要字段。"""

        thread_id = MultiSessionMonitor._first_string(
            item,
            ("id", "threadId", "thread_id"),
        )
        if not thread_id:
            return None
        session_id = (
            MultiSessionMonitor._first_string(
                item,
                ("sessionId", "session_id", "id"),
            )
            or thread_id
        )
        status = MultiSessionMonitor._status_text(item.get("status"))
        lowered = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", status)
        lowered = lowered.lower().replace("-", "_").replace(" ", "_")
        active = lowered in {
            "active",
            "running",
            "in_progress",
            "inprogress",
            "working",
            "processing",
            "waiting_for_approval",
            "waiting_on_approval",
            "waiting_on_user_input",
            "waiting_for_user_input",
        }
        waiting = "approval" in lowered or "user_input" in lowered
        source = (
            MultiSessionMonitor._first_string(
                item,
                ("sourceKind", "source_kind", "source"),
            )
            or "app-server"
        )
        cwd = MultiSessionMonitor._path_from_keys(
            item,
            ("cwd", "workingDirectory", "working_directory"),
        )
        jsonl_path = MultiSessionMonitor._path_from_keys(
            item,
            ("jsonlPath", "jsonl_path", "rolloutPath", "rollout_path", "path"),
        )
        parent = MultiSessionMonitor._first_string(
            item,
            ("parentThreadId", "parent_thread_id", "parentId", "parent_id"),
        )
        root = MultiSessionMonitor._first_string(
            item,
            ("rootThreadId", "root_thread_id", "rootId", "root_id"),
        )
        return AppServerThread(
            thread_id=thread_id,
            session_id=session_id,
            status=status,
            active=active,
            waiting_for_approval=waiting,
            cwd=cwd,
            jsonl_path=jsonl_path,
            source=source,
            parent_thread_id=parent,
            root_thread_id=root,
        )

    @staticmethod
    def _status_text(value: Any) -> str:
        """将字符串或状态对象归一化成可比较文本。"""

        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            for key in ("type", "status", "state", "kind"):
                child = value.get(key)
                if isinstance(child, str):
                    return child
        return "unknown"

    @staticmethod
    def _first_string(
        data: Mapping[str, Any],
        keys: Sequence[str],
    ) -> str | None:
        """读取候选字段中的第一个非空字符串。"""

        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _path_from_keys(
        data: Mapping[str, Any],
        keys: Sequence[str],
    ) -> Path | None:
        """读取候选路径字段。"""

        value = MultiSessionMonitor._first_string(data, keys)
        return Path(value).expanduser() if value else None

    @staticmethod
    def _is_subagent(source: str) -> bool:
        """子代理不是独立可安全重放的根任务。"""

        normalized = re.sub(r"[^a-z0-9]", "", source.lower())
        return normalized.startswith("subagent")

    @staticmethod
    def _file_identifier(path: Path) -> str:
        """用路径生成内部 ID，不把路径当作 session ID 发送给 Codex。"""

        import hashlib

        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24]
        return f"file:{digest}"

    @staticmethod
    def _resolved_path(path: Path) -> Path:
        """统一路径键，处理 proc fd 和相对工作目录。"""

        try:
            return path.expanduser().resolve()
        except OSError:
            return path.expanduser()

    @staticmethod
    def _find_session(
        by_path: Mapping[Path, TrackedSession],
        by_thread: Mapping[str, TrackedSession],
        path: Path,
        metadata: SessionMetadata | None,
    ) -> TrackedSession | None:
        """优先按 JSONL 路径、再按元数据 thread ID 找回记录。"""

        session = by_path.get(path)
        if session is not None:
            return session
        if metadata and metadata.thread_id:
            return by_thread.get(metadata.thread_id)
        return None

    def _build_resume_command(self, session: TrackedSession) -> list[str]:
        """构造不经过 shell 的 session 续跑命令。"""

        if not session.session_id:
            raise RuntimeError("没有 session ID，拒绝自动 resume")
        return [
            self.config.codex_path,
            "exec",
            "resume",
            "--json",
            session.session_id,
            self.config.continuation_prompt,
        ]

    def _environment(self) -> dict[str, str] | None:
        """为当前账号构造 Codex 子进程环境。"""

        if self.config.codex_home is None:
            return None
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.config.codex_home.expanduser())
        return environment

    def _remove_finished_workers(self) -> None:
        """清理已退出后台线程，使下一个队列项可以获得槽位。"""

        finished = [
            thread_id
            for thread_id, worker in self._resume_workers.items()
            if not worker.thread.is_alive()
        ]
        for thread_id in finished:
            self._resume_workers.pop(thread_id, None)
