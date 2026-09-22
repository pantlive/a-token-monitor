"""agent 数据目录的磁盘占用统计，以及会话文件的归档与清理。

只统计目录体积、文件数量和会话文件元数据，不读取会话内容。归档会把选中的
Codex session JSONL 打包成 tar.gz 并写入 manifest，校验成功后删除原文件，
可用 ``restore`` 还原；清理直接删除。两者都先给出预览，拒绝处理仍在运行的
活动会话和过新的文件。

v1 只对 Codex ``<CODEX_HOME>/sessions/**/rollout-*.jsonl`` 执行归档/清理；
Kimi、DeepSeek Harness、Grok 等目录只统计占用并提醒，不在这里删除。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tarfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .traffic import format_bytes
from .usage import SessionUsage, session_id_from_path

_GIB = 1024 * 1024 * 1024
DEFAULT_SINGLE_WARN_GIB = 5.0
DEFAULT_TOTAL_WARN_GIB = 10.0
DEFAULT_RETENTION_DAYS = 30
# 中文注释：太新的文件可能正在被写入，无论条件如何都不归档或删除。
_MIN_AGE_SECONDS = 600.0
_MAX_PREVIEW_ITEMS = 50
_TOP_CHILDREN = 6
_REFRESH_INTERVAL_SECONDS = 60.0
_SESSION_GLOB = "rollout-*.jsonl"
_ARCHIVE_PREFIX = "codex-sessions"
# 中文注释：后台任务只保留最近若干条，供网页轮询进度。
_MAX_TASKS = 10
# 中文注释：会话清单带 10 秒缓存，避免网页每 5 秒轮询都重新扫描目录。
_SESSIONS_CACHE_SECONDS = 10.0
_MANIFEST_SUFFIX = ".manifest.json"


class HousekeepingError(RuntimeError):
    """磁盘统计或会话归档/清理失败时抛出的异常。"""


@dataclass(frozen=True)
class DiskThresholds:
    """磁盘占用提醒阈值，单位为字节。"""

    single_warn_bytes: int = int(DEFAULT_SINGLE_WARN_GIB * _GIB)
    total_warn_bytes: int = int(DEFAULT_TOTAL_WARN_GIB * _GIB)

    def __post_init__(self) -> None:
        """拒绝无意义的阈值。"""

        if self.single_warn_bytes <= 0:
            raise ValueError("single_warn_bytes 必须大于 0")
        if self.total_warn_bytes <= 0:
            raise ValueError("total_warn_bytes 必须大于 0")

    @classmethod
    def from_gb(
        cls,
        single_warn_gb: float = DEFAULT_SINGLE_WARN_GIB,
        total_warn_gb: float = DEFAULT_TOTAL_WARN_GIB,
    ) -> "DiskThresholds":
        """从 GiB 配置构造阈值。"""

        return cls(
            single_warn_bytes=int(float(single_warn_gb) * _GIB),
            total_warn_bytes=int(float(total_warn_gb) * _GIB),
        )

    def to_dict(self) -> dict[str, int]:
        """返回 Dashboard / CLI 可展示的阈值。"""

        return {
            "single_warn_bytes": self.single_warn_bytes,
            "total_warn_bytes": self.total_warn_bytes,
        }


@dataclass(frozen=True)
class AuditTarget:
    """一个需要统计占用的 agent 数据目录。"""

    label: str
    product: str
    path: Path
    sessions_root: Path | None = None

    @property
    def cleanable(self) -> bool:
        """判断该目录下的会话文件是否支持归档和清理。"""

        return self.sessions_root is not None

    def to_dict(self) -> dict[str, Any]:
        """转换为 Dashboard / CLI 展示字段。"""

        return {
            "label": self.label,
            "product": self.product,
            "path": str(self.path),
            "sessions_root": (
                str(self.sessions_root) if self.sessions_root is not None else None
            ),
            "cleanable": self.cleanable,
        }


@dataclass(frozen=True)
class SessionFile:
    """一个可归档或清理的会话文件。"""

    path: Path
    product: str
    label: str
    size: int
    modified_at: float
    session_id: str

    def to_dict(self) -> dict[str, Any]:
        """转换为 Dashboard / CLI 展示字段。"""

        return {
            "path": str(self.path),
            "product": self.product,
            "label": self.label,
            "size": self.size,
            "modified_at": self.modified_at,
            "session_id": self.session_id,
        }


@dataclass(frozen=True)
class CleanupCriteria:
    """会话归档/清理的筛选条件。"""

    older_than_days: int = DEFAULT_RETENTION_DAYS
    min_bytes: int = 0
    # 中文注释：指定具体会话时忽略保留天数，只按路径精确匹配，
    # 但仍然跳过活动会话和刚写入过的文件。
    paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """拒绝无意义的时间范围、大小下限和路径。"""

        if self.older_than_days < 1 or self.older_than_days > 3650:
            raise ValueError("older_than_days 必须在 1 到 3650 之间")
        if self.min_bytes < 0:
            raise ValueError("min_bytes 不能小于 0")
        for item in self.paths:
            if not str(item).strip():
                raise ValueError("paths 不能包含空路径")

    def to_dict(self) -> dict[str, Any]:
        """返回 Dashboard / CLI 展示字段。"""

        return {
            "older_than_days": self.older_than_days,
            "min_bytes": self.min_bytes,
            "paths": list(self.paths),
        }


@dataclass(frozen=True)
class CleanupPlan:
    """一次归档/清理的预览结果。"""

    criteria: CleanupCriteria
    cutoff: float
    files: tuple[SessionFile, ...]
    skipped_active: int
    skipped_recent: int
    skipped_small: int

    @property
    def count(self) -> int:
        """返回将处理的文件数。"""

        return len(self.files)

    @property
    def total_bytes(self) -> int:
        """返回将释放的字节数。"""

        return sum(item.size for item in self.files)

    def to_dict(self, include_items: bool = True) -> dict[str, Any]:
        """转换为 Dashboard / CLI 展示字段。"""

        payload: dict[str, Any] = {
            "criteria": self.criteria.to_dict(),
            "cutoff": self.cutoff,
            "count": self.count,
            "bytes": self.total_bytes,
            "skipped_active": self.skipped_active,
            "skipped_recent": self.skipped_recent,
            "skipped_small": self.skipped_small,
            "oldest_at": min(
                (item.modified_at for item in self.files),
                default=None,
            ),
            "newest_at": max(
                (item.modified_at for item in self.files),
                default=None,
            ),
        }
        if include_items:
            payload["files"] = [
                item.to_dict() for item in self.files[:_MAX_PREVIEW_ITEMS]
            ]
            payload["truncated"] = self.count > _MAX_PREVIEW_ITEMS
        return payload


class HousekeepingMonitor:
    """统计 agent 目录占用、提醒磁盘压力，并归档或清理历史会话。"""

    def __init__(
        self,
        targets: Sequence[AuditTarget],
        thresholds: DiskThresholds | None = None,
        archive_dir: Path | None = None,
        active_paths: Callable[[], set[str]] | None = None,
        refresh_interval: float = _REFRESH_INTERVAL_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        """记录统计目标、阈值、归档目录和活动会话来源。"""

        if refresh_interval <= 0:
            raise ValueError("refresh_interval 必须大于 0")
        self.targets = tuple(target for target in targets if target.path.exists())
        self.thresholds = thresholds or DiskThresholds()
        self.archive_dir = (
            Path(archive_dir).expanduser() if archive_dir is not None else None
        )
        self.active_paths = active_paths or (lambda: set())
        self.refresh_interval = float(refresh_interval)
        self.logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._task_lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._report: dict[str, Any] | None = None
        self._reported_at = 0.0
        self._sessions_cache: (
            tuple[float, tuple[SessionFile, ...]] | None
        ) = None

    # ---------------------------------------------------------------- 统计

    def latest(self) -> dict[str, Any]:
        """返回最近一次统计结果；尚未统计时返回空报告。"""

        with self._lock:
            if self._report is not None:
                return self._report
        return empty_housekeeping_report(self.thresholds, self.archive_dir)

    def refresh(
        self,
        now: float | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """按刷新间隔重新统计目录占用和清理预览。"""

        observed_at = time.time() if now is None else float(now)
        with self._lock:
            if (
                not force
                and self._report is not None
                and observed_at - self._reported_at < self.refresh_interval
            ):
                return self._report
        report = self.scan(now=observed_at)
        with self._lock:
            self._report = report
            self._reported_at = observed_at
        return report

    def scan(self, now: float | None = None) -> dict[str, Any]:
        """统计每个目标目录的占用、会话文件分布和提醒。"""

        observed_at = time.time() if now is None else float(now)
        directories: list[dict[str, Any]] = []
        cleanable: list[SessionFile] = []
        for target in self.targets:
            size, files, top_children = _walk_usage(target.path)
            sessions = scan_session_files(target)
            cleanable.extend(sessions)
            directories.append(
                {
                    **target.to_dict(),
                    "bytes": size,
                    "files": files,
                    "session_bytes": sum(item.size for item in sessions),
                    "session_files": len(sessions),
                    "top_children": top_children,
                    "exists": True,
                }
            )
        total_bytes = sum(item["bytes"] for item in directories)
        report: dict[str, Any] = {
            "observed_at": observed_at,
            "thresholds": self.thresholds.to_dict(),
            "archive_dir": (
                str(self.archive_dir) if self.archive_dir is not None else None
            ),
            "directories": directories,
            "totals": {
                "bytes": total_bytes,
                "files": sum(item["files"] for item in directories),
                "session_bytes": sum(item["session_bytes"] for item in directories),
                "session_files": sum(
                    item["session_files"] for item in directories
                ),
                "directories": len(directories),
            },
            "reminders": self._disk_reminders(directories, total_bytes),
        }
        report["preview"] = self.preview(
            CleanupCriteria(),
            now=observed_at,
            sessions=tuple(cleanable),
        )
        return report

    def _disk_reminders(
        self,
        directories: Sequence[Mapping[str, Any]],
        total_bytes: int,
    ) -> list[dict[str, Any]]:
        """按阈值生成磁盘占用提醒。"""

        reminders: list[dict[str, Any]] = []
        single = self.thresholds.single_warn_bytes
        for item in sorted(
            directories,
            key=lambda entry: -int(entry["bytes"]),
        ):
            used = int(item["bytes"])
            if used < single:
                continue
            level = "danger" if used >= single * 2 else "warn"
            reminders.append(
                {
                    "level": level,
                    "kind": "disk",
                    "title": f"{item['label']} 占用 {format_bytes(used)}",
                    "detail": (
                        f"超过单目录 {format_bytes(single)} 提醒阈值。"
                        f"其中会话文件 {format_bytes(int(item['session_bytes']))}"
                        f"（{item['session_files']} 个）；可归档或清理旧会话，"
                        "或检查最大的子目录。"
                    ),
                    "message": (
                        f"{item['label']} 占用 {format_bytes(used)}，"
                        f"超过 {format_bytes(single)} 提醒阈值"
                    ),
                    "path": item["path"],
                    "bytes": used,
                }
            )
        total_warn = self.thresholds.total_warn_bytes
        if total_bytes >= total_warn:
            level = "danger" if total_bytes >= total_warn * 2 else "warn"
            reminders.append(
                {
                    "level": level,
                    "kind": "disk",
                    "title": f"agent 数据目录合计 {format_bytes(total_bytes)}",
                    "detail": (
                        f"超过合计 {format_bytes(total_warn)} 提醒阈值。"
                        "建议归档或清理不再需要的历史会话。"
                    ),
                    "message": (
                        f"agent 数据目录合计 {format_bytes(total_bytes)}，"
                        f"超过 {format_bytes(total_warn)} 提醒阈值"
                    ),
                    "path": None,
                    "bytes": total_bytes,
                }
            )
        return reminders

    # ------------------------------------------------------- 归档 / 清理

    def sessions(self, *, refresh: bool = False) -> tuple[SessionFile, ...]:
        """返回所有可归档或清理的会话文件（带短 TTL 缓存）。"""

        with self._lock:
            cached = self._sessions_cache
            if (
                not refresh
                and cached is not None
                and time.monotonic() - cached[0] < _SESSIONS_CACHE_SECONDS
            ):
                return cached[1]
        sessions: list[SessionFile] = []
        for target in self.targets:
            if target.cleanable:
                sessions.extend(scan_session_files(target))
        resolved = tuple(sessions)
        with self._lock:
            self._sessions_cache = (time.monotonic(), resolved)
        return resolved

    def preview(
        self,
        criteria: CleanupCriteria | None = None,
        now: float | None = None,
        sessions: Sequence[SessionFile] | None = None,
    ) -> dict[str, Any]:
        """预览符合条件的会话文件；不修改任何数据。"""

        selected = criteria or CleanupCriteria()
        plan = self.plan(selected, now=now, sessions=sessions)
        payload = plan.to_dict()
        payload["unmatched"] = self.unmatched_paths(selected)
        return payload

    def unmatched_paths(self, criteria: CleanupCriteria) -> list[str]:
        """返回指定路径里不存在或不在可归档目录内的项。"""

        if not criteria.paths:
            return []
        available = {str(item.path) for item in self.sessions()}
        return [
            candidate
            for candidate in (
                str(Path(str(item)).expanduser()) for item in criteria.paths
            )
            if candidate not in available
        ]

    def _invalidate_sessions(self) -> None:
        """归档或清理后丢弃会话清单缓存，下一页立刻看到最新占用。"""

        with self._lock:
            self._sessions_cache = None
            self._report = None

    def session_archive_state(
        self,
        paths: Sequence[str],
        now: float | None = None,
    ) -> dict[str, dict[str, Any]]:
        """返回每个会话文件当前能否单独归档，以及不能的原因。"""

        observed_at = time.time() if now is None else float(now)
        active = self.active_paths()
        available = {str(item.path): item for item in self.sessions()}
        states: dict[str, dict[str, Any]] = {}
        for raw in paths:
            if not raw:
                continue
            key = str(Path(str(raw)).expanduser())
            item = available.get(key)
            if item is None:
                states[key] = {
                    "eligible": False,
                    "reason": "不在可归档的 Codex 会话目录内",
                }
                continue
            if key in active:
                states[key] = {"eligible": False, "reason": "会话仍在运行"}
                continue
            if observed_at - item.modified_at < _MIN_AGE_SECONDS:
                states[key] = {
                    "eligible": False,
                    "reason": "最近 10 分钟内仍在写入",
                }
                continue
            states[key] = {
                "eligible": True,
                "reason": None,
                "size": item.size,
                "session_id": item.session_id,
            }
        return states

    def plan(
        self,
        criteria: CleanupCriteria | None = None,
        now: float | None = None,
        sessions: Sequence[SessionFile] | None = None,
    ) -> CleanupPlan:
        """计算一次归档/清理将涉及的会话文件。"""

        selected_criteria = criteria or CleanupCriteria()
        observed_at = time.time() if now is None else float(now)
        cutoff = observed_at - selected_criteria.older_than_days * 86_400
        active = self.active_paths()
        candidates = self.sessions() if sessions is None else tuple(sessions)
        wanted = (
            {str(Path(item).expanduser()) for item in selected_criteria.paths}
            if selected_criteria.paths
            else None
        )
        chosen: list[SessionFile] = []
        skipped_active = 0
        skipped_recent = 0
        skipped_small = 0
        for item in candidates:
            if wanted is not None and str(item.path) not in wanted:
                continue
            if str(item.path) in active:
                skipped_active += 1
                continue
            too_recent = observed_at - item.modified_at < _MIN_AGE_SECONDS
            # 中文注释：指定具体会话时不再看保留天数，但过新的文件仍然跳过。
            if too_recent or (wanted is None and item.modified_at > cutoff):
                skipped_recent += 1
                continue
            if item.size < selected_criteria.min_bytes:
                skipped_small += 1
                continue
            chosen.append(item)
        chosen.sort(key=lambda item: item.modified_at)
        return CleanupPlan(
            criteria=selected_criteria,
            cutoff=cutoff,
            files=tuple(chosen),
            skipped_active=skipped_active,
            skipped_recent=skipped_recent,
            skipped_small=skipped_small,
        )

    def archive(
        self,
        criteria: CleanupCriteria | None = None,
        *,
        now: float | None = None,
        confirm: bool = False,
        usage: Mapping[str, SessionUsage] | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """把符合条件的会话打包成 tar.gz 后删除原文件。"""

        if not confirm:
            raise HousekeepingError("归档会删除原文件，需要显式确认")
        if self.archive_dir is None:
            raise HousekeepingError("没有配置归档目录")
        observed_at = time.time() if now is None else float(now)
        plan = self.plan(criteria, now=observed_at)
        if plan.count == 0:
            return {
                "action": "archive",
                "count": 0,
                "bytes": 0,
                "archive": None,
                "manifest": None,
                "deleted": 0,
                "failed": [],
            }
        archive_path, manifest_path = self._archive_paths(observed_at)
        self.archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = archive_path.with_name(archive_path.name + ".tmp")
        try:
            with tarfile.open(temporary, "w:gz") as handle:
                for index, item in enumerate(plan.files, start=1):
                    handle.add(str(item.path), arcname=str(item.path).lstrip("/"))
                    _report_progress(
                        progress,
                        phase="compress",
                        done=index,
                        total=plan.count,
                        bytes_done=sum(
                            entry.size for entry in plan.files[:index]
                        ),
                        total_bytes=plan.total_bytes,
                    )
            _report_progress(
                progress,
                phase="verify",
                done=plan.count,
                total=plan.count,
                bytes_done=plan.total_bytes,
                total_bytes=plan.total_bytes,
            )
            missing = _missing_members(temporary, plan.files)
            if missing:
                raise HousekeepingError(
                    f"归档校验失败，缺少 {len(missing)} 个文件；未删除任何原文件"
                )
            temporary.replace(archive_path)
            archive_path.chmod(0o600)
        except (OSError, tarfile.TarError) as error:
            temporary.unlink(missing_ok=True)
            raise HousekeepingError(f"归档失败: {error}") from error
        deleted, failed = _delete_files(plan.files, progress=progress, total=plan.count)
        self._invalidate_sessions()
        manifest = {
            "created_at": observed_at,
            "action": "archive",
            "criteria": plan.criteria.to_dict(),
            "cutoff": plan.cutoff,
            "archive": str(archive_path),
            "archive_sha256": _sha256(archive_path),
            "count": plan.count,
            "bytes": plan.total_bytes,
            "deleted": len(deleted),
            "failed": failed,
            "files": [
                {
                    **item.to_dict(),
                    "usage": _usage_payload(usage, item.path),
                }
                for item in plan.files
            ],
        }
        _write_manifest(manifest_path, manifest)
        self.logger.info(
            "已归档 %d 个会话文件（%s）到 %s",
            plan.count,
            format_bytes(plan.total_bytes),
            archive_path,
        )
        return {
            "action": "archive",
            "count": plan.count,
            "bytes": plan.total_bytes,
            "archive": str(archive_path),
            "manifest": str(manifest_path),
            "deleted": len(deleted),
            "failed": failed,
        }

    def clean(
        self,
        criteria: CleanupCriteria | None = None,
        *,
        now: float | None = None,
        confirm: bool = False,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """直接删除符合条件的会话文件；操作前必须显式确认。"""

        if not confirm:
            raise HousekeepingError("清理会删除会话文件，需要显式确认")
        observed_at = time.time() if now is None else float(now)
        plan = self.plan(criteria, now=observed_at)
        deleted, failed = _delete_files(
            plan.files,
            progress=progress,
            total=plan.count,
        )
        self._invalidate_sessions()
        if plan.count:
            self.logger.info(
                "已清理 %d 个会话文件（%s）",
                len(deleted),
                format_bytes(plan.total_bytes),
            )
        return {
            "action": "clean",
            "count": plan.count,
            "bytes": plan.total_bytes,
            "archive": None,
            "manifest": None,
            "deleted": len(deleted),
            "failed": failed,
        }

    def restores(self) -> tuple[dict[str, Any], ...]:
        """列出归档目录里已有的归档及其 manifest 摘要。"""

        if self.archive_dir is None or not self.archive_dir.is_dir():
            return ()
        items: list[dict[str, Any]] = []
        for archive in sorted(
            self.archive_dir.glob(f"{_ARCHIVE_PREFIX}-*.tar.gz"),
            reverse=True,
        ):
            manifest_path = archive.with_name(archive.name + _MANIFEST_SUFFIX)
            entry: dict[str, Any] = {
                "archive": str(archive),
                "manifest": (
                    str(manifest_path) if manifest_path.exists() else None
                ),
                "bytes": _safe_size(archive),
                "created_at": None,
                "count": None,
            }
            manifest = _read_manifest(manifest_path)
            if manifest is not None:
                entry["created_at"] = manifest.get("created_at")
                entry["count"] = manifest.get("count")
            items.append(entry)
        return tuple(items)

    def restore(
        self,
        archive_path: Path,
        destination: Path | None = None,
    ) -> dict[str, Any]:
        """把归档解包回原路径（或指定根目录）。"""

        source = Path(archive_path).expanduser()
        if not source.is_file():
            raise HousekeepingError(f"归档不存在: {source}")
        root = Path(destination).expanduser() if destination is not None else Path("/")
        manifest = _read_manifest(
            source.with_name(source.name + _MANIFEST_SUFFIX)
        )
        restored = 0
        try:
            with tarfile.open(source, "r:gz") as handle:
                members = [item for item in handle.getmembers() if item.isfile()]
                for item in members:
                    _guard_member(item.name)
                _extract_all(handle, root, members)
                restored = len(members)
        except (OSError, tarfile.TarError) as error:
            raise HousekeepingError(f"恢复归档失败: {error}") from error
        self.logger.info("已从 %s 恢复 %d 个会话文件", source, restored)
        return {
            "action": "restore",
            "archive": str(source),
            "destination": str(root),
            "restored": restored,
            "manifest": manifest if manifest is not None else None,
        }

    # ------------------------------------------------------- 后台任务

    def start_task(
        self,
        action: str,
        criteria: CleanupCriteria | None = None,
        *,
        now: float | None = None,
        usage: Mapping[str, SessionUsage] | None = None,
    ) -> dict[str, Any]:
        """在后台线程执行归档或清理，立即返回可轮询的任务信息。"""

        if action not in {"archive", "clean"}:
            raise HousekeepingError(f"未知操作: {action}")
        selected = criteria or CleanupCriteria()
        task_id = uuid.uuid4().hex[:12]
        task: dict[str, Any] = {
            "id": task_id,
            "action": action,
            "state": "running",
            "criteria": selected.to_dict(),
            "started_at": time.time() if now is None else float(now),
            "finished_at": None,
            "progress": {
                "phase": "plan",
                "done": 0,
                "total": 0,
                "bytes_done": 0,
                "total_bytes": 0,
            },
            "result": None,
            "error": None,
        }
        with self._task_lock:
            self._tasks[task_id] = task
            self._trim_tasks_locked()

        def update(progress: dict[str, Any]) -> None:
            with self._task_lock:
                entry = self._tasks.get(task_id)
                if entry is not None:
                    entry["progress"] = progress

        def run() -> None:
            try:
                if action == "archive":
                    result = self.archive(
                        selected,
                        now=task["started_at"],
                        confirm=True,
                        usage=usage,
                        progress=update,
                    )
                else:
                    result = self.clean(
                        selected,
                        now=task["started_at"],
                        confirm=True,
                        progress=update,
                    )
            except (HousekeepingError, OSError, ValueError) as error:
                with self._task_lock:
                    entry = self._tasks.get(task_id)
                    if entry is not None:
                        entry["state"] = "failed"
                        entry["error"] = str(error)
                        entry["finished_at"] = time.time()
                return
            with self._task_lock:
                entry = self._tasks.get(task_id)
                if entry is not None:
                    entry["state"] = "done"
                    entry["result"] = result
                    entry["finished_at"] = time.time()
                    entry["progress"] = {
                        "phase": "done",
                        "done": result.get("deleted", result.get("count", 0)),
                        "total": result.get("count", 0),
                        "bytes_done": result.get("bytes", 0),
                        "total_bytes": result.get("bytes", 0),
                    }

        threading.Thread(
            target=run,
            name=f"token-monitor-{action}",
            daemon=True,
        ).start()
        return dict(task)

    def task(self, task_id: str) -> dict[str, Any] | None:
        """返回后台任务的最新状态。"""

        with self._task_lock:
            entry = self._tasks.get(task_id)
            return dict(entry) if entry is not None else None

    def tasks(self) -> tuple[dict[str, Any], ...]:
        """返回最近的后台任务（新到旧）。"""

        with self._task_lock:
            entries = sorted(
                self._tasks.values(),
                key=lambda item: float(item["started_at"]),
                reverse=True,
            )
            return tuple(dict(entry) for entry in entries)

    def _trim_tasks_locked(self) -> None:
        """只保留最近的任务，避免长时间运行后无限增长。"""

        if len(self._tasks) <= _MAX_TASKS:
            return
        ordered = sorted(
            self._tasks,
            key=lambda item: float(self._tasks[item]["started_at"]),
        )
        for stale in ordered[: len(self._tasks) - _MAX_TASKS]:
            self._tasks.pop(stale, None)

    def _archive_paths(self, now: float) -> tuple[Path, Path]:
        """返回本次归档的文件名，避免同一秒内互相覆盖。"""

        assert self.archive_dir is not None
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        archive = self.archive_dir / f"{_ARCHIVE_PREFIX}-{stamp}.tar.gz"
        counter = 1
        while archive.exists():
            counter += 1
            archive = self.archive_dir / f"{_ARCHIVE_PREFIX}-{stamp}-{counter}.tar.gz"
        return archive, archive.with_name(archive.name + _MANIFEST_SUFFIX)


def empty_housekeeping_report(
    thresholds: DiskThresholds | None = None,
    archive_dir: Path | None = None,
) -> dict[str, Any]:
    """返回尚未统计时仍可给 Dashboard 使用的空报告。"""

    selected = thresholds or DiskThresholds()
    return {
        "observed_at": None,
        "thresholds": selected.to_dict(),
        "archive_dir": str(archive_dir) if archive_dir is not None else None,
        "directories": [],
        "totals": {
            "bytes": 0,
            "files": 0,
            "session_bytes": 0,
            "session_files": 0,
            "directories": 0,
        },
        "reminders": [],
        "preview": CleanupPlan(
            criteria=CleanupCriteria(),
            cutoff=0.0,
            files=(),
            skipped_active=0,
            skipped_recent=0,
            skipped_small=0,
        ).to_dict(include_items=False),
    }


def scan_session_files(target: AuditTarget) -> tuple[SessionFile, ...]:
    """扫描一个目标目录下可归档/清理的会话文件。"""

    if target.sessions_root is None or not target.sessions_root.is_dir():
        return ()
    files: list[SessionFile] = []
    for path in sorted(target.sessions_root.glob(f"**/{_SESSION_GLOB}")):
        try:
            stat_result = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        files.append(
            SessionFile(
                path=path,
                product=target.product,
                label=target.label,
                size=stat_result.st_size,
                modified_at=stat_result.st_mtime,
                session_id=session_id_from_path(path),
            )
        )
    return tuple(files)


def _walk_usage(
    root: Path,
    max_entries: int = 500_000,
) -> tuple[int, int, list[dict[str, Any]]]:
    """一次遍历同时得到总占用、文件数和占用最大的若干一级子目录/文件。

    旧实现先整树遍历一次、再对每个一级子目录各遍历一次，等于走两遍；
    这里按「一级子目录」归账，一次遍历就能给出排行榜。
    """

    total = 0
    files = 0
    seen = 0
    children: dict[str, int] = {}
    stack: list[tuple[Path, str | None]] = [(root, None)]
    while stack:
        current, top_name = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen > max_entries:
                return total, files, _rank_children(children)
            name = top_name if top_name is not None else entry.name
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), name))
                    continue
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            total += size
            files += 1
            children[name] = children.get(name, 0) + size
    return total, files, _rank_children(children)


def _rank_children(
    children: Mapping[str, int],
    limit: int = _TOP_CHILDREN,
) -> list[dict[str, Any]]:
    """按占用排序一级子目录，返回展示用列表。"""

    items = [
        {"name": name, "bytes": size}
        for name, size in children.items()
        if size > 0
    ]
    items.sort(key=lambda item: (-int(item["bytes"]), str(item["name"])))
    return items[:limit]


def _delete_files(
    files: Sequence[SessionFile],
    progress: Callable[[dict[str, Any]], None] | None = None,
    total: int | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """删除文件，返回成功路径和失败原因。"""

    deleted: list[str] = []
    failed: list[dict[str, str]] = []
    total_count = total if total is not None else len(files)
    total_bytes = sum(item.size for item in files)
    bytes_done = 0
    for index, item in enumerate(files, start=1):
        try:
            item.path.unlink()
            deleted.append(str(item.path))
        except OSError as error:
            failed.append({"path": str(item.path), "error": str(error)})
        bytes_done += item.size
        _report_progress(
            progress,
            phase="delete",
            done=index,
            total=total_count,
            bytes_done=bytes_done,
            total_bytes=total_bytes,
        )
    return deleted, failed


def _report_progress(
    progress: Callable[[dict[str, Any]], None] | None,
    *,
    phase: str,
    done: int,
    total: int,
    bytes_done: int,
    total_bytes: int,
) -> None:
    """把进度回调包在保护里：回调失败不能影响归档本身。"""

    if progress is None:
        return
    try:
        progress(
            {
                "phase": phase,
                "done": done,
                "total": total,
                "bytes_done": bytes_done,
                "total_bytes": total_bytes,
            }
        )
    except Exception:  # noqa: BLE001 - 进度回调只是展示用途
        return


def _missing_members(archive: Path, files: Sequence[SessionFile]) -> set[str]:
    """校验归档里是否包含全部待删除文件。"""

    expected = {str(item.path).lstrip("/") for item in files}
    try:
        with tarfile.open(archive, "r:gz") as handle:
            members = {item.name for item in handle.getmembers() if item.isfile()}
    except (OSError, tarfile.TarError) as error:
        raise HousekeepingError(f"无法校验归档: {error}") from error
    return expected - members


def _guard_member(name: str) -> None:
    """拒绝绝对路径和向上穿越的归档成员。"""

    candidate = Path(name)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise HousekeepingError(f"归档包含不安全的路径: {name}")


def _extract_all(
    handle: tarfile.TarFile,
    root: Path,
    members: Sequence[tarfile.TarInfo],
) -> None:
    """把归档成员解包到指定根目录，兼容不同 Python 版本的 filter 参数。"""

    try:
        handle.extractall(path=root, members=list(members), filter="data")
    except TypeError:  # pragma: no cover - Python 3.11 及更早版本
        handle.extractall(path=root, members=list(members))


def _sha256(path: Path) -> str:
    """计算文件摘要，用于 manifest 校验。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_size(path: Path) -> int:
    """安全读取文件大小。"""

    try:
        return path.stat().st_size
    except OSError:
        return 0


def _write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    """写入归档 manifest，权限限制为当前用户。"""

    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - 权限受限时保持原状
        pass


def _read_manifest(path: Path) -> dict[str, Any] | None:
    """读取归档 manifest；缺失或损坏时返回 None。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _usage_payload(
    usage: Mapping[str, SessionUsage] | None,
    path: Path,
) -> dict[str, Any] | None:
    """把会话用量摘要写进 manifest，便于归档后核对历史规模。"""

    if not usage:
        return None
    summary = usage.get(str(path))
    return summary.to_dict() if summary is not None else None
