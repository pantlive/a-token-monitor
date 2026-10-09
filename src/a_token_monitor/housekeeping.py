"""agent 数据目录的磁盘占用统计，以及会话文件的归档与清理。

只统计目录体积、文件数量和会话文件元数据；项目归属只读会话头部或目录名，
不读取对话内容。归档会把选中的会话文件打包成 tar.gz 并写入 manifest，
校验成功后删除原文件，可用 ``restore`` 还原；清理直接删除。两者都先给出
预览，拒绝处理仍在运行的活动会话和过新的文件，并支持按项目（工作目录）
筛选后压缩归档。

Codex、Claude Code、Kimi Code、Grok、DeepSeek Harness、Command Code、
Cursor、Gemini CLI、Qwen Code、Aider 的会话文件都可以归档/清理。OpenCode 的
数据库包含全部会话，只统计目录占用，不作为可删除的会话文件。
监控状态目录同样只统计占用并提醒。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tarfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Callable, Mapping, Sequence

from .local_time import to_local
from .claude import claude_session_id
from .commandcode import read_commandcode_session_info
from .dsh import read_dsh_projcache
from .grok import decode_grok_project
from .local_agents import chat_project, cursor_session_id
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
_ARCHIVE_PREFIX = "sessions"
# 中文注释：旧版归档使用 codex-sessions 前缀，列出已有归档时保持兼容。
_LEGACY_ARCHIVE_PREFIX = "codex-sessions"
# 中文注释：后台任务只保留最近若干条，供网页轮询进度。
_MAX_TASKS = 10
# 中文注释：会话清单带 10 秒缓存，避免网页每 5 秒轮询都重新扫描目录。
_SESSIONS_CACHE_SECONDS = 10.0
_MANIFEST_SUFFIX = ".manifest.json"
_UNKNOWN_PROJECT = "未知项目"
# 中文注释：项目归属只读文件头部一小段，避免为大会话文件付出整文件 IO。
_HEAD_READ_BYTES = 64 * 1024
_HEAD_READ_LINES = 25
_PROJECT_SLUG_MAX = 40
# 中文注释：项目缓存只按路径保留，体积或修改时间变化即失效。
_PROJECT_CACHE_MAX = 50_000
_CWD_HEAD_PATTERN = re.compile(r'"cwd"\s*:\s*"([^"]+)"')


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
    project: str = _UNKNOWN_PROJECT

    def to_dict(self) -> dict[str, Any]:
        """转换为 Dashboard / CLI 展示字段。"""

        return {
            "path": str(self.path),
            "product": self.product,
            "label": self.label,
            "size": self.size,
            "modified_at": self.modified_at,
            "session_id": self.session_id,
            "project": self.project,
        }


@dataclass(frozen=True)
class CleanupCriteria:
    """会话归档/清理的筛选条件。"""

    older_than_days: int = DEFAULT_RETENTION_DAYS
    min_bytes: int = 0
    # 中文注释：指定具体会话时忽略保留天数，只按路径精确匹配，
    # 但仍然跳过活动会话和刚写入过的文件。
    paths: tuple[str, ...] = ()
    # 中文注释：按项目（工作目录）筛选；空元组表示全部项目。
    # 与 paths 同时指定时以 paths 为准。
    projects: tuple[str, ...] = ()
    # 中文注释：为 True 时忽略保留天数（整项目压缩用），
    # 活动会话和 10 分钟内写入的文件仍然跳过。
    any_age: bool = False

    def __post_init__(self) -> None:
        """拒绝无意义的时间范围、大小下限、路径和项目。"""

        if self.older_than_days < 1 or self.older_than_days > 3650:
            raise ValueError("older_than_days 必须在 1 到 3650 之间")
        if self.min_bytes < 0:
            raise ValueError("min_bytes 不能小于 0")
        for item in self.paths:
            if not str(item).strip():
                raise ValueError("paths 不能包含空路径")
        for project in self.projects:
            if not str(project).strip():
                raise ValueError("projects 不能包含空项目")
            if len(str(project)) > 1024:
                raise ValueError("projects 单条不能超过 1024 字符")

    def to_dict(self) -> dict[str, Any]:
        """返回 Dashboard / CLI 展示字段。"""

        return {
            "older_than_days": self.older_than_days,
            "min_bytes": self.min_bytes,
            "paths": list(self.paths),
            "projects": list(self.projects),
            "any_age": self.any_age,
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
    skipped_project: int = 0

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
            "skipped_project": self.skipped_project,
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
            # 中文注释：plan() 已按「体积 × 闲置时长」降序排好，截断后留下的都是
            # 很久没用且占用大的会话。
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
        # 中文注释：不在构造时过滤——状态目录可能在 daemon 启动后才创建，
        # 读取 targets 时按当前文件系统状态过滤。
        self._targets = tuple(targets)
        self.thresholds = thresholds or DiskThresholds()
        self.archive_dir = (
            Path(archive_dir).expanduser() if archive_dir is not None else None
        )
        self.active_paths = active_paths or (lambda: set())
        self.refresh_interval = float(refresh_interval)
        self.logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._task_lock = threading.Lock()
        # 中文注释：归档、清理和恢复串行执行，避免处理相同文件或争用归档名。
        self._operation_lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._report: dict[str, Any] | None = None
        self._reported_at = 0.0
        self._sessions_cache: (
            tuple[float, tuple[SessionFile, ...]] | None
        ) = None
        # 中文注释：项目归属解析按 (path, size, mtime) 缓存，避免每轮扫描都
        # 重读所有会话文件头部。
        self._project_cache: dict[str, tuple[int, float, str]] = {}

    # ---------------------------------------------------------------- 统计

    @property
    def targets(self) -> tuple[AuditTarget, ...]:
        """返回当前真实存在的审计目标。"""

        return tuple(target for target in self._targets if target.path.exists())

    def update_targets(self, targets: Sequence[AuditTarget]) -> None:
        """原子替换审计目标，并丢弃按旧目标统计出的报告和会话清单缓存。"""

        with self._lock:
            self._targets = tuple(targets)
            self._sessions_cache = None
            self._report = None

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
            sessions = scan_session_files(target, project_cache=self._project_cache)
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
                sessions.extend(
                    scan_session_files(target, project_cache=self._project_cache)
                )
        resolved = tuple(sessions)
        with self._lock:
            self._sessions_cache = (time.monotonic(), resolved)
        return resolved

    def projects(
        self,
        criteria: CleanupCriteria | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """按项目聚合当前可归档会话，供 Dashboard 筛选下拉展示。

        给定筛选条件时，每个项目额外带 ``selected_files``/``selected_bytes``
        （该条件下实际会被处理的部分），让卡片总数和预览数量能对上。
        """

        grouped: dict[str, dict[str, Any]] = {}
        for item in self.sessions():
            entry = grouped.get(item.project)
            if entry is None:
                entry = {
                    "project": item.project,
                    "files": 0,
                    "bytes": 0,
                    "oldest_at": None,
                    "newest_at": None,
                }
                grouped[item.project] = entry
            entry["files"] += 1
            entry["bytes"] += item.size
            entry["oldest_at"] = (
                item.modified_at
                if entry["oldest_at"] is None
                else min(entry["oldest_at"], item.modified_at)
            )
            entry["newest_at"] = (
                item.modified_at
                if entry["newest_at"] is None
                else max(entry["newest_at"], item.modified_at)
            )
        if criteria is not None:
            selected: dict[str, dict[str, int]] = {}
            for item in self.plan(criteria).files:
                bucket = selected.get(item.project)
                if bucket is None:
                    bucket = {"files": 0, "bytes": 0}
                    selected[item.project] = bucket
                bucket["files"] += 1
                bucket["bytes"] += item.size
            for project, entry in grouped.items():
                bucket = selected.get(project)
                entry["selected_files"] = bucket["files"] if bucket else 0
                entry["selected_bytes"] = bucket["bytes"] if bucket else 0
        return tuple(
            sorted(
                grouped.values(),
                key=lambda entry: (-int(entry["bytes"]), str(entry["project"])),
            )
        )

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
                    "reason": "不在可归档的会话目录内",
                }
                continue
            if _path_is_protected(item.path, active):
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
        wanted_projects = (
            {str(item).strip() for item in selected_criteria.projects}
            if selected_criteria.projects
            else None
        )
        chosen: list[SessionFile] = []
        skipped_active = 0
        skipped_recent = 0
        skipped_small = 0
        skipped_project = 0
        for item in candidates:
            if wanted is not None and str(item.path) not in wanted:
                continue
            if _path_is_protected(item.path, active):
                skipped_active += 1
                continue
            # 中文注释：指定具体会话时不再看项目筛选，只按路径精确匹配。
            if (
                wanted is None
                and wanted_projects is not None
                and item.project not in wanted_projects
            ):
                skipped_project += 1
                continue
            too_recent = observed_at - item.modified_at < _MIN_AGE_SECONDS
            # 中文注释：指定具体会话或整项目压缩时不再看保留天数，
            # 但过新的文件仍然跳过。
            bypass_age = wanted is not None or selected_criteria.any_age
            if too_recent or (not bypass_age and item.modified_at > cutoff):
                skipped_recent += 1
                continue
            if item.size < selected_criteria.min_bytes:
                skipped_small += 1
                continue
            chosen.append(item)
        # 中文注释：按「体积 × 闲置时长」降序——很久没用且占用大的会话排在最前，
        # 展示与归档执行都用这个顺序。
        chosen.sort(
            key=lambda item: item.size * max(0.0, observed_at - item.modified_at),
            reverse=True,
        )
        return CleanupPlan(
            criteria=selected_criteria,
            cutoff=cutoff,
            files=tuple(chosen),
            skipped_active=skipped_active,
            skipped_recent=skipped_recent,
            skipped_small=skipped_small,
            skipped_project=skipped_project,
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
        """串行执行归档，防止多个任务同时压缩、删除同一会话。"""

        with self._operation_lock:
            return self._archive(
                criteria, now=now, confirm=confirm, usage=usage, progress=progress
            )

    def _archive(
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
        archive_path, manifest_path = self._archive_paths(
            observed_at,
            name_hint=_archive_name_hint(plan.criteria),
        )
        self.archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = archive_path.with_name(archive_path.name + ".tmp")
        signatures: dict[Path, tuple[int, ...]] = {}
        try:
            with tarfile.open(temporary, "w:gz") as handle:
                bytes_done = 0
                for index, item in enumerate(plan.files, start=1):
                    signature = _session_file_signature(item.path)
                    if (
                        signature[2] != item.size
                        or item.path.stat().st_mtime != item.modified_at
                    ):
                        raise HousekeepingError(
                            "会话在归档前发生变化；未删除任何原文件"
                        )
                    signatures[item.path] = signature
                    handle.add(str(item.path), arcname=_archive_member_name(item.path))
                    bytes_done += item.size
                    _report_progress(
                        progress,
                        phase="compress",
                        done=index,
                        total=plan.count,
                        bytes_done=bytes_done,
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
        except (OSError, tarfile.TarError, HousekeepingError) as error:
            temporary.unlink(missing_ok=True)
            raise HousekeepingError(f"归档失败: {error}") from error
        # 中文注释：校验结果与 manifest 先持久化，再允许删除原会话。
        archive_digest = _sha256(archive_path)
        _write_manifest(
            manifest_path,
            {
                "created_at": observed_at,
                "archive": str(archive_path),
                "archive_sha256": archive_digest,
                "count": plan.count,
                "deleted": 0,
                "files": [item.to_dict() for item in plan.files],
            },
        )
        deleted, failed = _delete_files(
            plan.files,
            progress=progress,
            total=plan.count,
            active_paths=self.active_paths,
            signatures=signatures,
        )
        self._invalidate_sessions()
        manifest = {
            "created_at": observed_at,
            "action": "archive",
            "criteria": plan.criteria.to_dict(),
            "cutoff": plan.cutoff,
            "archive": str(archive_path),
            "archive_sha256": archive_digest,
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
        """串行执行清理；每个文件删除前重新检查保护名单和文件签名。"""

        with self._operation_lock:
            return self._clean(criteria, now=now, confirm=confirm, progress=progress)

    def _clean(
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
            active_paths=self.active_paths,
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
        archives = {
            path
            for prefix in (_ARCHIVE_PREFIX, _LEGACY_ARCHIVE_PREFIX)
            for path in self.archive_dir.glob(f"{prefix}-*.tar.gz")
        }
        for archive in sorted(archives, reverse=True):
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
        """串行恢复归档，避免与归档或清理交叉执行。"""

        with self._operation_lock:
            return self._restore(archive_path, destination)

    def _restore(
        self,
        archive_path: Path,
        destination: Path | None = None,
    ) -> dict[str, Any]:
        """把归档解包回原路径（或指定根目录）。"""

        source = Path(archive_path).expanduser()
        if not source.is_file():
            raise HousekeepingError(f"归档不存在: {source}")
        root = Path(destination).expanduser() if destination is not None else None
        manifest = _read_manifest(
            source.with_name(source.name + _MANIFEST_SUFFIX)
        )
        if manifest is not None and manifest.get("archive_sha256"):
            if _sha256(source) != manifest["archive_sha256"]:
                raise HousekeepingError("归档摘要校验失败；未恢复任何文件")
        restored = 0
        try:
            with tarfile.open(source, "r:gz") as handle:
                members = [item for item in handle.getmembers() if item.isfile()]
                for item in members:
                    _guard_member(item.name)
                if root is not None:
                    _extract_all(handle, root, members)
                    roots = [root]
                else:
                    # 中文注释：成员名不带盘符；不指定目标时按 manifest 里的原路径
                    # 把每个文件写回它原来的盘，而不是当前工作目录所在的盘。
                    anchors = _member_anchors(manifest)
                    groups: dict[Path, list[tarfile.TarInfo]] = {}
                    for item in members:
                        anchor = Path(anchors.get(item.name, os.sep))
                        groups.setdefault(anchor, []).append(item)
                    for anchor, group in groups.items():
                        _extract_all(handle, anchor, group)
                    roots = list(groups) or [Path(os.sep)]
                restored = len(members)
        except (OSError, tarfile.TarError) as error:
            raise HousekeepingError(f"恢复归档失败: {error}") from error
        # 中文注释：恢复改变了会话目录内容，缓存的会话清单必须作废，
        # 否则紧接着的单会话归档/清理会误判「不在可归档目录内」。
        self._invalidate_sessions()
        self.logger.info("已从 %s 恢复 %d 个会话文件", source, restored)
        return {
            "action": "restore",
            "archive": str(source),
            "destination": ", ".join(str(item) for item in roots),
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
            name=f"a-token-monitor-{action}",
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

    def _archive_paths(
        self,
        now: float,
        name_hint: str | None = None,
    ) -> tuple[Path, Path]:
        """返回本次归档的文件名，避免同一秒内互相覆盖。"""

        assert self.archive_dir is not None
        stamp = to_local(now).strftime("%Y%m%d-%H%M%S")
        prefix = (
            f"{_ARCHIVE_PREFIX}-{name_hint}" if name_hint else _ARCHIVE_PREFIX
        )
        archive = self.archive_dir / f"{prefix}-{stamp}.tar.gz"
        counter = 1
        while archive.exists():
            counter += 1
            archive = self.archive_dir / f"{prefix}-{stamp}-{counter}.tar.gz"
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


@dataclass(frozen=True)
class _SessionSpec:
    """一种产品的会话文件布局：相对 home 的会话根目录和文件匹配模式。"""

    sessions_dir: str
    pattern: str


_SESSION_SPECS = {
    "codex": _SessionSpec("sessions", "**/rollout-*.jsonl"),
    "claude": _SessionSpec("projects", "**/*.jsonl"),
    "kimi": _SessionSpec("sessions", "**/*"),
    "grok": _SessionSpec("sessions", "**/*"),
    "dsh": _SessionSpec("sessions", "**/*"),
    "command-code": _SessionSpec("projects", "**/*"),
    "cursor": _SessionSpec("projects", "**/agent-transcripts/**/*.jsonl"),
    "gemini": _SessionSpec("", "tmp/*/chats/*"),
    "qwen": _SessionSpec("", "**/chats/*"),
    "aider": _SessionSpec("", ".aider.chat.history.md"),
}
_CHAT_SUFFIXES = frozenset({".json", ".jsonl"})


def default_sessions_root(product: str, home: Path) -> Path | None:
    """返回某产品的会话根目录；没有可归档会话布局的产品返回 None。"""

    spec = _SESSION_SPECS.get(product)
    if spec is None:
        return None
    if spec.sessions_dir == "":
        return home
    return home / spec.sessions_dir


def scan_session_files(
    target: AuditTarget,
    *,
    project_cache: dict[str, tuple[int, float, str]] | None = None,
) -> tuple[SessionFile, ...]:
    """扫描一个目标目录下可归档/清理的会话文件。"""

    if target.sessions_root is None or not target.sessions_root.is_dir():
        return ()
    spec = _SESSION_SPECS.get(target.product)
    if spec is None:
        return ()
    files: list[SessionFile] = []
    for path in sorted(target.sessions_root.glob(spec.pattern)):
        try:
            stat_result = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        if (
            target.product in {"gemini", "qwen"}
            and path.suffix not in _CHAT_SUFFIXES
        ):
            continue
        files.append(
            SessionFile(
                path=path,
                product=target.product,
                label=target.label,
                size=stat_result.st_size,
                modified_at=stat_result.st_mtime,
                session_id=_resolve_session_id(target, path),
                project=_resolve_project(target, path, stat_result, project_cache),
            )
        )
    return tuple(files)


def _resolve_session_id(target: AuditTarget, path: Path) -> str:
    """按产品提取会话 ID，失败时退化为文件名。"""

    root = target.sessions_root
    product = target.product
    if product == "codex":
        return session_id_from_path(path)
    if product == "claude":
        return claude_session_id(path) or path.stem
    if product in {"kimi", "grok", "dsh"} and root is not None:
        try:
            relative = path.relative_to(root)
        except ValueError:
            return path.stem
        # 布局：<root>/<项目>/<会话>/...
        if len(relative.parts) >= 2:
            return relative.parts[1]
        return path.stem
    if product == "command-code":
        name = path.name
        for suffix in (".checkpoints.jsonl", ".meta.json", ".jsonl"):
            if name.endswith(suffix):
                return name[: -len(suffix)] or path.stem
    if product == "cursor":
        return cursor_session_id(path)
    if product == "aider":
        return path.parent.name or path.stem
    return path.stem


def _resolve_project(
    target: AuditTarget,
    path: Path,
    stat_result: os.stat_result,
    cache: dict[str, tuple[int, float, str]] | None,
) -> str:
    """解析会话文件的项目（工作目录），缓存按体积和修改时间失效。"""

    key = str(path)
    if cache is not None:
        cached = cache.get(key)
        if (
            cached is not None
            and cached[0] == stat_result.st_size
            and cached[1] == stat_result.st_mtime
        ):
            return cached[2]
    project = _extract_file_project(target, path) or _UNKNOWN_PROJECT
    if cache is not None:
        if len(cache) >= _PROJECT_CACHE_MAX:
            cache.clear()
        cache[key] = (stat_result.st_size, stat_result.st_mtime, project)
    return project


def _extract_file_project(target: AuditTarget, path: Path) -> str | None:
    """按产品从会话头部或目录名解析项目，不读取对话内容。"""

    root = target.sessions_root
    if root is None:
        return None
    product = target.product
    if product == "codex":
        return _codex_project(path)
    if product == "claude":
        return _claude_project(root, path)
    if product == "kimi":
        return _kimi_project(root, path)
    if product == "grok":
        return _grok_project(root, path)
    if product == "dsh":
        return _dsh_project(target.path, root, path)
    if product == "command-code":
        return _commandcode_project(path)
    if product == "cursor":
        return chat_project(target.path, path, product="cursor")
    if product in {"gemini", "qwen"}:
        return chat_project(target.path, path, product=product)
    if product == "aider":
        return str(path.parent)
    return None


def _codex_project(path: Path) -> str | None:
    """从 rollout 文件头部的 session_meta 读取工作目录。"""

    for line in _head_lines(path):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        containers: list[Any] = [event, event.get("payload")]
        for container in tuple(containers):
            if isinstance(container, dict):
                containers.append(container.get("session_meta"))
        for container in containers:
            if not isinstance(container, dict):
                continue
            cwd = container.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                return cwd.strip()[:1024]
    return None


def _claude_project(root: Path, path: Path) -> str | None:
    """Claude 主会话从文件头读 cwd；子代理文件回退到同级主会话。"""

    project = _head_cwd(path)
    if project is not None:
        return project
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    parts = relative.parts
    # 布局：projects/<项目>/<会话>/subagents/**/agent-*.jsonl
    if len(parts) >= 4 and parts[2] == "subagents":
        return _head_cwd(root / parts[0] / f"{parts[1]}.jsonl")
    return None


def _kimi_project(root: Path, path: Path) -> str | None:
    """Kimi 会话的项目写在会话目录的 state.json 里。"""

    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    parts = relative.parts
    # 布局：sessions/<工作目录>/<session>/...
    if len(parts) < 2:
        return None
    state_path = root / parts[0] / parts[1] / "state.json"
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    cwd = payload.get("cwd")
    return cwd.strip() if isinstance(cwd, str) and cwd.strip() else None


def _grok_project(root: Path, path: Path) -> str | None:
    """Grok 的项目直接编码在会话一级目录名里。"""

    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    if not relative.parts:
        return None
    return decode_grok_project(relative.parts[0])


def _dsh_project(home: Path, root: Path, path: Path) -> str | None:
    """DSH 会话目录名不可逆，项目从 projcache 快照读取。"""

    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    if len(relative.parts) < 2:
        return None
    snapshot = read_dsh_projcache(home, relative.parts[1])
    return snapshot.project if snapshot is not None else None


def _commandcode_project(path: Path) -> str | None:
    """Command Code 的项目写在会话 JSONL 头；meta 文件回退到同名主文件。"""

    info = read_commandcode_session_info(path)
    if info is not None and info.cwd:
        return info.cwd
    name = path.name
    for suffix in (".meta.json", ".checkpoints.jsonl"):
        if name.endswith(suffix):
            sibling = path.with_name(f"{name[: -len(suffix)]}.jsonl")
            info = read_commandcode_session_info(sibling)
            return info.cwd if info is not None and info.cwd else None
    return None


def _head_lines(path: Path) -> list[str]:
    """读取文件头部若干行；失败时返回空列表。"""

    try:
        with path.open("rb") as handle:
            chunk = handle.read(_HEAD_READ_BYTES)
    except OSError:
        return []
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    return [line for line in lines[:_HEAD_READ_LINES] if line.strip()]


def _head_cwd(path: Path) -> str | None:
    """在文件头部匹配首个 "cwd" 字段（Claude 风格的 JSONL 头）。"""

    try:
        with path.open("rb") as handle:
            chunk = handle.read(_HEAD_READ_BYTES)
    except OSError:
        return None
    match = _CWD_HEAD_PATTERN.search(chunk.decode("utf-8", errors="replace"))
    return match.group(1) if match is not None else None


def _path_is_protected(path: Path, active: set[str]) -> bool:
    """活动集合既含会话文件也含会话目录；目录下的所有文件都受保护。"""

    text = str(path)
    if text in active:
        return True
    return any(text.startswith(entry + os.sep) for entry in active)


def _project_slug(project: str) -> str | None:
    """把项目路径转成归档文件名里可读的短 slug。"""

    text = project.strip()
    if not text or text == _UNKNOWN_PROJECT:
        return None
    base = text.rstrip("/").rsplit("/", 1)[-1] or text
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-.")
    return (slug or "project")[:_PROJECT_SLUG_MAX]


def _archive_name_hint(criteria: CleanupCriteria) -> str | None:
    """只在恰好选中一个项目时把项目名写进归档文件名。"""

    if len(criteria.projects) != 1:
        return None
    return _project_slug(criteria.projects[0])


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
    *,
    active_paths: Callable[[], set[str]] | None = None,
    signatures: Mapping[Path, tuple[int, ...]] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """删除文件，返回成功路径和失败原因。"""

    deleted: list[str] = []
    failed: list[dict[str, str]] = []
    total_count = total if total is not None else len(files)
    total_bytes = sum(item.size for item in files)
    bytes_done = 0
    for index, item in enumerate(files, start=1):
        try:
            # 中文注释：名单读取失败时直接跳过删除，不能把失败当成没有活动会话。
            if active_paths is not None and _path_is_protected(
                item.path, active_paths()
            ):
                raise HousekeepingError("会话重新开始运行，已保留原文件")
            signature = _session_file_signature(item.path)
            expected = signatures.get(item.path) if signatures is not None else None
            if (
                (expected is not None and signature != expected)
                or signature[2] != item.size
                or item.path.stat().st_mtime != item.modified_at
            ):
                raise HousekeepingError("会话文件发生变化，已保留原文件")
            item.path.unlink()
            deleted.append(str(item.path))
        except (OSError, HousekeepingError) as error:
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


def _session_file_signature(path: Path) -> tuple[int, ...]:
    """记录文件身份和写入状态；拒绝符号链接及非普通文件。"""

    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise HousekeepingError("会话路径不是普通文件，已保留原路径")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


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
    except Exception:  # noqa: BLE001 - 进度回调只是展示用途，失败不影响归档本身
        logging.getLogger(__name__).debug("归档进度回调失败，已忽略", exc_info=True)


def _archive_member_name(path: PurePath) -> str:
    """会话文件在归档里的成员名，与 tarfile 写入时的规范化一致。

    tarfile 会去掉盘符、把 Windows 的 ``\\`` 换成 ``/`` 并去掉开头的 ``/``；
    校验时若直接用 ``str(path)``，Windows 上每个成员都对不上，归档会被判失败。
    """

    _, name = os.path.splitdrive(str(path))
    return name.replace(os.sep, "/").lstrip("/")


def _member_anchors(manifest: Mapping[str, Any] | None) -> dict[str, str]:
    """从 manifest 的原始路径推出每个归档成员该写回的根（Windows 上是所在盘）。"""

    anchors: dict[str, str] = {}
    files = manifest.get("files") if manifest is not None else None
    if not isinstance(files, list):
        return anchors
    for entry in files:
        original = entry.get("path") if isinstance(entry, Mapping) else None
        if isinstance(original, str) and original:
            path = Path(original)
            anchors[_archive_member_name(path)] = path.anchor or os.sep
    return anchors


def _missing_members(archive: Path, files: Sequence[SessionFile]) -> set[str]:
    """校验归档里是否包含全部待删除文件。"""

    expected = {_archive_member_name(item.path): item.size for item in files}
    try:
        with tarfile.open(archive, "r:gz") as handle:
            members: set[str] = set()
            for item in handle:
                if not item.isfile() or item.name not in expected:
                    continue
                if item.size != expected[item.name]:
                    continue
                # 中文注释：完整读取每个成员，检测损坏或截断，不能仅检查文件名。
                source = handle.extractfile(item)
                if source is None:
                    continue
                with source:
                    read_bytes = 0
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        read_bytes += len(chunk)
                if read_bytes == item.size:
                    members.add(item.name)
    except (OSError, tarfile.TarError) as error:
        raise HousekeepingError(f"无法校验归档: {error}") from error
    return set(expected) - members


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
