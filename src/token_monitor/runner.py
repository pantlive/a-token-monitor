"""Codex CLI 进程管理、额度等待和 session 续跑。"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from .events import EventObservation, RateLimitWindow, parse_event_line
from .models import JobState, JobStatus
from .storage import StateError, StateStore


class MonitorError(RuntimeError):
    """监控流程无法安全继续时抛出的异常。"""


@dataclass(frozen=True)
class RunnerConfig:
    """运行器的等待参数，所有时间单位均为秒。"""

    poll_interval: float = 30.0
    reset_grace: float = 30.0
    unknown_reset_wait: float = 900.0

    def __post_init__(self) -> None:
        """拒绝无意义的负数等待配置。"""

        if self.poll_interval <= 0:
            raise ValueError("poll_interval 必须大于 0")
        if self.reset_grace < 0:
            raise ValueError("reset_grace 不能小于 0")
        if self.unknown_reset_wait <= 0:
            raise ValueError("unknown_reset_wait 必须大于 0")


@dataclass(frozen=True)
class ProcessResult:
    """一次 Codex 调用的结果。"""

    returncode: int
    quota_exhausted: bool
    session_id: str | None
    reset_at: float | None
    reason: str | None


class CodexRunner:
    """以持久化状态驱动一次 Codex 任务及其后续续跑。"""

    def __init__(
        self,
        store: StateStore,
        config: RunnerConfig | None = None,
        logger: logging.Logger | None = None,
        codex_home: Path | None = None,
    ) -> None:
        self.store = store
        self.config = config or RunnerConfig()
        self.logger = logger or logging.getLogger(__name__)
        self.codex_home = codex_home.expanduser() if codex_home else None

    def start(
        self,
        cwd: Path,
        codex_path: str,
        prompt: str,
        continuation_prompt: str,
        codex_options: Sequence[str],
    ) -> int:
        """创建任务并启动首次 Codex 调用。"""

        normalized_cwd = cwd.expanduser().resolve()
        if not normalized_cwd.is_dir():
            raise MonitorError(f"工作目录不存在或不是目录: {normalized_cwd}")

        with self.store.lock():
            self._ensure_no_conflicting_job()
            job_id = self._new_job_id()
            log_file = self.store.create_log_file(job_id)

            state = JobState.create(
                cwd=str(normalized_cwd),
                codex_path=codex_path,
                prompt=prompt,
                continuation_prompt=continuation_prompt,
                codex_options=list(codex_options),
                log_file=str(log_file),
                codex_home=(
                    str(self.codex_home) if self.codex_home is not None else None
                ),
            )
            state.job_id = job_id
            self.store.save(state)
            self.logger.info("开始监控任务 %s，日志: %s", state.job_id, log_file)
            return self._drive(state, resume=False)

    def watch(self) -> int:
        """继续等待状态为 waiting_for_reset 的任务并自动续跑。"""

        with self.store.lock():
            state = self._load_state()
            self._apply_codex_home(state)
            if state.status == JobStatus.WAITING_FOR_RESET:
                return self._drive(state, resume=True, wait_for_reset=True)

            if state.status == JobStatus.RUNNING:
                if self._pid_is_alive(state.pid):
                    raise MonitorError(
                        f"任务仍在运行中（PID {state.pid}），请不要重复启动 watch"
                    )
                state.status = JobStatus.ORPHANED
                state.pid = None
                state.last_error = "监控器重启时发现原 Codex 进程已不存在"
                self.store.save(state)
                raise MonitorError(
                    "原 Codex 进程已结束但状态未知；为避免重复执行，"
                    "请检查仓库后使用 `resume --force` 明确续跑"
                )

            raise MonitorError(f"当前任务状态为 {state.status.value}，无需 watch")

    def resume_now(self, force: bool = False) -> int:
        """手动续跑一个等待或孤立任务。"""

        with self.store.lock():
            state = self._load_state()
            self._apply_codex_home(state)
            if state.session_id is None:
                raise MonitorError("任务尚未记录 Codex session ID，无法安全续跑")

            if state.status == JobStatus.WAITING_FOR_RESET and not force:
                next_attempt = state.next_attempt_at
                if next_attempt is not None and next_attempt > time.time():
                    raise MonitorError(
                        "预计额度尚未恢复；如确认要立即尝试，请添加 --force"
                    )

            if state.status in {JobStatus.ORPHANED, JobStatus.FAILED} and not force:
                raise MonitorError(
                    "该任务的上一次结束原因不确定；请检查仓库后添加 --force 续跑"
                )

            if state.status == JobStatus.RUNNING and self._pid_is_alive(state.pid):
                raise MonitorError(f"任务仍在运行中（PID {state.pid}）")

            if state.status not in {
                JobStatus.WAITING_FOR_RESET,
                JobStatus.ORPHANED,
                JobStatus.FAILED,
            }:
                raise MonitorError(f"状态 {state.status.value} 不允许手动续跑")

            state.next_attempt_at = None
            state.status = JobStatus.RUNNING
            state.last_error = None
            self.store.save(state)
            self.logger.info("手动续跑任务 %s", state.job_id)
            return self._drive(state, resume=True)

    def _drive(
        self,
        state: JobState,
        resume: bool,
        wait_for_reset: bool = False,
    ) -> int:
        """循环执行 Codex，额度失败时等待并续跑。"""

        try:
            if wait_for_reset:
                self._wait_for_reset(state)

            next_resume = resume
            while True:
                state.status = JobStatus.RUNNING
                state.next_attempt_at = None
                self.store.save(state)
                result = self._execute_once(state, resume=next_resume)
                state.pid = None
                state.last_exit_code = result.returncode

                if result.session_id is not None:
                    state.session_id = result.session_id
                if result.quota_exhausted:
                    if state.session_id is None:
                        state.status = JobStatus.FAILED
                        state.last_error = (
                            "识别到额度限制，但没有拿到 session ID，无法安全续跑"
                        )
                        self.store.save(state)
                        return 1

                    state.status = JobStatus.WAITING_FOR_RESET
                    state.retry_count += 1
                    state.reset_at = result.reset_at
                    state.next_attempt_at = self._next_attempt_at(result.reset_at)
                    state.last_error = result.reason or "Codex 因额度限制结束"
                    self.store.save(state)
                    self.logger.warning(
                        "任务 %s 命中额度限制，预计 %s 后尝试续跑",
                        state.job_id,
                        self._format_epoch(state.next_attempt_at),
                    )
                    self._wait_for_reset(state)
                    next_resume = True
                    continue

                state.reset_at = None
                state.next_attempt_at = None
                if result.returncode == 0:
                    state.status = JobStatus.COMPLETED
                    state.last_error = None
                    self.store.save(state)
                    self.logger.info("任务 %s 已完成", state.job_id)
                    return 0

                state.status = JobStatus.FAILED
                state.last_error = result.reason or (
                    f"Codex 退出码为 {result.returncode}"
                )
                self.store.save(state)
                self.logger.error(
                    "任务 %s 失败，退出码: %s；不会自动重试非额度错误",
                    state.job_id,
                    result.returncode,
                )
                return result.returncode if result.returncode > 0 else 1
        except KeyboardInterrupt:
            # 等待阶段中断时保留 waiting 状态；执行阶段中断则明确标记取消。
            state.pid = None
            if state.status == JobStatus.WAITING_FOR_RESET:
                self.store.save(state)
                self.logger.warning(
                    "已暂停监控，等待状态已保存在 %s",
                    self.store.state_file,
                )
            else:
                state.status = JobStatus.CANCELLED
                state.last_error = "用户中断监控或 Codex 进程"
                self.store.save(state)
                self.logger.warning("任务 %s 已取消", state.job_id)
            return 130
        except MonitorError as error:
            state.pid = None
            state.status = JobStatus.FAILED
            state.last_error = str(error)
            self.store.save(state)
            raise
        except (OSError, StateError) as error:
            state.pid = None
            state.status = JobStatus.FAILED
            state.last_error = str(error)
            self.store.save(state)
            raise MonitorError(str(error)) from error

    def _execute_once(self, state: JobState, resume: bool) -> ProcessResult:
        """执行一次初始或 resume 命令，并实时解析输出。"""

        working_directory = Path(state.cwd)
        if not working_directory.is_dir():
            raise MonitorError(f"工作目录不存在或不是目录: {working_directory}")

        command = self._build_command(state, resume=resume)
        command_preview = " ".join(command[:4])
        self.logger.info("启动 Codex（%s）", command_preview)
        try:
            process = subprocess.Popen(
                command,
                cwd=working_directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=self._environment(state),
            )
        except FileNotFoundError as error:
            raise MonitorError(
                f"找不到 Codex 可执行文件: {state.codex_path}"
            ) from error

        state.pid = process.pid
        self.store.save(state)
        quota_exhausted = False
        latest_reset_at: float | None = None
        latest_reason: str | None = None
        observed_session_id = state.session_id

        try:
            if process.stdout is not None:
                for line in process.stdout:
                    self._forward_line(state, line)
                    observation = parse_event_line(line)
                    changed = self._apply_observation(
                        state,
                        observation,
                    )
                    if observation.session_id is not None:
                        observed_session_id = observation.session_id
                    if observation.reset_at is not None:
                        latest_reset_at = observation.reset_at
                    if observation.quota_exhausted:
                        quota_exhausted = True
                        latest_reason = observation.reason
                    if observation.rate_limits:
                        changed = (
                            self._apply_rate_limits(
                                state,
                                observation.rate_limits,
                            )
                            or changed
                        )
                    if changed:
                        self.store.save(state)
            returncode = process.wait()
        except KeyboardInterrupt:
            self._terminate_process(process)
            raise
        finally:
            if process.poll() is None:
                process.wait()
            state.pid = None
            self.store.save(state)

        return ProcessResult(
            returncode=returncode,
            quota_exhausted=quota_exhausted,
            session_id=observed_session_id,
            reset_at=latest_reset_at,
            reason=latest_reason,
        )

    def _apply_observation(
        self,
        state: JobState,
        observation: EventObservation,
    ) -> bool:
        """把事件中的 session 和 reset 时间更新到状态。"""

        changed = False
        if observation.session_id is not None and (
            state.session_id != observation.session_id
        ):
            state.session_id = observation.session_id
            changed = True
        if observation.reset_at is not None and state.reset_at != observation.reset_at:
            state.reset_at = observation.reset_at
            changed = True
        return changed

    @staticmethod
    def _apply_rate_limits(
        state: JobState,
        windows: Sequence[RateLimitWindow],
    ) -> bool:
        """把最近事件中的额度窗口快照保存到任务状态。"""

        snapshot = {
            window.name: {
                "used_percent": window.used_percent,
                "window_minutes": window.window_minutes,
                "reset_at": window.reset_at,
            }
            for window in windows
        }
        if state.rate_limits == snapshot:
            return False
        state.rate_limits = snapshot
        return True

    def _forward_line(self, state: JobState, line: str) -> None:
        """同时保存和转发 Codex 原始输出。"""

        self.store.append_log(state, line)
        sys.stdout.write(line)
        sys.stdout.flush()

    def _build_command(self, state: JobState, resume: bool) -> list[str]:
        """构造不经过 shell 的 Codex 参数列表。"""

        if resume:
            if state.session_id is None:
                raise MonitorError("没有 session ID，无法构造 resume 命令")
            global_options, resume_options = self._split_resume_options(
                state.codex_options
            )
            return [
                state.codex_path,
                "exec",
                *global_options,
                "resume",
                "--json",
                *resume_options,
                state.session_id,
                state.continuation_prompt,
            ]
        return [
            state.codex_path,
            "exec",
            "--json",
            *state.codex_options,
            state.prompt,
        ]

    @staticmethod
    def _split_resume_options(
        options: Sequence[str],
    ) -> tuple[list[str], list[str]]:
        """把 resume 子命令不接受的 exec 全局选项放到正确位置。"""

        global_options: list[str] = []
        resume_options: list[str] = []
        index = 0
        while index < len(options):
            option = options[index]
            if option == "--sandbox":
                if index + 1 >= len(options):
                    raise MonitorError("--sandbox 缺少模式值")
                global_options.extend([option, options[index + 1]])
                index += 2
                continue
            if option.startswith("--sandbox="):
                global_options.append(option)
            else:
                resume_options.append(option)
            index += 1
        return global_options, resume_options

    def _wait_for_reset(self, state: JobState) -> None:
        """等待预计恢复时间，并定期保存心跳。"""

        if state.next_attempt_at is None:
            state.next_attempt_at = self._next_attempt_at(state.reset_at)
            self.store.save(state)

        target = state.next_attempt_at
        self.logger.info(
            "等待额度恢复至 %s（可用 Ctrl-C 暂停，之后运行 watch）",
            self._format_epoch(target),
        )
        next_report = 0.0
        while True:
            remaining = target - time.time()
            if remaining <= 0:
                break
            now = time.time()
            if now >= next_report:
                self.logger.info("额度等待中，剩余约 %.0f 秒", remaining)
                next_report = now + 60.0
                self.store.save(state)
            time.sleep(min(self.config.poll_interval, remaining))

        state.next_attempt_at = None
        self.store.save(state)
        self.logger.info("到达预计 reset 时间，开始尝试续跑")

    def _next_attempt_at(self, reset_at: float | None) -> float:
        """计算首次安全尝试时间。"""

        now = time.time()
        if reset_at is None:
            return now + self.config.unknown_reset_wait
        return max(now, reset_at) + self.config.reset_grace

    def _ensure_no_conflicting_job(self) -> None:
        """拒绝覆盖仍活跃的任务状态。"""

        existing = self.store.load()
        if existing is None or not existing.is_active:
            return
        if existing.status == JobStatus.RUNNING and not self._pid_is_alive(
            existing.pid
        ):
            existing.status = JobStatus.ORPHANED
            existing.pid = None
            existing.last_error = "检测到旧监控状态，但对应进程已不存在"
            self.store.save(existing)
            raise MonitorError(
                "发现一个孤立任务状态；请检查仓库后运行 `resume --force`，"
                "或手动处理状态文件后再开始新任务"
            )
        raise MonitorError(
            f"已有活跃任务 {existing.job_id}（状态 {existing.status.value}），"
            "请先使用 watch 或 resume"
        )

    def _load_state(self) -> JobState:
        """读取当前状态并给出统一错误。"""

        state = self.store.load()
        if state is None:
            raise MonitorError(
                f"没有任务状态，请先运行 run；状态目录: {self.store.state_dir}"
            )
        return state

    def _apply_codex_home(self, state: JobState) -> None:
        """为旧状态补上命令行指定的登录目录。"""

        if state.codex_home is None and self.codex_home is not None:
            state.codex_home = str(self.codex_home)
            self.store.save(state)

    def _environment(self, state: JobState) -> dict[str, str] | None:
        """为任务子进程构造隔离的 ``CODEX_HOME`` 环境。"""

        codex_home = state.codex_home or (
            str(self.codex_home) if self.codex_home is not None else None
        )
        if codex_home is None:
            return None
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(Path(codex_home).expanduser())
        return environment

    @staticmethod
    def _pid_is_alive(pid: int | None) -> bool:
        """以零信号检查进程是否存在，不向进程发送实际信号。"""

        if pid is None:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        """处理中断时优雅终止子进程，超时后再强制结束。"""

        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    @staticmethod
    def _format_epoch(epoch: float | None) -> str:
        """将 Unix 时间显示为本地时间。"""

        if epoch is None:
            return "未知"
        return time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(epoch))

    @staticmethod
    def _new_job_id() -> str:
        """生成任务 ID，独立于 Codex session ID。"""

        return str(uuid4())
