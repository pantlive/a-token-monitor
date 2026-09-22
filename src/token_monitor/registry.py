"""多会话监控的 SQLite 持久化。

数据库只保存恢复会话所需的标识、状态和额度窗口，不保存原始提示词、OAuth
Token 或完整 JSONL 内容。每个操作使用独立连接，便于监控主循环和续跑线程
安全地并发更新同一份状态。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .multi_models import SessionStatus, TrackedSession
from .quota import QuotaSnapshot, QuotaWindow


class RegistryError(RuntimeError):
    """多会话状态库不可用时抛出的异常。"""


class _AccountFilterUnset:
    """表示调用方没有要求按账号过滤。"""


_ACCOUNT_FILTER_UNSET = _AccountFilterUnset()


class MultiSessionRegistry:
    """保存所有发现会话和最近账户额度快照的 SQLite 注册表。"""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir.expanduser()
        self.database_file = self.state_dir / "monitor.sqlite3"
        self._ensure_database()

    def _ensure_database(self) -> None:
        """创建受保护的状态目录和数据库表。"""

        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.state_dir.chmod(0o700)
            with self._connection() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        thread_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        account_id TEXT,
                        jsonl_path TEXT,
                        cwd TEXT,
                        source TEXT NOT NULL,
                        status TEXT NOT NULL,
                        confidence TEXT NOT NULL,
                        first_seen_at REAL NOT NULL,
                        last_seen_at REAL NOT NULL,
                        pids_json TEXT NOT NULL,
                        process_start_tokens_json TEXT NOT NULL,
                        last_offset INTEGER NOT NULL,
                        last_event_at REAL,
                        last_event_type TEXT,
                        last_error TEXT,
                        quota_reset_at REAL,
                        quota_blocked_at REAL,
                        blocked_limit_ids_json TEXT NOT NULL,
                        next_attempt_at REAL,
                        auto_resume INTEGER NOT NULL,
                        resume_attempts INTEGER NOT NULL,
                        parent_thread_id TEXT,
                        root_thread_id TEXT,
                        last_exit_code INTEGER,
                        terminal INTEGER NOT NULL,
                        last_resume_started_at REAL,
                        last_resume_finished_at REAL,
                        last_resume_result TEXT,
                        metadata_json TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_sessions_status_attempt
                        ON sessions(status, next_attempt_at);
                    CREATE TABLE IF NOT EXISTS quota_snapshot (
                        snapshot_id INTEGER PRIMARY KEY CHECK (snapshot_id = 1),
                        observed_at REAL NOT NULL,
                        plan_type TEXT,
                        source TEXT NOT NULL,
                        raw_limit_ids_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS quota_windows (
                        limit_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        used_percent REAL,
                        window_minutes REAL,
                        resets_at REAL,
                        reached_type TEXT,
                        PRIMARY KEY (limit_id, name)
                    );
                    CREATE TABLE IF NOT EXISTS resume_attempts (
                        attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        thread_id TEXT NOT NULL,
                        started_at REAL NOT NULL,
                        finished_at REAL,
                        returncode INTEGER,
                        error TEXT
                    );
                    CREATE TABLE IF NOT EXISTS resume_cancellations (
                        thread_id TEXT PRIMARY KEY,
                        cancelled_at REAL NOT NULL
                    );
                        """
                )
                self._ensure_session_columns(connection)
                self._backfill_recovery_fields(connection)
            self._protect_database_files()
        except (OSError, sqlite3.Error) as error:
            raise RegistryError(f"无法初始化多会话状态库: {error}") from error

    @staticmethod
    def _ensure_session_columns(connection: sqlite3.Connection) -> None:
        """为已有状态库补充额度恢复字段，保持升级兼容。"""

        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(sessions)")
        }
        columns_to_add = (
            ("account_id", "TEXT"),
            ("quota_blocked_at", "REAL"),
            ("last_resume_started_at", "REAL"),
            ("last_resume_finished_at", "REAL"),
            ("last_resume_result", "TEXT"),
        )
        for name, column_type in columns_to_add:
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE sessions ADD COLUMN {name} {column_type}"
                )

    @staticmethod
    def _backfill_recovery_fields(connection: sqlite3.Connection) -> None:
        """用旧版续跑表回填可识别的恢复时间和结果。"""

        connection.execute(
            """
            UPDATE sessions
            SET last_resume_started_at = COALESCE(
                    last_resume_started_at,
                    (
                        SELECT started_at
                        FROM resume_attempts
                        WHERE resume_attempts.thread_id = sessions.thread_id
                        ORDER BY attempt_id DESC
                        LIMIT 1
                    )
                ),
                last_resume_finished_at = COALESCE(
                    last_resume_finished_at,
                    (
                        SELECT finished_at
                        FROM resume_attempts
                        WHERE resume_attempts.thread_id = sessions.thread_id
                        ORDER BY attempt_id DESC
                        LIMIT 1
                    )
                ),
                last_resume_result = COALESCE(
                    last_resume_result,
                    CASE
                        WHEN NOT EXISTS (
                            SELECT 1
                            FROM resume_attempts
                            WHERE resume_attempts.thread_id = sessions.thread_id
                        ) THEN NULL
                        WHEN (
                            SELECT returncode
                            FROM resume_attempts
                            WHERE resume_attempts.thread_id = sessions.thread_id
                            ORDER BY attempt_id DESC
                            LIMIT 1
                        ) = 0 THEN 'success'
                        WHEN (
                            SELECT error
                            FROM resume_attempts
                            WHERE resume_attempts.thread_id = sessions.thread_id
                            ORDER BY attempt_id DESC
                            LIMIT 1
                        ) LIKE '%额度%' THEN 'quota_blocked'
                        ELSE 'failed'
                    END
                )
            WHERE resume_attempts > 0
            """
        )

    def _protect_database_files(self) -> None:
        """限制数据库及已生成的 WAL 文件权限。"""

        for path in (
            self.database_file,
            self.database_file.with_name(f"{self.database_file.name}-wal"),
            self.database_file.with_name(f"{self.database_file.name}-shm"),
        ):
            try:
                if path.exists():
                    path.chmod(0o600)
            except OSError:
                continue

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """创建一个启用行对象和外键约束的 SQLite 连接。"""

        connection = sqlite3.connect(self.database_file, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._protect_database_files()

    def upsert_session(self, session: TrackedSession) -> None:
        """插入或完整更新一个会话记录。"""

        record = session.to_record()
        values = (
            record["thread_id"],
            record["session_id"],
            record["account_id"],
            record["jsonl_path"],
            record["cwd"],
            record["source"],
            record["status"],
            record["confidence"],
            record["first_seen_at"],
            record["last_seen_at"],
            json.dumps(record["pids"], ensure_ascii=False),
            json.dumps(record["process_start_tokens"], ensure_ascii=False),
            record["last_offset"],
            record["last_event_at"],
            record["last_event_type"],
            record["last_error"],
            record["quota_reset_at"],
            record["quota_blocked_at"],
            json.dumps(record["blocked_limit_ids"], ensure_ascii=False),
            record["next_attempt_at"],
            int(record["auto_resume"]),
            record["resume_attempts"],
            record["parent_thread_id"],
            record["root_thread_id"],
            record["last_exit_code"],
            int(record["terminal"]),
            record["last_resume_started_at"],
            record["last_resume_finished_at"],
            record["last_resume_result"],
            json.dumps(record["metadata"], ensure_ascii=False),
        )
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO sessions (
                        thread_id, session_id, account_id, jsonl_path, cwd,
                        source, status,
                        confidence, first_seen_at, last_seen_at, pids_json,
                        process_start_tokens_json, last_offset, last_event_at,
                        last_event_type, last_error, quota_reset_at,
                        quota_blocked_at,
                        blocked_limit_ids_json, next_attempt_at, auto_resume,
                        resume_attempts, parent_thread_id, root_thread_id,
                        last_exit_code, terminal, last_resume_started_at,
                        last_resume_finished_at, last_resume_result,
                        metadata_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET
                        session_id=excluded.session_id,
                        account_id=excluded.account_id,
                        jsonl_path=excluded.jsonl_path,
                        cwd=excluded.cwd,
                        source=excluded.source,
                        status=excluded.status,
                        confidence=excluded.confidence,
                        first_seen_at=excluded.first_seen_at,
                        last_seen_at=excluded.last_seen_at,
                        pids_json=excluded.pids_json,
                        process_start_tokens_json=excluded.process_start_tokens_json,
                        last_offset=excluded.last_offset,
                        last_event_at=excluded.last_event_at,
                        last_event_type=excluded.last_event_type,
                        last_error=excluded.last_error,
                        quota_reset_at=excluded.quota_reset_at,
                        quota_blocked_at=excluded.quota_blocked_at,
                        blocked_limit_ids_json=excluded.blocked_limit_ids_json,
                        next_attempt_at=excluded.next_attempt_at,
                        auto_resume=excluded.auto_resume,
                        resume_attempts=excluded.resume_attempts,
                        parent_thread_id=excluded.parent_thread_id,
                        root_thread_id=excluded.root_thread_id,
                        last_exit_code=excluded.last_exit_code,
                        terminal=excluded.terminal,
                        last_resume_started_at=excluded.last_resume_started_at,
                        last_resume_finished_at=excluded.last_resume_finished_at,
                        last_resume_result=excluded.last_resume_result,
                        metadata_json=excluded.metadata_json,
                        updated_at=excluded.updated_at
                    """,
                    values + (session.last_seen_at,),
                )
        except sqlite3.Error as error:
            raise RegistryError(f"无法保存会话 {session.thread_id}: {error}") from error

    def get_session(self, thread_id: str) -> TrackedSession | None:
        """按 thread ID 读取一个会话。"""

        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM sessions WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise RegistryError(f"无法读取会话 {thread_id}: {error}") from error
        return self._row_to_session(row) if row is not None else None

    def list_sessions(
        self,
        active_only: bool = False,
    ) -> list[TrackedSession]:
        """按最近观察时间倒序返回会话。"""

        active_values = tuple(
            status.value
            for status in SessionStatus
            if status
            not in {
                SessionStatus.COMPLETED,
                SessionStatus.FAILED,
                SessionStatus.ORPHANED,
                SessionStatus.UNKNOWN,
            }
        )
        try:
            with self._connection() as connection:
                if active_only:
                    placeholders = ",".join("?" for _ in active_values)
                    rows = connection.execute(
                        f"SELECT * FROM sessions WHERE status IN ({placeholders}) "
                        "ORDER BY last_seen_at DESC",
                        active_values,
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT * FROM sessions ORDER BY last_seen_at DESC"
                    ).fetchall()
        except sqlite3.Error as error:
            raise RegistryError(f"无法列出会话: {error}") from error
        return [self._row_to_session(row) for row in rows]

    def list_recovery_sessions(self, limit: int = 50) -> list[TrackedSession]:
        """返回最近发生过额度阻塞和恢复尝试的会话。"""

        if limit <= 0:
            return []
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM sessions
                    WHERE (
                        quota_blocked_at IS NOT NULL
                        OR resume_attempts > 0
                    )
                      AND last_resume_result IS NOT NULL
                    ORDER BY COALESCE(
                        last_resume_finished_at,
                        last_resume_started_at,
                        quota_blocked_at
                    ) DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except sqlite3.Error as error:
            raise RegistryError(f"无法列出额度恢复记录: {error}") from error
        return [self._row_to_session(row) for row in rows]

    def cancel_queued_resume(self, thread_id: str, now: float) -> str:
        """取消一个等待中的自动恢复，同时保留会话和额度中断历史。"""

        cancellation_reason = "已从 Dashboard 手动取消自动恢复"
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM sessions WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
                if row is None:
                    return "not_found"
                session = self._row_to_session(row)
                if session.last_resume_result not in {
                    "waiting_for_reset",
                    "quota_blocked",
                    "failed",
                }:
                    return "not_waiting"
                metadata = dict(session.metadata)
                metadata["auto_resume_disabled_reason"] = "user_cancelled"
                last_error = session.last_error or ""
                if cancellation_reason not in last_error:
                    last_error = (
                        f"{last_error}；{cancellation_reason}"
                        if last_error
                        else cancellation_reason
                    )
                cancelled_status = (
                    SessionStatus.FAILED.value
                    if session.last_resume_result == "failed"
                    else SessionStatus.LIMIT_BLOCKED.value
                )
                connection.execute(
                    """
                    UPDATE sessions
                    SET status = ?, auto_resume = 0, next_attempt_at = NULL,
                        last_error = ?, last_resume_result = ?,
                        metadata_json = ?, updated_at = ?
                    WHERE thread_id = ?
                    """,
                    (
                        cancelled_status,
                        last_error,
                        "cancelled",
                        json.dumps(metadata, ensure_ascii=False),
                        now,
                        thread_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO resume_cancellations (thread_id, cancelled_at)
                    VALUES (?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET
                        cancelled_at = excluded.cancelled_at
                    """,
                    (thread_id, now),
                )
                return "cancelled"
        except sqlite3.Error as error:
            raise RegistryError(
                f"无法取消会话 {thread_id} 的自动恢复: {error}"
            ) from error

    def is_resume_cancelled(self, thread_id: str) -> bool:
        """判断会话是否有独立于 session 快照的人工取消标记。"""

        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT 1 FROM resume_cancellations WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise RegistryError(
                f"无法读取会话 {thread_id} 的取消状态: {error}"
            ) from error
        return row is not None

    def clear_resume_cancellation(self, thread_id: str) -> None:
        """在用户明确开始新 turn 后清除上一轮人工取消标记。"""

        try:
            with self._connection() as connection:
                connection.execute(
                    "DELETE FROM resume_cancellations WHERE thread_id = ?",
                    (thread_id,),
                )
        except sqlite3.Error as error:
            raise RegistryError(f"无法清除会话 {thread_id} 的取消状态: {error}") from error

    def save_quota(self, snapshot: QuotaSnapshot) -> None:
        """原子保存账户额度快照和所有窗口。"""

        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO quota_snapshot (
                        snapshot_id, observed_at, plan_type, source,
                        raw_limit_ids_json, metadata_json
                    ) VALUES (1, ?, ?, ?, ?, ?)
                    ON CONFLICT(snapshot_id) DO UPDATE SET
                        observed_at=excluded.observed_at,
                        plan_type=excluded.plan_type,
                        source=excluded.source,
                        raw_limit_ids_json=excluded.raw_limit_ids_json,
                        metadata_json=excluded.metadata_json
                    """,
                    (
                        snapshot.observed_at,
                        snapshot.plan_type,
                        snapshot.source,
                        json.dumps(snapshot.raw_limit_ids, ensure_ascii=False),
                        json.dumps(snapshot.metadata, ensure_ascii=False),
                    ),
                )
                connection.execute("DELETE FROM quota_windows")
                connection.executemany(
                    """
                    INSERT INTO quota_windows (
                        limit_id, name, used_percent, window_minutes,
                        resets_at, reached_type
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            window.limit_id,
                            window.name,
                            window.used_percent,
                            window.window_minutes,
                            window.resets_at,
                            window.reached_type,
                        )
                        for window in snapshot.windows
                    ),
                )
        except sqlite3.Error as error:
            raise RegistryError(f"无法保存额度快照: {error}") from error

    def load_quota(self) -> QuotaSnapshot | None:
        """读取最近一次完整额度快照。"""

        try:
            with self._connection() as connection:
                snapshot_row = connection.execute(
                    "SELECT * FROM quota_snapshot WHERE snapshot_id = 1"
                ).fetchone()
                window_rows = connection.execute(
                    "SELECT * FROM quota_windows ORDER BY limit_id, name"
                ).fetchall()
        except sqlite3.Error as error:
            raise RegistryError(f"无法读取额度快照: {error}") from error
        if snapshot_row is None:
            return None
        return QuotaSnapshot(
            observed_at=float(snapshot_row["observed_at"]),
            windows=tuple(
                QuotaWindow(
                    limit_id=str(row["limit_id"]),
                    name=str(row["name"]),
                    used_percent=self._optional_float(row["used_percent"]),
                    window_minutes=self._optional_float(row["window_minutes"]),
                    resets_at=self._optional_float(row["resets_at"]),
                    reached_type=(
                        str(row["reached_type"])
                        if row["reached_type"] is not None
                        else None
                    ),
                )
                for row in window_rows
            ),
            plan_type=(
                str(snapshot_row["plan_type"])
                if snapshot_row["plan_type"] is not None
                else None
            ),
            source=str(snapshot_row["source"]),
            raw_limit_ids=self._json_tuple(
                snapshot_row["raw_limit_ids_json"],
            ),
            metadata=self._json_string_dict(snapshot_row["metadata_json"]),
        )

    def claim_due_session(
        self,
        now: float,
        account_id: str | None | _AccountFilterUnset = _ACCOUNT_FILTER_UNSET,
    ) -> TrackedSession | None:
        """原子领取当前账号的到期队列项，避免错误 resume。"""

        account_clause, account_values = self._account_filter(account_id)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT * FROM sessions
                    WHERE status = ?
                      AND auto_resume = 1
                      AND terminal = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM resume_cancellations
                          WHERE resume_cancellations.thread_id = sessions.thread_id
                      )
                      AND next_attempt_at IS NOT NULL
                      AND next_attempt_at <= ?
                      AND {account_clause}
                    ORDER BY next_attempt_at ASC, last_seen_at ASC
                    LIMIT 1
                    """.format(account_clause=account_clause),
                    (SessionStatus.QUEUED.value, now, *account_values),
                ).fetchone()
                if row is None:
                    return None
                connection.execute(
                    """
                    UPDATE sessions
                    SET status = ?, resume_attempts = resume_attempts + 1,
                        updated_at = ?
                    WHERE thread_id = ? AND status = ?
                    """,
                    (
                        SessionStatus.RESUMING.value,
                        now,
                        row["thread_id"],
                        SessionStatus.QUEUED.value,
                    ),
                )
                values = dict(row)
                values["status"] = SessionStatus.RESUMING.value
                values["resume_attempts"] = int(row["resume_attempts"]) + 1
                return self._row_to_session(values)
        except sqlite3.Error as error:
            raise RegistryError(f"无法领取续跑队列: {error}") from error

    def has_due_session(
        self,
        now: float,
        account_id: str | None | _AccountFilterUnset = _ACCOUNT_FILTER_UNSET,
    ) -> bool:
        """判断当前账号是否存在已经到期且允许自动续跑的会话。"""

        account_clause, account_values = self._account_filter(account_id)
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT 1 FROM sessions
                    WHERE status = ?
                      AND auto_resume = 1
                      AND terminal = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM resume_cancellations
                          WHERE resume_cancellations.thread_id = sessions.thread_id
                      )
                      AND next_attempt_at IS NOT NULL
                      AND next_attempt_at <= ?
                      AND {account_clause}
                    LIMIT 1
                    """.format(account_clause=account_clause),
                    (SessionStatus.QUEUED.value, now, *account_values),
                ).fetchone()
        except sqlite3.Error as error:
            raise RegistryError(f"无法检查续跑队列: {error}") from error
        return row is not None

    def record_resume_attempt(
        self,
        thread_id: str,
        started_at: float,
        finished_at: float | None,
        returncode: int | None,
        error: str | None,
    ) -> None:
        """记录一次续跑尝试，不写入命令提示词。"""

        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO resume_attempts (
                        thread_id, started_at, finished_at, returncode, error
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (thread_id, started_at, finished_at, returncode, error),
                )
        except sqlite3.Error as db_error:
            raise RegistryError(
                f"无法保存会话 {thread_id} 的续跑记录: {db_error}"
            ) from db_error

    def defer_due_sessions(
        self,
        now: float,
        next_attempt_at: float,
        account_id: str | None | _AccountFilterUnset = _ACCOUNT_FILTER_UNSET,
    ) -> int:
        """把当前账号额度仍阻塞的到期项整体推迟，避免忙轮询。"""

        account_clause, account_values = self._account_filter(account_id)
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE sessions
                    SET next_attempt_at = ?, updated_at = ?
                    WHERE status = ?
                      AND auto_resume = 1
                      AND terminal = 0
                      AND next_attempt_at IS NOT NULL
                      AND next_attempt_at <= ?
                      AND {account_clause}
                    """.format(account_clause=account_clause),
                    (
                        next_attempt_at,
                        now,
                        SessionStatus.QUEUED.value,
                        now,
                        *account_values,
                    ),
                )
                return cursor.rowcount
        except sqlite3.Error as error:
            raise RegistryError(f"无法推迟续跑队列: {error}") from error

    @staticmethod
    def _account_filter(
        account_id: str | None | _AccountFilterUnset,
    ) -> tuple[str, tuple[str, ...]]:
        """生成账号队列过滤条件，避免身份未知记录跨账号执行。"""

        if isinstance(account_id, _AccountFilterUnset):
            return "1 = 1", ()
        if account_id is None:
            return "account_id IS NULL", ()
        return "account_id = ?", (account_id,)

    @staticmethod
    def _row_to_session(row: Mapping[str, Any]) -> TrackedSession:
        """把 SQLite 行的 JSON 列解码为领域模型。"""

        data = dict(row)
        for column in (
            "pids_json",
            "process_start_tokens_json",
            "blocked_limit_ids_json",
            "metadata_json",
        ):
            target = column.removesuffix("_json")
            data[target] = MultiSessionRegistry._json_value(data.get(column))
        data.pop("updated_at", None)
        return TrackedSession.from_record(data)

    @staticmethod
    def _json_value(value: Any) -> Any:
        """解码 JSON 列，损坏时返回安全空值。"""

        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []

    @staticmethod
    def _json_tuple(value: Any) -> tuple[str, ...]:
        """读取字符串元组 JSON。"""

        decoded = MultiSessionRegistry._json_value(value)
        if isinstance(decoded, (list, tuple)):
            return tuple(str(item) for item in decoded)
        return ()

    @staticmethod
    def _json_string_dict(value: Any) -> dict[str, str]:
        """读取字符串字典 JSON。"""

        decoded = MultiSessionRegistry._json_value(value)
        if isinstance(decoded, Mapping):
            return {str(key): str(item) for key, item in decoded.items()}
        return {}

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        """把可空数据库数字转换为浮点数。"""

        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
