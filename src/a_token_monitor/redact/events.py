"""脱敏事件的元数据日志：给 Dashboard 画拦截时间轴用。

日志里只放时间、来源 agent / 上游、事件阶段、动作、命中类别与数量、敏感值字节数
和扫描过的正文大小。**不写命中的原值，也不写任何上下文**，所以即使日志被读到，
也无法从中还原密钥。``stats.json`` 保存累计计数（事件日志会滚动裁剪，累计值不丢），
``events.jsonl`` 保存最近的事件明细，两个文件都在 ``redact/`` 下、权限 0600。
"""

from __future__ import annotations

import json
import math
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

try:  # 中文注释：Windows 没有 fcntl，退化为进程内锁（与密钥表一致）。
    import fcntl
except ImportError:  # pragma: no cover - 平台相关
    fcntl = None  # type: ignore[assignment]


# 中文注释：仪表盘只展示最近一段记录；累计数字来自 stats.json，不受裁剪影响。
MAX_EVENTS = 1000
_MAX_EVENT_FILE_BYTES = 1024 * 1024
CHANNELS = ("prompt", "tool_output", "request")
ACTIONS = ("block", "warn", "redact")

_EMPTY_STATS: dict[str, Any] = {
    "events": 0,
    "findings": 0,
    "blocked_events": 0,
    "warned_events": 0,
    "redacted_events": 0,
    "blocked_bytes": 0,
    "secret_bytes": 0,
    "payload_bytes": 0,
    "kinds": {},
    "first_at": None,
    "last_at": None,
}


