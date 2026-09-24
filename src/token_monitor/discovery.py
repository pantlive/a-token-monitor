"""发现正在运行的 Codex 进程和其打开的 session JSONL 文件。

Codex 的历史 JSONL 文件会长期留在 ``~/.codex/sessions``，所以单看文件修改
时间不足以证明会话还在运行。本模块在 Linux 上读取 ``/proc`` 的文件描述符，
只有被活动进程实际打开的 JSONL 才会被标记为强证据；App Server 提供的会话
摘要由上层监控器负责合并。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .events import EventObservation, parse_event_line


@dataclass(frozen=True)
class ProcessObservation:
    """一个持有 Codex session JSONL 文件的进程。"""

    pid: int
    start_token: str
    cwd: Path | None
    command: tuple[str, ...]
    open_jsonl_paths: tuple[Path, ...]


@dataclass(frozen=True)
class SessionMetadata:
    """从 session_meta 或兼容事件中提取的会话元数据。"""

    thread_id: str | None = None
    session_id: str | None = None
    cwd: Path | None = None
    source: str | None = None
    parent_thread_id: str | None = None
    root_thread_id: str | None = None


@dataclass(frozen=True)
class SessionEvent:
    """JSONL 中一条已解析的会话事件。"""

    observation: EventObservation
    event_type: str | None
    timestamp: float | None


@dataclass(frozen=True)
class SessionTail:
    """从上次偏移量开始读取到的 JSONL 增量。"""

    path: Path
    next_offset: int
    events: tuple[SessionEvent, ...]
    metadata: SessionMetadata | None = None
    terminal_event: bool = False
    approval_waiting: bool = False

    @property
    def quota_events(self) -> tuple[SessionEvent, ...]:
        """返回本次增量中识别为额度耗尽的事件。"""

        return tuple(
            event for event in self.events if event.observation.quota_exhausted
        )


def default_session_root() -> Path:
    """返回 Codex 默认 session JSONL 目录。"""

    configured_home = os.environ.get("CODEX_HOME")
    home = (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".codex"
    )
    return home / "sessions"


class ProcessScanner:
    """通过 Linux ``/proc`` 发现打开 session JSONL 的进程。"""

    def __init__(
        self,
        session_root: Path | None = None,
        proc_root: Path | None = None,
        ignore_pids: tuple[int, ...] | None = None,
    ) -> None:
        self.session_root = (session_root or default_session_root()).expanduser()
        # 中文注释：保留 None 表示“按平台自动选择后端”，显式路径（测试、容器）
        # 才固定按 /proc 语义处理。
        self.proc_root = proc_root
        self.ignore_pids = set(ignore_pids if ignore_pids is not None else (os.getpid(),))
        try:
            self._session_root_resolved = self.session_root.resolve()
        except OSError:
            self._session_root_resolved = self.session_root.absolute()

    def scan(self) -> tuple[ProcessObservation, ...]:
        """扫描当前可读进程，返回持有 session JSONL 的进程。"""

        from .process_backend import process_root, select_backend

        if select_backend(self.proc_root) in {"macos", "windows"}:
            return self._scan_portable()
        proc_path = process_root(self.proc_root)
        if not proc_path.is_dir():
            return ()
        observations: list[ProcessObservation] = []
        try:
            process_directories = tuple(proc_path.iterdir())
        except OSError:
            return ()
        for process_directory in process_directories:
            if not process_directory.name.isdigit():
                continue
            observation = self._scan_process(process_directory)
            if observation is not None:
                observations.append(observation)
        return tuple(observations)

    def _scan_portable(self) -> tuple[ProcessObservation, ...]:
        """macOS / Windows：用平台后端找到持有 session JSONL 的 agent 进程。

        macOS 走 ``ps`` + ``lsof``，Windows 走 Toolhelp32 + Restart Manager；
        两者都返回统一的 :class:`~token_monitor.agents.RunningAgent`。
        """

        from .agents import scan_running_agents

        observations: list[ProcessObservation] = []
        agents = scan_running_agents(
            ignore_pids=tuple(sorted(self.ignore_pids)),
            session_roots=(self.session_root,),
        )
        for agent in agents:
            open_paths = {
                path
                for path in agent.open_paths
                if self._is_session_jsonl(path)
            }
            if not open_paths:
                continue
            observations.append(
                ProcessObservation(
                    pid=agent.pid,
                    start_token=agent.start_token,
                    cwd=agent.cwd,
                    command=agent.command,
                    open_jsonl_paths=tuple(sorted(open_paths, key=str)),
                )
            )
        return tuple(observations)

    def _scan_process(self, process_directory: Path) -> ProcessObservation | None:
        """读取一个 PID 目录，单个进程不可读时跳过。"""

        try:
            pid = int(process_directory.name)
            if pid in self.ignore_pids:
                return None
            command = self._read_command(process_directory / "cmdline")
            cwd = self._read_link(process_directory / "cwd")
            start_token = self._read_start_token(process_directory / "stat")
            fd_directory = process_directory / "fd"
            open_paths: set[Path] = set()
            for descriptor in fd_directory.iterdir():
                target = self._read_link(descriptor, strip_deleted=True)
                if target is None or not self._is_session_jsonl(target):
                    continue
                open_paths.add(target)
        except (OSError, ValueError):
            return None
        if not open_paths:
            return None
        return ProcessObservation(
            pid=pid,
            start_token=start_token,
            cwd=cwd,
            command=command,
            open_jsonl_paths=tuple(sorted(open_paths, key=str)),
        )

    def _is_session_jsonl(self, path: Path) -> bool:
        """确认路径位于 session 根目录下且扩展名为 JSONL。"""

        if path.suffix.lower() != ".jsonl":
            return False
        try:
            path_resolved = path.resolve()
        except OSError:
            path_resolved = path
        try:
            path_resolved.relative_to(self._session_root_resolved)
        except ValueError:
            return False
        return True

    @staticmethod
    def _read_command(path: Path) -> tuple[str, ...]:
        """读取 NUL 分隔的进程命令行。"""

        raw = path.read_bytes()
        return tuple(
            item.decode("utf-8", errors="replace") for item in raw.split(b"\0") if item
        )

    @staticmethod
    def _read_link(path: Path, strip_deleted: bool = False) -> Path | None:
        """读取 proc 符号链接，不跟随不存在的目标。"""

        try:
            target = path.readlink()
        except OSError:
            return None
        target_text = str(target)
        if strip_deleted and target_text.endswith(" (deleted)"):
            target_text = target_text[: -len(" (deleted)")]
        target_path = Path(target_text)
        try:
            return target_path.resolve()
        except OSError:
            return target_path

    @staticmethod
    def _read_start_token(path: Path) -> str:
        """读取 Linux 进程启动时刻，防止 PID 重用造成误认。"""

        content = path.read_text(encoding="utf-8", errors="replace")
        closing_parenthesis = content.rfind(")")
        if closing_parenthesis < 0:
            return ""
        fields_after_name = content[closing_parenthesis + 1 :].split()
        # /proc/[pid]/stat 的第 22 列是 starttime；分割后第 20 项对应它。
        return fields_after_name[19] if len(fields_after_name) > 19 else ""


class JsonlSessionReader:
    """按字节偏移增量读取 Codex session JSONL。"""

    _DEFAULT_INITIAL_BYTES = 256 * 1024
    _TERMINAL_EVENT_TYPES = {
        "task_complete",
        "task_completed",
        "turn_completed",
        "turn.complete",
        "turn.failed",
        "turn_failed",
        "turn_aborted",
        "turn.aborted",
        "response.completed",
        "response_completed",
        "response.failed",
        "response_failed",
        "response.incomplete",
        "response_incomplete",
        "thread.completed",
        "thread_completed",
        "thread.archived",
        "error",
    }
    _APPROVAL_EVENT_TYPES = {
        "approval_requested",
        "approval_request",
        "exec_approval_request",
        "command_approval_request",
        "request_user_input",
        "user_input_requested",
        "waiting_for_approval",
        "waiting_on_approval",
    }

    def initial_offset(
        self,
        path: Path,
        maximum_bytes: int = _DEFAULT_INITIAL_BYTES,
    ) -> int:
        """为新会话选择最近一段内容，避免首次读取超大历史文件。"""

        if maximum_bytes < 0:
            raise ValueError("maximum_bytes 不能小于 0")
        try:
            size = path.stat().st_size
        except OSError:
            return 0
        return max(0, size - maximum_bytes)

    def read_metadata(self, path: Path) -> SessionMetadata | None:
        """只读取文件开头的元数据，不读取用户提示词或完整历史。"""

        try:
            with path.open("rb") as handle:
                remaining_bytes = self._DEFAULT_INITIAL_BYTES
                for _ in range(32):
                    if remaining_bytes <= 0:
                        break
                    line = handle.readline(remaining_bytes)
                    if not line:
                        break
                    remaining_bytes -= len(line)
                    metadata = self._metadata_from_line(line)
                    if metadata is not None:
                        return metadata
        except OSError:
            return None
        return None

    def read(
        self,
        path: Path,
        offset: int = 0,
        now: float | None = None,
        maximum_bytes: int | None = None,
    ) -> SessionTail:
        """读取完整 JSONL 行并返回下一次应使用的偏移量。"""

        if offset < 0:
            raise ValueError("offset 不能小于 0")
        if maximum_bytes is not None and maximum_bytes <= 0:
            raise ValueError("maximum_bytes 必须大于 0")
        current_time = (
            now if now is not None else datetime.now(timezone.utc).timestamp()
        )
        try:
            file_size = path.stat().st_size
            if offset > file_size:
                offset = 0
            with path.open("rb") as handle:
                read_offset = offset
                if offset > 0:
                    # 首次读取通常从“最近 N 字节”开始，可能落在 JSON 行中间；
                    # 丢弃这一段残行，避免把截断文本误判成额度错误。
                    handle.seek(offset - 1)
                    previous_byte = handle.read(1)
                    handle.seek(offset)
                    if previous_byte != b"\n":
                        discarded = handle.readline()
                        read_offset += len(discarded)
                content = (
                    handle.read(maximum_bytes)
                    if maximum_bytes is not None
                    else handle.read()
                )
        except OSError:
            return SessionTail(path=path, next_offset=offset, events=())

        complete_content = content
        if content and not content.endswith(b"\n"):
            last_newline = content.rfind(b"\n")
            if last_newline < 0:
                complete_content = b""
            else:
                complete_content = content[: last_newline + 1]
        next_offset = read_offset + len(complete_content)
        events: list[SessionEvent] = []
        metadata: SessionMetadata | None = None
        terminal_event = False
        approval_waiting = False
        for raw_line in complete_content.splitlines(keepends=True):
            line = raw_line.decode("utf-8", errors="replace")
            observation = parse_event_line(line, now=current_time)
            payload = self._json_object(line)
            event_type = self._semantic_event_type(payload, observation)
            timestamp = self._event_timestamp(payload)
            events.append(
                SessionEvent(
                    observation=observation,
                    event_type=event_type,
                    timestamp=timestamp,
                )
            )
            if metadata is None:
                metadata = self._metadata_from_payload(payload, event_type)
            terminal_event = terminal_event or event_type in self._TERMINAL_EVENT_TYPES
            approval_waiting = approval_waiting or self._is_approval_event(
                payload,
                event_type,
            )
        return SessionTail(
            path=path,
            next_offset=next_offset,
            events=tuple(events),
            metadata=metadata,
            terminal_event=terminal_event,
            approval_waiting=approval_waiting,
        )

    @staticmethod
    def _json_object(line: str) -> Mapping[str, Any]:
        """解析 JSON 对象，文本输出统一返回空对象。"""

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, Mapping) else {}

    @staticmethod
    def _semantic_event_type(
        payload: Mapping[str, Any],
        observation: EventObservation,
    ) -> str | None:
        """兼容 rollout 外层类型和内层 payload.type。"""

        nested = payload.get("payload")
        if isinstance(nested, Mapping):
            for key in ("type", "event", "kind"):
                value = nested.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip().lower()
        return observation.event_type

    @staticmethod
    def _event_timestamp(payload: Mapping[str, Any]) -> float | None:
        """解析事件自身时间戳。"""

        value = payload.get("timestamp")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            timestamp = float(value)
            return timestamp / 1000 if timestamp > 10_000_000_000 else timestamp
        if isinstance(value, str):
            try:
                numeric = float(value)
            except ValueError:
                numeric = None
            if numeric is not None:
                return numeric / 1000 if numeric > 10_000_000_000 else numeric
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        return None

    def _metadata_from_line(self, line: bytes) -> SessionMetadata | None:
        """从文件头的一行尝试提取会话元数据。"""

        try:
            payload = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, Mapping):
            return None
        event_type = self._semantic_event_type(
            payload,
            parse_event_line(json.dumps(payload, ensure_ascii=False)),
        )
        return self._metadata_from_payload(payload, event_type)

    @staticmethod
    def _metadata_from_payload(
        payload: Mapping[str, Any],
        event_type: str | None,
    ) -> SessionMetadata | None:
        """从 session_meta 或 thread.started 事件提取元数据。"""

        if payload.get("type") == "session_meta":
            metadata = payload.get("payload")
            data = metadata if isinstance(metadata, Mapping) else payload
            session_id = JsonlSessionReader._first_string(
                data,
                ("session_id", "sessionId", "id"),
            )
            thread_id = JsonlSessionReader._first_string(
                data,
                ("thread_id", "threadId", "id", "session_id", "sessionId"),
            )
            cwd = JsonlSessionReader._path_value(data, ("cwd", "working_directory"))
            source = JsonlSessionReader._first_string(
                data,
                ("source", "originator", "thread_source"),
            )
            parent = JsonlSessionReader._first_string(
                data,
                ("parent_thread_id", "parentThreadId", "parent_id"),
            )
            root = JsonlSessionReader._first_string(
                data,
                ("root_thread_id", "rootThreadId", "root_id"),
            )
            if any((thread_id, session_id, cwd, source, parent, root)):
                return SessionMetadata(
                    thread_id=thread_id,
                    session_id=session_id or thread_id,
                    cwd=cwd,
                    source=source,
                    parent_thread_id=parent,
                    root_thread_id=root,
                )

        if event_type == "thread.started":
            session_id = JsonlSessionReader._first_string(
                payload,
                ("session_id", "sessionId", "thread_id", "threadId"),
            )
            if session_id:
                return SessionMetadata(thread_id=session_id, session_id=session_id)
        return None

    @staticmethod
    def _first_string(
        data: Mapping[str, Any],
        keys: tuple[str, ...],
    ) -> str | None:
        """按优先级读取非空字符串。"""

        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _path_value(
        data: Mapping[str, Any],
        keys: tuple[str, ...],
    ) -> Path | None:
        """把工作目录字段转换成 Path。"""

        value = JsonlSessionReader._first_string(data, keys)
        return Path(value).expanduser() if value else None

    @classmethod
    def _is_approval_event(
        cls,
        payload: Mapping[str, Any],
        event_type: str | None,
    ) -> bool:
        """识别需要用户批准或输入的暂停事件。"""

        if event_type in cls._APPROVAL_EVENT_TYPES:
            return True
        approval_keys = {
            "approvalrequest",
            "execapprovalrequest",
            "commandapprovalrequest",
            "requestuserinput",
            "userinputrequested",
            "waitingonapproval",
            "waitingonuserinput",
            "approvalrequired",
        }
        waiting_values = {
            "waitingonapproval",
            "waitingonuserinput",
            "approvalrequired",
        }

        def visit(value: Any) -> bool:
            if not isinstance(value, Mapping):
                if isinstance(value, list):
                    return any(visit(child) for child in value)
                return False
            for raw_key, child in value.items():
                key = re.sub(r"[^a-z0-9]", "", str(raw_key).lower())
                if (
                    key in approval_keys
                    and child is not False
                    and child is not None
                    and child != ""
                ):
                    return True
                if key in {"status", "state", "type", "event", "kind"}:
                    if isinstance(child, str):
                        normalized = re.sub(
                            r"([a-z0-9])([A-Z])",
                            r"\1\2",
                            child,
                        )
                        normalized = re.sub(
                            r"[^a-z0-9]",
                            "",
                            normalized.lower(),
                        )
                        if normalized in waiting_values:
                            return True
                if visit(child):
                    return True
            return False

        return visit(payload)
