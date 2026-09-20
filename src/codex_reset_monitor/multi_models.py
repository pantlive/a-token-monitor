"""多会话监控使用的数据模型。

单个 Codex 账户可以同时运行多个会话，因此这些模型不复用旧版的
``JobState`` 单任务状态。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SessionStatus(str, Enum):
    """被监控会话的生命周期状态。"""

    DISCOVERED = "discovered"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    LIMIT_BLOCKED = "limit_blocked"
    QUEUED = "queued"
    RESUMING = "resuming"
    COMPLETED = "completed"
    FAILED = "failed"
    ORPHANED = "orphaned"
    UNKNOWN = "unknown"


class DetectionConfidence(str, Enum):
    """会话活动状态的证据强度。"""

    APP_SERVER = "app_server"
    OPEN_FILE = "open_file"
    RECENT_FILE = "recent_file"
    PERSISTED = "persisted"


@dataclass
class TrackedSession:
    """一个可持久化的 Codex 会话观察记录。"""

    thread_id: str
    session_id: str
    jsonl_path: str | None
    cwd: str | None
    source: str
    status: SessionStatus
    confidence: DetectionConfidence
    first_seen_at: float
    last_seen_at: float
    pids: tuple[int, ...] = ()
    process_start_tokens: tuple[str, ...] = ()
    last_offset: int = 0
    last_event_at: float | None = None
    last_event_type: str | None = None
    last_error: str | None = None
    quota_reset_at: float | None = None
    blocked_limit_ids: tuple[str, ...] = ()
    next_attempt_at: float | None = None
    auto_resume: bool = True
    resume_attempts: int = 0
    parent_thread_id: str | None = None
    root_thread_id: str | None = None
    last_exit_code: int | None = None
    terminal: bool = False
    quota_blocked_at: float | None = None
    last_resume_started_at: float | None = None
    last_resume_finished_at: float | None = None
    last_resume_result: str | None = None
    account_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def is_process_backed(self) -> bool:
        """判断当前记录是否有打开 JSONL 的活动进程。"""

        return bool(self.pids)

    @property
    def is_active(self) -> bool:
        """判断会话是否仍应出现在活动监控列表。"""

        return self.status in {
            SessionStatus.DISCOVERED,
            SessionStatus.RUNNING,
            SessionStatus.WAITING_FOR_APPROVAL,
            SessionStatus.LIMIT_BLOCKED,
            SessionStatus.QUEUED,
            SessionStatus.RESUMING,
        }

    def to_record(self) -> dict[str, Any]:
        """转换成 SQLite JSON 字段可保存的普通字典。"""

        return {
            "thread_id": self.thread_id,
            "session_id": self.session_id,
            "jsonl_path": self.jsonl_path,
            "cwd": self.cwd,
            "source": self.source,
            "status": self.status.value,
            "confidence": self.confidence.value,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "pids": list(self.pids),
            "process_start_tokens": list(self.process_start_tokens),
            "last_offset": self.last_offset,
            "last_event_at": self.last_event_at,
            "last_event_type": self.last_event_type,
            "last_error": self.last_error,
            "quota_reset_at": self.quota_reset_at,
            "blocked_limit_ids": list(self.blocked_limit_ids),
            "next_attempt_at": self.next_attempt_at,
            "auto_resume": self.auto_resume,
            "resume_attempts": self.resume_attempts,
            "parent_thread_id": self.parent_thread_id,
            "root_thread_id": self.root_thread_id,
            "last_exit_code": self.last_exit_code,
            "terminal": self.terminal,
            "quota_blocked_at": self.quota_blocked_at,
            "last_resume_started_at": self.last_resume_started_at,
            "last_resume_finished_at": self.last_resume_finished_at,
            "last_resume_result": self.last_resume_result,
            "account_id": self.account_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "TrackedSession":
        """从 SQLite 记录恢复会话，并为旧记录提供安全默认值。"""

        def string_value(name: str) -> str | None:
            value = record.get(name)
            return str(value) if value is not None else None

        def float_value(name: str) -> float | None:
            value = record.get(name)
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def int_value(name: str, default: int = 0) -> int:
            value = record.get(name, default)
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        def tuple_of_strings(name: str) -> tuple[str, ...]:
            value = record.get(name, ())
            if isinstance(value, (list, tuple)):
                return tuple(str(item) for item in value)
            return ()

        def tuple_of_ints(name: str) -> tuple[int, ...]:
            value = record.get(name, ())
            if not isinstance(value, (list, tuple)):
                return ()
            result: list[int] = []
            for item in value:
                try:
                    result.append(int(item))
                except (TypeError, ValueError):
                    continue
            return tuple(result)

        raw_status = str(record.get("status", SessionStatus.UNKNOWN.value))
        raw_confidence = str(
            record.get("confidence", DetectionConfidence.PERSISTED.value)
        )
        try:
            status = SessionStatus(raw_status)
        except ValueError:
            status = SessionStatus.UNKNOWN
        try:
            confidence = DetectionConfidence(raw_confidence)
        except ValueError:
            confidence = DetectionConfidence.PERSISTED

        metadata = record.get("metadata", {})
        normalized_metadata = (
            {str(key): str(value) for key, value in metadata.items()}
            if isinstance(metadata, Mapping)
            else {}
        )
        return cls(
            thread_id=str(record.get("thread_id", "")),
            session_id=str(record.get("session_id", "")),
            jsonl_path=string_value("jsonl_path"),
            cwd=string_value("cwd"),
            source=str(record.get("source", "unknown")),
            status=status,
            confidence=confidence,
            first_seen_at=float_value("first_seen_at") or 0.0,
            last_seen_at=float_value("last_seen_at") or 0.0,
            pids=tuple_of_ints("pids"),
            process_start_tokens=tuple_of_strings("process_start_tokens"),
            last_offset=int_value("last_offset"),
            last_event_at=float_value("last_event_at"),
            last_event_type=string_value("last_event_type"),
            last_error=string_value("last_error"),
            quota_reset_at=float_value("quota_reset_at"),
            blocked_limit_ids=tuple_of_strings("blocked_limit_ids"),
            next_attempt_at=float_value("next_attempt_at"),
            auto_resume=bool(record.get("auto_resume", True)),
            resume_attempts=int_value("resume_attempts"),
            parent_thread_id=string_value("parent_thread_id"),
            root_thread_id=string_value("root_thread_id"),
            last_exit_code=(
                int_value("last_exit_code")
                if record.get("last_exit_code") is not None
                else None
            ),
            terminal=bool(record.get("terminal", False)),
            quota_blocked_at=float_value("quota_blocked_at"),
            last_resume_started_at=float_value("last_resume_started_at"),
            last_resume_finished_at=float_value("last_resume_finished_at"),
            last_resume_result=string_value("last_resume_result"),
            account_id=string_value("account_id"),
            metadata=normalized_metadata,
        )
