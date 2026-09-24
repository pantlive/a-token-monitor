"""异常流量告警的落盘存储和历史查询。

告警只保存进程、目录、对端地址和字节数等元数据，不保存网络载荷或会话正文。
同一进程、同一触发规则、同一级别的重复告警会在合并窗口内合并成一条记录，
避免 daemon 重启或抖动时产生大量重复行；被合并的新告警会让已读记录重新变回
未读。保留天数用于过期清理，默认 30 天。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .agents import product_label
from .traffic import TrafficAlert

DEFAULT_RETENTION_DAYS = 30.0
DEFAULT_MERGE_WINDOW_SECONDS = 300.0
DEFAULT_QUERY_LIMIT = 100
MAX_QUERY_LIMIT = 500
_MAX_KEYWORD_LENGTH = 200
_PRUNE_INTERVAL_SECONDS = 3600.0
_LEVELS = ("warn", "danger")
_KINDS = ("burst", "window")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traffic_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    product TEXT NOT NULL,
    pid INTEGER NOT NULL,
    process_key TEXT NOT NULL DEFAULT '',
    command TEXT,
    cwd TEXT,
    remote TEXT,
    bytes INTEGER NOT NULL,
    peak_bytes INTEGER NOT NULL,
    window_seconds REAL NOT NULL,
    message TEXT NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    acknowledged_at REAL
);
CREATE INDEX IF NOT EXISTS traffic_alerts_last_seen
    ON traffic_alerts(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS traffic_alerts_acknowledged
    ON traffic_alerts(acknowledged_at);
CREATE INDEX IF NOT EXISTS traffic_alerts_fingerprint
    ON traffic_alerts(fingerprint, last_seen_at DESC);
"""

_ALERT_COLUMNS = (
    "id, level, kind, product, pid, process_key, command, cwd, remote, "
    "bytes, peak_bytes, window_seconds, message, first_seen_at, last_seen_at, "
    "count, acknowledged_at"
)


class AlertStoreError(RuntimeError):
    """告警历史数据库不可读、不可写或参数非法时抛出的异常。"""


@dataclass(frozen=True)
class StoredAlert:
    """一条已落盘的历史告警。"""

    id: int
    level: str
    kind: str
    product: str
    pid: int
    process_key: str
    command: str | None
    cwd: str | None
    remote: str | None
    bytes: int
    peak_bytes: int
    window_seconds: float
    message: str
    first_seen_at: float
    last_seen_at: float
    count: int
    acknowledged_at: float | None

    @property
    def acknowledged(self) -> bool:
        """是否已标记已读。"""

        return self.acknowledged_at is not None

    def to_dict(self) -> dict[str, Any]:
        """转换为 Dashboard / CLI 展示字段。"""

        return {
            "id": self.id,
            "level": self.level,
            "kind": self.kind,
            "product": self.product,
            "product_label": product_label(self.product),
            "pid": self.pid,
            "process_key": self.process_key,
            "command": self.command,
            "cwd": self.cwd,
            "remote": self.remote,
            "bytes": self.bytes,
            "peak_bytes": self.peak_bytes,
            "window_seconds": self.window_seconds,
            "message": self.message,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "count": self.count,
            "acknowledged": self.acknowledged,
            "acknowledged_at": self.acknowledged_at,
        }


@dataclass(frozen=True)
class AlertQuery:
    """历史告警的筛选条件。"""

    since: float | None = None
    until: float | None = None
    levels: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    products: tuple[str, ...] = ()
    acknowledged: bool | None = None
    keyword: str | None = None
    limit: int = DEFAULT_QUERY_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        """拒绝越界分页和未知级别，避免把脏数据带进查询。"""

        if self.limit <= 0 or self.limit > MAX_QUERY_LIMIT:
            raise AlertStoreError(f"limit 必须在 1 到 {MAX_QUERY_LIMIT} 之间")
        if self.offset < 0:
            raise AlertStoreError("offset 不能小于 0")
        for level in self.levels:
            if level not in _LEVELS:
                raise AlertStoreError(f"未知告警级别: {level}")
        for kind in self.kinds:
            if kind not in _KINDS:
                raise AlertStoreError(f"未知告警规则: {kind}")


