"""用量与会话历史的保留期配置、预览和安全清理。

保留天数优先级:Web 配置(settings.json)> 命令行参数 > 默认值。
清理只删除过期的历史行:用量索引只删 ``usage_delta`` 明细,绝不动
``usage_file_state`` 里的增量读取检查点;会话历史只删已结束且无恢复历史的行,
活动会话和额度恢复记录始终保留。删除后对数据库做 VACUUM 压缩。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from .alerts import TrafficAlertStore
from .health import sanitize_error
from .registry import MultiSessionRegistry
from .usage import _UsageIndexStore


DEFAULT_USAGE_RETENTION_DAYS = 90.0
DEFAULT_SESSION_RETENTION_DAYS = 30.0
MAX_RETENTION_DAYS = 3650.0
SETTINGS_FILENAME = "settings.json"

RETENTION_KEYS = ("usage_days", "session_days")

_SECONDS_PER_DAY = 86400.0


class RetentionError(ValueError):
    """保留期配置或历史数据清理失败。"""


def _check_days(value: float) -> float:
    """校验保留天数范围。"""

    days = float(value)
    if not 0 < days <= MAX_RETENTION_DAYS:
        raise RetentionError(
            f"保留天数必须在 (0, {MAX_RETENTION_DAYS:.0f}] 之间: {value}"
        )
    return days


@dataclass(frozen=True)
class RetentionSettings:
    """持久化到状态目录的 Web 保留期覆盖配置。"""

    SCHEMA_VERSION: ClassVar[int] = 1

    # 中文注释:只记录 Web 显式设置过的键;缺省表示跟随命令行/默认值。
    overrides: Mapping[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "overrides": {key: self.overrides[key] for key in sorted(self.overrides)},
        }

    def save(self, path: Path) -> None:
        """原子保存配置并限制为当前用户可读写。"""

        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(
            self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
        )
        temporary_path = path.with_suffix(".json.tmp")
        temporary_path.write_text(f"{payload}\n", encoding="utf-8")
        temporary_path.chmod(0o600)
        temporary_path.replace(path)
        path.chmod(0o600)

    @classmethod
    def load(cls, path: Path) -> RetentionSettings:
        """从磁盘读取并严格校验;文件不存在时视为没有覆盖。"""

        try:
            raw_payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RetentionError(f"无法读取保留期配置 {path}: {error}") from error
        if not isinstance(raw_payload, Mapping):
            raise RetentionError("保留期配置根节点必须是 JSON 对象")
        schema_version = raw_payload.get("schema_version")
        if schema_version != cls.SCHEMA_VERSION:
            raise RetentionError(f"不支持的保留期配置版本: {schema_version}")
        overrides_value = raw_payload.get("overrides")
        if overrides_value is None:
            return cls()
        if not isinstance(overrides_value, Mapping):
            raise RetentionError("保留期配置 overrides 必须是对象")
        overrides: dict[str, float] = {}
        for key, value in overrides_value.items():
            if key not in RETENTION_KEYS:
                raise RetentionError(f"保留期配置包含未知键: {key}")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RetentionError(f"保留期配置 overrides.{key} 必须是数值")
            overrides[key] = _check_days(value)
        return cls(overrides=overrides)

    def with_override(self, key: str, days: float) -> RetentionSettings:
        if key not in RETENTION_KEYS:
            raise RetentionError(f"未知的保留期键: {key}")
        overrides = dict(self.overrides)
        overrides[key] = _check_days(days)
        return RetentionSettings(overrides=overrides)

    def without_override(self, key: str) -> RetentionSettings:
        if key not in RETENTION_KEYS:
            raise RetentionError(f"未知的保留期键: {key}")
        if key not in self.overrides:
            return self
        overrides = dict(self.overrides)
        del overrides[key]
        return RetentionSettings(overrides=overrides)


class RetentionController:
    """设置页使用的保留期状态与修改入口(线程安全)。"""

    def __init__(
        self,
        state_dir: Path,
        cli_values: Mapping[str, float],
        *,
        reload_callback: Callable[[Mapping[str, float]], None] | None = None,
        config_path: Path | None = None,
    ) -> None:
        """加载持久化覆盖并记录命令行层取值。"""

        self.state_dir = state_dir.expanduser()
        self.config_path = config_path or self.state_dir / SETTINGS_FILENAME
        self._cli_values = {
            key: _check_days(cli_values[key]) for key in RETENTION_KEYS
        }
        self._config = RetentionSettings.load(self.config_path)
        self._reload_callback = reload_callback
        self._lock = threading.Lock()

    @property
    def reload_callback(
        self,
    ) -> Callable[[Mapping[str, float]], None] | None:
        return self._reload_callback

    @reload_callback.setter
    def reload_callback(
        self,
        callback: Callable[[Mapping[str, float]], None] | None,
    ) -> None:
        self._reload_callback = callback

    def effective(self) -> dict[str, float]:
        """返回两个保留期键的生效值。"""

        with self._lock:
            return {
                key: self._config.overrides.get(key, self._cli_values[key])
                for key in RETENTION_KEYS
            }

    def snapshot(self) -> dict[str, object]:
        """返回 GET /api/history 使用的保留期状态。"""

        with self._lock:
            config = self._config
        retention: dict[str, object] = {}
        for key in RETENTION_KEYS:
            override = config.overrides.get(key)
            retention[key] = {
                "value": override if override is not None else self._cli_values[key],
                "source": "web" if override is not None else "cli",
                "cli_value": self._cli_values[key],
                "override": override,
            }
        return {"retention": retention}

    def apply(
        self,
        action: str,
        *,
        usage_days: float | None = None,
        session_days: float | None = None,
    ) -> dict[str, object]:
        """应用修改,持久化后触发热生效,返回最新状态。"""

        with self._lock:
            if action == "set":
                config = self._config
                if usage_days is not None:
                    config = config.with_override("usage_days", usage_days)
                if session_days is not None:
                    config = config.with_override("session_days", session_days)
                if config is self._config:
                    return self.snapshot()
            elif action == "reset":
                config = self._config.without_override(
                    "usage_days"
                ).without_override("session_days")
            else:
                raise RetentionError(f"不支持的操作: {action}")
            config.save(self.config_path)
            self._config = config
            effective = {
                key: config.overrides.get(key, self._cli_values[key])
                for key in RETENTION_KEYS
            }
        if self._reload_callback is not None:
            self._reload_callback(effective)
        return self.snapshot()


def db_file_bytes(path: Path) -> int:
    """返回 sqlite 文件含 wal/shm/journal 兄弟文件的总字节数。"""

    total = 0
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(f"{path}{suffix}")
        try:
            if candidate.is_file():
                total += candidate.stat().st_size
        except OSError:
            continue
    return total


@dataclass(frozen=True)
class CleanupKindResult:
    """一类历史数据的清理结果。"""

    kind: str
    deleted_rows: int
    db_bytes_before: int
    db_bytes_after: int


class HistoryDataManager:
    """用量索引、会话历史和告警的预览与安全清理。"""

    def __init__(
        self,
        state_dir: Path,
        registries: Callable[[], Mapping[str, MultiSessionRegistry]],
        alert_store: TrafficAlertStore | None,
        *,
        usage_days: float,
        session_days: float,
        alert_days: float,
        usage_store_path: Path | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """创建管理器;registries 用回调以便热重载后看到最新账号集合。"""

        self.state_dir = state_dir.expanduser()
        self._registries = registries
        self._alert_store = alert_store
        self._usage_store_path = (
            usage_store_path or self.state_dir / "usage-index.sqlite3"
        )
        self._usage_days = _check_days(usage_days)
        self._session_days = _check_days(session_days)
        self._alert_days = _check_days(alert_days)
        self.logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._last_cleanup: dict[str, object] | None = None

    def update_retention(
        self,
        usage_days: float | None = None,
        session_days: float | None = None,
    ) -> None:
        """热更新保留天数;None 表示不变。"""

        with self._lock:
            if usage_days is not None:
                self._usage_days = _check_days(usage_days)
            if session_days is not None:
                self._session_days = _check_days(session_days)

    @property
    def retention_days(self) -> dict[str, float]:
        """返回三类历史数据当前生效的保留天数。"""

        with self._lock:
            return {
                "usage_days": self._usage_days,
                "session_days": self._session_days,
                "alert_days": self._alert_days,
            }

    @property
    def last_cleanup(self) -> dict[str, object] | None:
        """最近一次(自动或手动)清理的结果摘要。"""

        with self._lock:
            return dict(self._last_cleanup) if self._last_cleanup else None

    def preview(self, now: float | None = None) -> dict[str, object]:
        """返回将删除的数据范围和预计释放空间(估算值,UI 标注「预计」)。"""

        timestamp = time.time() if now is None else float(now)
        with self._lock:
            usage_days = self._usage_days
            session_days = self._session_days
            alert_days = self._alert_days
        kinds: list[dict[str, object]] = []
        usage_cutoff = timestamp - usage_days * _SECONDS_PER_DAY
        usage_store = _UsageIndexStore(self._usage_store_path)
        try:
            usage_rows = usage_store.count_deltas_before(usage_cutoff)
            usage_total = usage_store.count_deltas()
        finally:
            usage_store.close()
        usage_bytes = db_file_bytes(self._usage_store_path)
        kinds.append(
            self._kind_payload(
                "usage", usage_cutoff, usage_rows, usage_total, usage_bytes
            )
        )

        session_cutoff = timestamp - session_days * _SECONDS_PER_DAY
        session_rows = 0
        session_total = 0
        session_bytes = 0
        for name, registry in self._registries().items():
            session_rows += registry.count_finished_sessions_before(session_cutoff)
            session_total += registry.count_sessions()
            session_bytes += db_file_bytes(registry.db_path)
            del name
        kinds.append(
            self._kind_payload(
                "sessions", session_cutoff, session_rows, session_total, session_bytes
            )
        )

        alert_cutoff = timestamp - alert_days * _SECONDS_PER_DAY
        if self._alert_store is not None:
            alert_rows = self._alert_store.count_before(alert_cutoff)
            alert_total = self._alert_store.count_all()
            alert_bytes = db_file_bytes(self._alert_store.db_path)
        else:
            alert_rows = alert_total = alert_bytes = 0
        kinds.append(
            self._kind_payload(
                "alerts", alert_cutoff, alert_rows, alert_total, alert_bytes
            )
        )

        return {
            "observed_at": timestamp,
            "kinds": kinds,
            "dbs": self.db_sizes(),
            "estimated_free_bytes": sum(
                int(item["estimated_free_bytes"]) for item in kinds
            ),
        }

    def db_sizes(self) -> list[dict[str, object]]:
        """返回状态目录里各索引/数据库文件的占用。"""

        entries: list[dict[str, object]] = [
            {
                "key": "usage-index",
                "label": "用量索引",
                "bytes": db_file_bytes(self._usage_store_path),
            }
        ]
        for name, registry in self._registries().items():
            entries.append(
                {
                    "key": f"registry:{name}",
                    "label": f"会话历史({name})",
                    "bytes": db_file_bytes(registry.db_path),
                }
            )
        if self._alert_store is not None:
            entries.append(
                {
                    "key": "traffic-alerts",
                    "label": "告警历史",
                    "bytes": db_file_bytes(self._alert_store.db_path),
                }
            )
        return entries

    def cleanup(self, now: float | None = None) -> dict[str, object]:
        """删除过期历史并压缩数据库;单个库失败不影响其他库。"""

        timestamp = time.time() if now is None else float(now)
        with self._lock:
            usage_days = self._usage_days
            session_days = self._session_days
        deleted: dict[str, int] = {}
        vacuumed: list[str] = []
        vacuum_skipped: list[str] = []
        errors: list[str] = []
        freed_bytes = 0

        def run_store(
            kind: str,
            label: str,
            path: Path,
            delete: Callable[[], int],
            vacuum: Callable[[], None],
        ) -> None:
            """清理单个库:先删行再压缩,失败只影响本库。"""

            nonlocal freed_bytes
            before = db_file_bytes(path)
            try:
                deleted[kind] = delete()
            except Exception as error:  # noqa: BLE001 - 单库失败不阻断其他库
                errors.append(f"{label}删除失败: {sanitize_error(error)}")
                return
            try:
                vacuum()
                vacuumed.append(kind)
            except Exception as error:  # noqa: BLE001 - 压缩失败不影响删除结果
                vacuum_skipped.append(kind)
                self.logger.info("%s压缩跳过: %s", label, sanitize_error(error))
            after = db_file_bytes(path)
            freed_bytes += max(0, before - after)

        usage_store = _UsageIndexStore(self._usage_store_path)
        usage_cutoff = timestamp - usage_days * _SECONDS_PER_DAY
        try:
            run_store(
                "usage",
                "用量索引",
                self._usage_store_path,
                lambda: usage_store.delete_deltas_before(usage_cutoff),
                usage_store.vacuum,
            )
        finally:
            usage_store.close()

        session_cutoff = timestamp - session_days * _SECONDS_PER_DAY
        session_deleted = 0
        for name, registry in self._registries().items():
            per_account = {"deleted": 0}
            run_store(
                f"sessions:{name}",
                f"会话历史({name})",
                registry.db_path,
                lambda registry=registry, per_account=per_account: per_account.update(
                    deleted=registry.delete_finished_sessions_before(session_cutoff)
                )
                or per_account["deleted"],
                registry.vacuum,
            )
            session_deleted += per_account["deleted"]
        deleted["sessions"] = session_deleted

        if self._alert_store is not None:
            run_store(
                "alerts",
                "告警历史",
                self._alert_store.db_path,
                lambda: self._alert_store.prune(timestamp, force=True),
                self._alert_store.vacuum,
            )

        result: dict[str, object] = {
            "observed_at": timestamp,
            "deleted": deleted,
            "freed_bytes": freed_bytes,
            "vacuumed": vacuumed,
            "vacuum_skipped": vacuum_skipped,
            "errors": errors,
        }
        with self._lock:
            self._last_cleanup = result
        if errors:
            raise RetentionError(";".join(errors))
        return result

    @staticmethod
    def _kind_payload(
        kind: str,
        cutoff: float,
        rows_to_delete: int,
        total_rows: int,
        db_bytes: int,
    ) -> dict[str, object]:
        estimated = (
            int(db_bytes * rows_to_delete / total_rows) if total_rows > 0 else 0
        )
        return {
            "kind": kind,
            "cutoff": cutoff,
            "rows_to_delete": rows_to_delete,
            "total_rows": total_rows,
            "db_bytes": db_bytes,
            "estimated_free_bytes": estimated,
        }