def _non_negative_int(value: Any) -> int | None:
    """只接受非负整数（布尔值不算），用于清理读到的日志字段。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _clean_name(value: Any, *, limit: int = 64) -> str:
    """来源名只保留可打印字符并截断，避免日志字段带控制字符。"""

    if not isinstance(value, str):
        return "unknown"
    cleaned = "".join(char for char in value if char.isprintable() and char not in "\r\n\t")
    return cleaned[:limit] or "unknown"


@dataclass(frozen=True)
class RedactEvent:
    """一次脱敏命中：只描述「发生了什么」，不包含原值。"""

    at: float
    agent: str
    channel: str
    action: str
    findings: int
    kinds: Mapping[str, int]
    secret_bytes: int
    payload_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "agent": self.agent,
            "channel": self.channel,
            "action": self.action,
            "findings": self.findings,
            "kinds": dict(self.kinds),
            "secret_bytes": self.secret_bytes,
            "payload_bytes": self.payload_bytes,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> RedactEvent | None:
        """从日志行恢复事件；字段不合法时返回 None，坏行直接跳过。"""

        try:
            at = float(str(raw.get("at")))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(at) or at <= 0:
            return None
        findings = _non_negative_int(raw.get("findings"))
        secret_bytes = _non_negative_int(raw.get("secret_bytes"))
        payload_bytes = _non_negative_int(raw.get("payload_bytes"))
        if findings is None or secret_bytes is None or payload_bytes is None:
            return None
        channel = raw.get("channel") if raw.get("channel") in CHANNELS else "request"
        action = raw.get("action") if raw.get("action") in ACTIONS else "redact"
        raw_kinds = raw.get("kinds")
        kinds: dict[str, int] = {}
        if isinstance(raw_kinds, Mapping):
            for name, count in list(raw_kinds.items())[:64]:
                if not isinstance(name, str) or not name:
                    continue
                cleaned = _non_negative_int(count)
                if cleaned:
                    kinds[_clean_name(name, limit=32)] = cleaned
        return cls(
            at=at,
            agent=_clean_name(raw.get("agent")),
            channel=str(channel),
            action=str(action),
            findings=findings,
            kinds=kinds,
            secret_bytes=secret_bytes,
            payload_bytes=payload_bytes,
        )


class RedactEventLog:
    """``redact/events.jsonl`` 与 ``redact/stats.json`` 的读写封装。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.events_path = self.root / "events.jsonl"
        self.stats_path = self.root / "stats.json"
        self.lock_path = self.root / "events.lock"
        self._thread_lock = threading.Lock()

    # ----- 写入 -----

    def append(self, event: RedactEvent) -> None:
        """追加一条事件并更新累计计数；调用方负责把异常降级为日志。"""

        self._ensure_root()
        with self._locked():
            # 中文注释：stats.json 丢了就先用还留着的明细重建，再加这一条，避免从 0 开始。
            stats = self._read_stats()
            if stats is None:
                stats = self._stats_from_events(self._read_event_records())
            self._append_line(event)
            self._bump_stats(event, stats)
            self._trim()

    def _append_line(self, event: RedactEvent) -> None:
        line = json.dumps(event.as_dict(), ensure_ascii=False, separators=(",", ":"))
        with open(self.events_path, "a", encoding="utf-8") as handle:
            try:
                os.chmod(self.events_path, 0o600)
            except OSError:  # pragma: no cover - 平台相关
                pass
            handle.write(line + "\n")

    def _bump_stats(self, event: RedactEvent, stats: dict[str, Any]) -> None:
        stats["events"] += 1
        stats["findings"] += event.findings
        stats["secret_bytes"] += event.secret_bytes
        stats["payload_bytes"] += event.payload_bytes
        if event.action == "block":
            stats["blocked_events"] += 1
            stats["blocked_bytes"] += event.payload_bytes
        elif event.action == "warn":
            stats["warned_events"] += 1
        else:
            stats["redacted_events"] += 1
        kinds = stats["kinds"]
        for name, count in event.kinds.items():
            # 中文注释：规则名由用户自定义；限制种类数量，避免统计文件无限长大。
            if name not in kinds and len(kinds) >= 64:
                continue
            kinds[name] = kinds.get(name, 0) + count
        stats["first_at"] = event.at if stats["first_at"] is None else min(stats["first_at"], event.at)
        stats["last_at"] = event.at if stats["last_at"] is None else max(stats["last_at"], event.at)
        _write_private_json(self.stats_path, stats)

    def _trim(self) -> None:
        """文件超过上限时只保留最近 MAX_EVENTS 条；累计数字在 stats.json 里。"""

        try:
            if self.events_path.stat().st_size <= _MAX_EVENT_FILE_BYTES:
                return
            lines = self.events_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        if len(lines) <= MAX_EVENTS:
            return
        temporary = self.events_path.with_name(f".{self.events_path.name}.{os.getpid()}.tmp")
        temporary.write_text("\n".join(lines[-MAX_EVENTS:]) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.events_path)

    # ----- 读取 -----

    def snapshot(self, limit: int = 100) -> dict[str, Any]:
        """返回累计统计和最近的 ``limit`` 条事件（按时间倒序）。"""

        limit = max(1, min(int(limit), MAX_EVENTS))
        events = self._read_event_records()
        stats = self._read_stats()
        if stats is None:
            stats = self._stats_from_events(events)
        events.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        return {
            "stats": stats,
            "events": [event.as_dict() for _index, _order, event in events[:limit]],
        }

    def _read_event_records(self) -> list[tuple[float, int, RedactEvent]]:
        try:
            text = self.events_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return []
        except OSError:
            return []
        records: list[tuple[float, int, RedactEvent]] = []
        for order, line in enumerate(text.splitlines()):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, Mapping):
                continue
            event = RedactEvent.from_mapping(raw)
            if event is not None:
                records.append((event.at, order, event))
        return records

    def _read_stats(self) -> dict[str, Any] | None:
        try:
            raw = json.loads(self.stats_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, Mapping):
            return None
        stats = self._empty_stats()
        for name in ("events", "findings", "blocked_events", "warned_events", "redacted_events", "blocked_bytes", "secret_bytes", "payload_bytes"):
            value = _non_negative_int(raw.get(name))
            if value is not None:
                stats[name] = value
        for name in ("first_at", "last_at"):
            try:
                time_value = float(str(raw.get(name)))
            except (TypeError, ValueError):
                continue
            if math.isfinite(time_value) and time_value > 0:
                stats[name] = time_value
        raw_kinds = raw.get("kinds")
        if isinstance(raw_kinds, Mapping):
            kinds: dict[str, int] = {}
            for name, count in list(raw_kinds.items())[:64]:
                if not isinstance(name, str) or not name:
                    continue
                value = _non_negative_int(count)
                if value:
                    kinds[_clean_name(name, limit=32)] = value
            stats["kinds"] = kinds
        return stats

    @staticmethod
    def _empty_stats() -> dict[str, Any]:
        return {**_EMPTY_STATS, "kinds": {}}

    def _stats_from_events(self, records: list[tuple[float, int, RedactEvent]]) -> dict[str, Any]:
        """stats.json 丢失或损坏时，用保留的明细重建一份近似值。"""

        stats = self._empty_stats()
        for _at, _order, event in records:
            stats["events"] += 1
            stats["findings"] += event.findings
            stats["secret_bytes"] += event.secret_bytes
            stats["payload_bytes"] += event.payload_bytes
            if event.action == "block":
                stats["blocked_events"] += 1
                stats["blocked_bytes"] += event.payload_bytes
            elif event.action == "warn":
                stats["warned_events"] += 1
            else:
                stats["redacted_events"] += 1
            for name, count in event.kinds.items():
                if name not in stats["kinds"] and len(stats["kinds"]) >= 64:
                    continue
                stats["kinds"][name] = stats["kinds"].get(name, 0) + count
            stats["first_at"] = event.at if stats["first_at"] is None else min(stats["first_at"], event.at)
            stats["last_at"] = event.at if stats["last_at"] is None else max(stats["last_at"], event.at)
        return stats

    # ----- 小工具 -----

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:  # pragma: no cover - 平台相关
            pass

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            if fcntl is None:  # pragma: no cover - 平台相关
                yield
                return
            self._ensure_root()
            with open(self.lock_path, "a+") as handle:
                try:
                    os.chmod(self.lock_path, 0o600)
                except OSError:  # pragma: no cover - 平台相关
                    pass
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def _write_private_json(path: Path, payload: Any) -> None:
    """0600 权限的原子 JSON 写入（与 rules 里的实现保持一致）。"""

    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


__all__ = [
    "ACTIONS",
    "CHANNELS",
    "MAX_EVENTS",
    "RedactEvent",
    "RedactEventLog",
]
