"""监控器互斥锁和本地状态文件的安全持久化。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - 只会在 Windows 分支触发
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - 只会在 POSIX 分支触发
    msvcrt = None  # type: ignore[assignment]

from .models import JobState


class StateError(RuntimeError):
    """状态文件不可读或不可写时抛出的异常。"""


class StateStore:
    """以一个当前任务状态和按任务分隔的日志文件保存监控数据。"""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir.expanduser()
        self.state_file = self.state_dir / "state.json"
        self.jobs_dir = self.state_dir / "jobs"
        self.lock_file = self.state_dir / "monitor.lock"

    def _ensure_directories(self) -> None:
        """创建目录并限制状态目录权限。"""

        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)
        self.jobs_dir.chmod(0o700)

    def create_log_file(self, job_id: str) -> Path:
        """为任务创建一个权限为 0600 的原始输出日志。"""

        self._ensure_directories()
        log_file = self.jobs_dir / f"{job_id}.log"
        log_file.touch(mode=0o600, exist_ok=False)
        log_file.chmod(0o600)
        return log_file

    @contextmanager
    def lock(self) -> Iterator[None]:
        """独占监控锁，防止同一状态目录启动多个监控器。"""

        self._ensure_directories()
        handle = self.lock_file.open("a+", encoding="utf-8")
        self.lock_file.chmod(0o600)
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise StateError(
                        "监控器已在运行，请不要同时启动第二个监控器"
                    ) from error
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                # Windows 的 locking 需要先确保文件至少有一个字节。
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write("0")
                    handle.flush()
                handle.seek(0)
                locked = False
                try:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    except OSError as error:
                        raise StateError(
                            "监控器已在运行，请不要同时启动第二个监控器"
                        ) from error
                    locked = True
                    yield
                finally:
                    if locked:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                raise StateError("当前平台没有可用的监控锁实现")
        finally:
            handle.close()

    def append_log(self, state: JobState, line: str) -> None:
        """追加一行 Codex 原始输出，不改变其 JSONL/文本格式。"""

        log_file = Path(state.log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(line)
            if not line.endswith("\n"):
                handle.write("\n")
        log_file.chmod(0o600)

    def save(self, state: JobState) -> None:
        """原子写入状态文件，并将状态文件权限限制为 0600。"""

        self._ensure_directories()
        state.touch()
        content = json.dumps(
            state.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        temporary_file = self.state_file.with_suffix(".json.tmp")
        temporary_file.write_text(f"{content}\n", encoding="utf-8")
        temporary_file.chmod(0o600)
        temporary_file.replace(self.state_file)
        self.state_file.chmod(0o600)

    def load(self) -> JobState | None:
        """读取当前状态；状态文件不存在时返回 None。"""

        if not self.state_file.exists():
            return None

        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("顶层 JSON 必须是对象")
            return JobState.from_dict(data)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise StateError(f"无法读取状态文件 {self.state_file}: {error}") from error
