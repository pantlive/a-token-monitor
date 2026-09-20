"""任务状态模型。

本模块只描述持久化数据，不负责调用外部进程，便于离线测试。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class JobStatus(str, Enum):
    """监控任务的生命周期状态。"""

    RUNNING = "running"
    WAITING_FOR_RESET = "waiting_for_reset"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"


def utc_now_iso() -> str:
    """返回带时区的 UTC ISO 8601 时间。"""

    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobState:
    """可恢复 Codex 任务的持久化状态。"""

    job_id: str
    status: JobStatus
    cwd: str
    codex_path: str
    prompt: str
    continuation_prompt: str
    codex_options: list[str]
    log_file: str
    created_at: str
    updated_at: str
    session_id: str | None = None
    pid: int | None = None
    reset_at: float | None = None
    next_attempt_at: float | None = None
    rate_limits: dict[str, dict[str, float | None]] = field(default_factory=dict)
    retry_count: int = 0
    last_exit_code: int | None = None
    last_error: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    codex_home: str | None = None

    @classmethod
    def create(
        cls,
        cwd: str,
        codex_path: str,
        prompt: str,
        continuation_prompt: str,
        codex_options: list[str],
        log_file: str,
        codex_home: str | None = None,
    ) -> "JobState":
        """创建一个新的监控任务状态。"""

        now = utc_now_iso()
        return cls(
            job_id=str(uuid4()),
            status=JobStatus.RUNNING,
            cwd=cwd,
            codex_path=codex_path,
            prompt=prompt,
            continuation_prompt=continuation_prompt,
            codex_options=list(codex_options),
            log_file=log_file,
            created_at=now,
            updated_at=now,
            codex_home=codex_home,
        )

    @property
    def is_active(self) -> bool:
        """判断任务是否仍可能拥有外部 Codex 进程。"""

        return self.status in {
            JobStatus.RUNNING,
            JobStatus.WAITING_FOR_RESET,
        }

    def touch(self) -> None:
        """更新最后一次状态写入时间。"""

        self.updated_at = utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        """转换为可 JSON 序列化的字典。"""

        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobState":
        """从 JSON 字典恢复任务状态，并校验关键字段。"""

        required_fields = {
            "job_id",
            "status",
            "cwd",
            "codex_path",
            "prompt",
            "continuation_prompt",
            "codex_options",
            "log_file",
            "created_at",
            "updated_at",
        }
        missing_fields = sorted(required_fields.difference(data))
        if missing_fields:
            missing = ", ".join(missing_fields)
            raise ValueError(f"状态文件缺少字段: {missing}")

        options = data["codex_options"]
        if not isinstance(options, list) or not all(
            isinstance(option, str) for option in options
        ):
            raise ValueError("状态文件中的 codex_options 必须是字符串列表")

        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("状态文件中的 metadata 必须是对象")

        rate_limits = data.get("rate_limits", {})
        if not isinstance(rate_limits, dict):
            raise ValueError("状态文件中的 rate_limits 必须是对象")
        normalized_rate_limits: dict[str, dict[str, float | None]] = {}
        for window_name, window_data in rate_limits.items():
            if not isinstance(window_data, dict):
                raise ValueError("rate_limits 中的窗口必须是对象")
            normalized_window: dict[str, float | None] = {}
            for field_name, field_value in window_data.items():
                if field_value is None:
                    normalized_window[str(field_name)] = None
                else:
                    try:
                        normalized_window[str(field_name)] = float(field_value)
                    except (TypeError, ValueError) as error:
                        raise ValueError(
                            "rate_limits 窗口中的值必须是数字或 null"
                        ) from error
            normalized_rate_limits[str(window_name)] = normalized_window

        return cls(
            job_id=str(data["job_id"]),
            status=JobStatus(str(data["status"])),
            cwd=str(data["cwd"]),
            codex_path=str(data["codex_path"]),
            prompt=str(data["prompt"]),
            continuation_prompt=str(data["continuation_prompt"]),
            codex_options=list(options),
            log_file=str(data["log_file"]),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            session_id=(
                str(data["session_id"]) if data.get("session_id") is not None else None
            ),
            pid=int(data["pid"]) if data.get("pid") is not None else None,
            reset_at=(
                float(data["reset_at"]) if data.get("reset_at") is not None else None
            ),
            next_attempt_at=(
                float(data["next_attempt_at"])
                if data.get("next_attempt_at") is not None
                else None
            ),
            rate_limits=normalized_rate_limits,
            retry_count=int(data.get("retry_count", 0)),
            last_exit_code=(
                int(data["last_exit_code"])
                if data.get("last_exit_code") is not None
                else None
            ),
            last_error=(
                str(data["last_error"]) if data.get("last_error") is not None else None
            ),
            metadata={str(key): str(value) for key, value in metadata.items()},
            codex_home=(
                str(data["codex_home"]) if data.get("codex_home") is not None else None
            ),
        )