class TrafficAlertStore:
    """把异常流量告警持久化到独立 SQLite 文件，并支持历史查询。"""

    def __init__(
        self,
        state_dir: Path,
        retention_days: float = DEFAULT_RETENTION_DAYS,
        merge_window_seconds: float = DEFAULT_MERGE_WINDOW_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        """记录存储位置、保留天数和重复告警合并窗口。"""

        if retention_days <= 0:
            raise ValueError("retention_days 必须大于 0")
        if merge_window_seconds < 0:
            raise ValueError("merge_window_seconds 不能小于 0")
        self.state_dir = Path(state_dir).expanduser()
        self.database_file = self.state_dir / "traffic-alerts.sqlite3"
        self.retention_days = float(retention_days)
        self.merge_window_seconds = float(merge_window_seconds)
        self.logger = logger or logging.getLogger(__name__)
        self._retention_seconds = self.retention_days * 86400.0
        self._writer_lock = threading.Lock()
        self._last_prune_at = 0.0

    @property
    def db_path(self) -> Path:
        """告警历史 SQLite 文件路径，供历史数据管理器统计占用。"""

        return self.database_file

    # ---------------------------------------------------------------- 写入

    def record(
        self,
        alerts: Sequence[TrafficAlert],
        now: float | None = None,
    ) -> tuple[StoredAlert, ...]:
        """落盘一批新告警，并合并窗口内的重复告警。

        中文注释:过期清理由 HistoryDataManager 按天统一调用 prune 执行,
        写入路径不再顺带清理,避免偶发的大批量删除拖慢告警落盘。
        """

        observed_at = time.time() if now is None else float(now)
        with self._writer_lock:
            self._ensure_database()
            if not alerts:
                return ()
            stored: list[StoredAlert] = []
            with closing(self._connect()) as connection:
                for alert in alerts:
                    stored.append(
                        self._record_one(connection, alert, observed_at=observed_at)
                    )
                connection.commit()
            return tuple(stored)

    def _record_one(
        self,
        connection: sqlite3.Connection,
        alert: TrafficAlert,
        observed_at: float,
    ) -> StoredAlert:
        """插入一条告警，或把窗口内的重复告警合并进已有记录。"""

        fingerprint = _fingerprint(alert)
        seen_at = float(alert.observed_at or observed_at)
        existing = connection.execute(
            "SELECT id, count, peak_bytes, first_seen_at, last_seen_at "
            "FROM traffic_alerts WHERE fingerprint = ? "
            "ORDER BY last_seen_at DESC, id DESC LIMIT 1",
            (fingerprint,),
        ).fetchone()
        if existing is not None and (
            seen_at - float(existing["last_seen_at"]) <= self.merge_window_seconds
        ):
            connection.execute(
                "UPDATE traffic_alerts SET bytes = ?, "
                "peak_bytes = MAX(peak_bytes, ?), message = ?, "
                "last_seen_at = ?, count = count + 1, "
                "acknowledged_at = NULL, remote = ?, cwd = ?, command = ?, "
                "pid = ?, process_key = ? WHERE id = ?",
                (
                    int(alert.bytes),
                    int(alert.bytes),
                    alert.message,
                    seen_at,
                    alert.remote,
                    alert.cwd,
                    alert.command,
                    int(alert.pid),
                    alert.process_key,
                    int(existing["id"]),
                ),
            )
            row_id = int(existing["id"])
        else:
            cursor = connection.execute(
                "INSERT INTO traffic_alerts (fingerprint, level, kind, product, "
                "pid, process_key, command, cwd, remote, bytes, peak_bytes, "
                "window_seconds, message, first_seen_at, last_seen_at, count, "
                "acknowledged_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)",
                (
                    fingerprint,
                    alert.level,
                    alert.kind,
                    alert.product,
                    int(alert.pid),
                    alert.process_key,
                    alert.command,
                    alert.cwd,
                    alert.remote,
                    int(alert.bytes),
                    int(alert.bytes),
                    float(alert.window_seconds),
                    alert.message,
                    seen_at,
                    seen_at,
                ),
            )
            row_id = int(cursor.lastrowid or 0)
        row = connection.execute(
            f"SELECT {_ALERT_COLUMNS} FROM traffic_alerts WHERE id = ?",
            (row_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - 只在并发删除时触发
            raise AlertStoreError("告警写入后无法读回记录")
        return _row_to_alert(row)

    # ---------------------------------------------------------------- 查询

    def query(self, query: AlertQuery | None = None) -> tuple[StoredAlert, ...]:
        """按条件返回历史告警，按最近出现时间倒序。"""

        rows, _ = self.query_page(query)
        return rows

    def query_page(
        self,
        query: AlertQuery | None = None,
    ) -> tuple[tuple[StoredAlert, ...], bool]:
        """返回一页历史告警，并说明是否还有更多记录。"""

        criteria = query or AlertQuery()
        if not self.database_file.exists():
            return (), False
        where, parameters = _where_clause(criteria)
        statement = (
            f"SELECT {_ALERT_COLUMNS} FROM traffic_alerts{where} "
            "ORDER BY last_seen_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        parameters.extend((criteria.limit + 1, criteria.offset))
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(statement, parameters).fetchall()
        except sqlite3.Error as error:
            raise AlertStoreError(f"读取告警历史失败: {error}") from error
        has_more = len(rows) > criteria.limit
        return tuple(_row_to_alert(row) for row in rows[: criteria.limit]), has_more

    def stats(self, since: float | None = None) -> dict[str, Any]:
        """返回总数、未读数、级别分布和最近一条告警时间。"""

        empty = {
            "total": 0,
            "unread": 0,
            "danger": 0,
            "warn": 0,
            "last_alert_at": None,
        }
        if not self.database_file.exists():
            return empty
        where = ""
        parameters: list[Any] = []
        if since is not None:
            where = " WHERE last_seen_at >= ?"
            parameters.append(float(since))
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT COUNT(*) AS total, "
                    "SUM(CASE WHEN acknowledged_at IS NULL THEN 1 ELSE 0 END) "
                    "AS unread, "
                    "SUM(CASE WHEN level = 'danger' THEN 1 ELSE 0 END) AS danger, "
                    "SUM(CASE WHEN level = 'warn' THEN 1 ELSE 0 END) AS warn, "
                    "MAX(last_seen_at) AS last_alert_at "
                    f"FROM traffic_alerts{where}",
                    parameters,
                ).fetchone()
        except sqlite3.Error as error:
            raise AlertStoreError(f"读取告警统计失败: {error}") from error
        if row is None:
            return empty
        return {
            "total": int(row["total"] or 0),
            "unread": int(row["unread"] or 0),
            "danger": int(row["danger"] or 0),
            "warn": int(row["warn"] or 0),
            "last_alert_at": (
                float(row["last_alert_at"])
                if row["last_alert_at"] is not None
                else None
            ),
        }

    # ------------------------------------------------------------ 已读状态

    def acknowledge(
        self,
        ids: Sequence[int] | None = None,
        *,
        all_alerts: bool = False,
        now: float | None = None,
    ) -> int:
        """把指定告警（或全部未读告警）标记为已读，返回改动条数。"""

        return self._set_acknowledged(
            ids,
            all_alerts=all_alerts,
            acknowledged_at=time.time() if now is None else float(now),
        )

    def unacknowledge(self, ids: Sequence[int] | None = None) -> int:
        """把指定告警恢复为未读，返回改动条数。"""

        return self._set_acknowledged(ids, all_alerts=False, acknowledged_at=None)

    def _set_acknowledged(
        self,
        ids: Sequence[int] | None,
        *,
        all_alerts: bool,
        acknowledged_at: float | None,
    ) -> int:
        if not all_alerts and not ids:
            return 0
        if not self.database_file.exists():
            return 0
        if all_alerts:
            where = (
                "WHERE acknowledged_at IS NULL"
                if acknowledged_at is not None
                else ""
            )
            parameters: list[Any] = []
        else:
            placeholders = ", ".join("?" for _ in ids or ())
            where = f"WHERE id IN ({placeholders})"
            parameters = [int(item) for item in ids or ()]
        if acknowledged_at is not None:
            assignments = "acknowledged_at = ?"
            parameters = [acknowledged_at, *parameters]
        else:
            assignments = "acknowledged_at = NULL"
        try:
            with closing(self._connect()) as connection:
                cursor = connection.execute(
                    f"UPDATE traffic_alerts SET {assignments} {where}",
                    parameters,
                )
                connection.commit()
                return int(cursor.rowcount or 0)
        except sqlite3.Error as error:
            raise AlertStoreError(f"更新告警已读状态失败: {error}") from error

    # ---------------------------------------------------------------- 清理

    def count_all(self) -> int:
        """返回全部历史告警条数，供历史数据预览使用。"""

        if not self.database_file.exists():
            return 0
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM traffic_alerts"
                ).fetchone()
        except sqlite3.Error as error:
            raise AlertStoreError(f"统计告警总数失败: {error}") from error
        return int(row["total"] or 0) if row is not None else 0

    def vacuum(self) -> None:
        """先截断 WAL 再压缩告警数据库;数据库忙时抛错由上层跳过。"""

        if not self.database_file.exists():
            return
        try:
            with closing(self._connect()) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")
            self._protect_database_files()
        except sqlite3.Error as error:
            raise AlertStoreError(f"压缩告警历史数据库失败: {error}") from error

    def count_before(self, timestamp: float) -> int:
        """返回早于指定时间的历史告警条数，用于清理预览。"""

        if not self.database_file.exists():
            return 0
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM traffic_alerts "
                    "WHERE last_seen_at < ?",
                    (float(timestamp),),
                ).fetchone()
        except sqlite3.Error as error:
            raise AlertStoreError(f"统计待清理告警失败: {error}") from error
        return int(row["total"] or 0) if row is not None else 0

    def clear(self, ids: Sequence[int] | None = None) -> int:
        """删除指定告警，返回删除条数。"""

        if not ids or not self.database_file.exists():
            return 0
        placeholders = ", ".join("?" for _ in ids)
        parameters = [int(item) for item in ids]
        return self._delete(
            f"WHERE id IN ({placeholders})",
            parameters,
            "删除指定告警失败",
        )

    def clear_before(self, timestamp: float) -> int:
        """删除早于指定时间的告警，返回删除条数。"""

        if not self.database_file.exists():
            return 0
        return self._delete(
            "WHERE last_seen_at < ?",
            [float(timestamp)],
            "清理历史告警失败",
        )

    def clear_all(self) -> int:
        """删除全部历史告警，返回删除条数。"""

        if not self.database_file.exists():
            return 0
        return self._delete("", [], "清空历史告警失败")

    def _delete(
        self,
        where: str,
        parameters: Sequence[Any],
        message: str,
    ) -> int:
        try:
            with closing(self._connect()) as connection:
                cursor = connection.execute(
                    f"DELETE FROM traffic_alerts {where}",
                    list(parameters),
                )
                connection.commit()
                return int(cursor.rowcount or 0)
        except sqlite3.Error as error:
            raise AlertStoreError(f"{message}: {error}") from error

    def prune(self, now: float | None = None, *, force: bool = False) -> int:
        """按保留天数清理过期告警，返回清理条数。"""

        observed_at = time.time() if now is None else float(now)
        with self._writer_lock:
            if not self.database_file.exists():
                return 0
            if not force and not self._prune_due(observed_at):
                return 0
            return self._prune_locked(observed_at)

    def _prune_due(self, now: float) -> bool:
        return now - self._last_prune_at >= _PRUNE_INTERVAL_SECONDS

    def _prune_locked(self, now: float) -> int:
        self._last_prune_at = now
        cutoff = now - self._retention_seconds
        try:
            with closing(self._connect()) as connection:
                cursor = connection.execute(
                    "DELETE FROM traffic_alerts WHERE last_seen_at < ?",
                    (cutoff,),
                )
                connection.commit()
                removed = int(cursor.rowcount or 0)
        except sqlite3.Error as error:
            raise AlertStoreError(f"清理过期告警失败: {error}") from error
        if removed:
            self.logger.info(
                "已清理 %d 条超过 %.0f 天的异常流量告警",
                removed,
                self.retention_days,
            )
        return removed

    # ------------------------------------------------------------ 数据库

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_file, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_database(self) -> None:
        """按需创建状态目录、告警表，并限制文件权限。"""

        if self.database_file.exists():
            return
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.state_dir.chmod(0o700)
            with closing(self._connect()) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.executescript(_SCHEMA)
                connection.commit()
            self._protect_database_files()
        except (OSError, sqlite3.Error) as error:
            raise AlertStoreError(f"无法创建告警历史数据库: {error}") from error

    def _protect_database_files(self) -> None:
        """把数据库及 WAL 文件限制为当前用户可读写。"""

        for path in (
            self.database_file,
            self.database_file.with_name(self.database_file.name + "-wal"),
            self.database_file.with_name(self.database_file.name + "-shm"),
        ):
            try:
                if path.exists():
                    path.chmod(0o600)
            except OSError:  # pragma: no cover - 权限受限时保持原状
                continue


def _fingerprint(alert: TrafficAlert) -> str:
    """同一进程、规则、级别的告警视为同一条，用于重复合并。"""

    process = alert.process_key or f"{alert.product}:{alert.pid}"
    return "|".join((process, alert.kind, alert.level))


def _where_clause(query: AlertQuery) -> tuple[str, list[Any]]:
    """把筛选条件翻译成 SQL WHERE 子句和参数。"""

    clauses: list[str] = []
    parameters: list[Any] = []
    if query.since is not None:
        clauses.append("last_seen_at >= ?")
        parameters.append(float(query.since))
    if query.until is not None:
        clauses.append("last_seen_at <= ?")
        parameters.append(float(query.until))
    if query.levels:
        placeholders = ", ".join("?" for _ in query.levels)
        clauses.append(f"level IN ({placeholders})")
        parameters.extend(query.levels)
    if query.kinds:
        placeholders = ", ".join("?" for _ in query.kinds)
        clauses.append(f"kind IN ({placeholders})")
        parameters.extend(query.kinds)
    if query.products:
        placeholders = ", ".join("?" for _ in query.products)
        clauses.append(f"product IN ({placeholders})")
        parameters.extend(query.products)
    if query.acknowledged is not None:
        clauses.append(
            "acknowledged_at IS NOT NULL"
            if query.acknowledged
            else "acknowledged_at IS NULL"
        )
    keyword = (query.keyword or "").strip()[:_MAX_KEYWORD_LENGTH]
    if keyword:
        pattern = _like_pattern(keyword)
        clauses.append(
            "(message LIKE ? ESCAPE '\\' OR COALESCE(command, '') LIKE ? ESCAPE '\\' "
            "OR COALESCE(cwd, '') LIKE ? ESCAPE '\\' "
            "OR COALESCE(remote, '') LIKE ? ESCAPE '\\')"
        )
        parameters.extend([pattern] * 4)
    if not clauses:
        return "", parameters
    return " WHERE " + " AND ".join(clauses), parameters


def _like_pattern(keyword: str) -> str:
    """转义 LIKE 通配符，避免用户输入变成通配查询。"""

    escaped = (
        keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def _row_to_alert(row: Mapping[str, Any]) -> StoredAlert:
    """把 SQLite 行转换为不可变告警模型。"""

    return StoredAlert(
        id=int(row["id"]),
        level=str(row["level"]),
        kind=str(row["kind"]),
        product=str(row["product"]),
        pid=int(row["pid"]),
        process_key=str(row["process_key"] or ""),
        command=row["command"],
        cwd=row["cwd"],
        remote=row["remote"],
        bytes=int(row["bytes"] or 0),
        peak_bytes=int(row["peak_bytes"] or 0),
        window_seconds=float(row["window_seconds"] or 0.0),
        message=str(row["message"]),
        first_seen_at=float(row["first_seen_at"]),
        last_seen_at=float(row["last_seen_at"]),
        count=int(row["count"] or 1),
        acknowledged_at=(
            float(row["acknowledged_at"])
            if row["acknowledged_at"] is not None
            else None
        ),
    )
