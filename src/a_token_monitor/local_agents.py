"""OpenCode、Cursor、Gemini CLI、Qwen Code 与 Aider 的目录和按请求用量。

只读取 token 计数、模型、时间戳和项目路径。不读消息正文，不读凭证表，
也不按文本长度或字节数估算 token。Cursor 行里没有 usage/tokens 对象时
贡献 0；没有逐行时间戳时用文件修改时间，同一文件可能共用一个时间。
Aider 只认聊天历史里的 ``Tokens:`` 行。满 1000 之后是它自己四舍五入的
显示值，不是接口返回的原始整数。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote


_UNKNOWN_MODEL = "未知模型"
_BLOB_LIMIT = 32 * 1024 * 1024
_MAX_LINE_BYTES = 1024 * 1024
_PROJECTS_JSON_LIMIT = 1024 * 1024
_GEMINI_TYPES = frozenset({"gemini", "model", "message_update"})
_HEADER_KEYS = ("cwd", "projectPath", "directory")
_INPUT_KEYS = (
    "input_tokens",
    "inputTokens",
    "input",
    "prompt_tokens",
    "promptTokens",
)
_OUTPUT_KEYS = (
    "output_tokens",
    "outputTokens",
    "output",
    "completion_tokens",
    "completionTokens",
)
_CACHED_KEYS = (
    "cached_input_tokens",
    "cachedInputTokens",
    "cached",
    "cache_read",
    "cacheRead",
    "cache_read_input_tokens",
)
_WRITE_KEYS = (
    "cache_write_input_tokens",
    "cacheWriteInputTokens",
    "cache_write",
    "cacheWrite",
    "cache_creation_input_tokens",
)
_REASON_KEYS = (
    "reasoning_output_tokens",
    "reasoningOutputTokens",
    "reasoning",
    "reasoning_tokens",
    "reasoningTokens",
    "thoughts",
)
_TOTAL_KEYS = ("total_tokens", "totalTokens", "total")


@dataclass(frozen=True)
class CountedUsage:
    """一次请求上真实记录的 token，不是文本长度估算。"""

    timestamp: float
    model: str
    project: str | None
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    dedupe_key: str


@dataclass(frozen=True)
class ChatChunk:
    """一段 JSONL 解析结果；未读完时 reached_eof 为 False。"""

    next_offset: int
    events: tuple[CountedUsage, ...]
    project: str | None
    bytes_read: int
    reached_eof: bool


@dataclass(frozen=True)
class AiderChunk:
    """一段 Aider 聊天历史；模型和会话起点要带到下一次增量读取。"""

    next_offset: int
    events: tuple[CountedUsage, ...]
    bytes_read: int
    reached_eof: bool
    model: str
    session_timestamp: float | None


def default_opencode_home() -> Path:
    """返回 OpenCode 数据目录（含 opencode.db 的那一层）。"""

    configured = os.environ.get("OPENCODE_DB")
    if configured:
        path = Path(configured).expanduser()
        if path.name == "opencode.db":
            return path.parent
        return path if path.suffix == "" else path.parent
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "opencode"


def default_cursor_home() -> Path:
    """返回 Cursor 配置目录。"""

    configured = os.environ.get("CURSOR_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "cursor"
    return Path.home() / ".cursor"


def default_gemini_home() -> Path:
    """返回 Gemini CLI 数据目录。"""

    configured = os.environ.get("GEMINI_CLI_HOME")
    if configured:
        raw = Path(configured).expanduser()
        return raw if raw.name == ".gemini" else raw / ".gemini"
    return Path.home() / ".gemini"


def default_qwen_home() -> Path:
    """返回 Qwen Code 数据目录。"""

    configured = os.environ.get("QWEN_HOME") or os.environ.get("QWEN_CODE_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".qwen"


def resolve_opencode_homes(
    homes: Sequence[Path] | None = None,
) -> tuple[Path, ...]:
    """解析 OpenCode 目录；None 时仅在默认目录存在时使用它。"""

    return _resolve(homes, default_opencode_home)


def resolve_cursor_homes(
    homes: Sequence[Path] | None = None,
) -> tuple[Path, ...]:
    """解析 Cursor 目录；None 时仅在默认目录存在时使用它。"""

    return _resolve(homes, default_cursor_home)


def resolve_gemini_homes(
    homes: Sequence[Path] | None = None,
) -> tuple[Path, ...]:
    """解析 Gemini CLI 目录；None 时仅在默认目录存在时使用它。"""

    return _resolve(homes, default_gemini_home)


def resolve_qwen_homes(
    homes: Sequence[Path] | None = None,
) -> tuple[Path, ...]:
    """解析 Qwen Code 目录；None 时仅在默认目录存在时使用它。"""

    return _resolve(homes, default_qwen_home)


def default_aider_home() -> Path:
    """返回 Aider 数据目录。项目根或 ``~/.aider`` 都可以。"""

    configured = os.environ.get("AIDER_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".aider"


def resolve_aider_homes(
    homes: Sequence[Path] | None = None,
) -> tuple[Path, ...]:
    """解析 Aider 目录；None 时仅在默认目录存在时使用它。"""

    return _resolve(homes, default_aider_home)


def opencode_db_path(home: Path) -> Path:
    """返回某个 OpenCode 目录里的数据库文件。"""

    path = Path(home)
    if path.name == "opencode.db":
        return path
    return path / "opencode.db"


def list_opencode_dbs(homes: Sequence[Path]) -> tuple[Path, ...]:
    """列出存在的 opencode.db。不打开库，也不把它当成可删除的会话文件。"""

    found: list[Path] = []
    for home in homes:
        database = opencode_db_path(home)
        if database.is_file():
            found.append(database)
    return tuple(found)


def list_cursor_transcripts(home: Path) -> tuple[Path, ...]:
    """列出 Cursor agent 转录。store.db 不在此列，也不解码。"""

    root = Path(home) / "projects"
    if not root.is_dir():
        return ()
    try:
        paths = [
            path
            for path in root.glob("**/agent-transcripts/**/*.jsonl")
            if path.is_file()
        ]
    except OSError:
        return ()
    return tuple(sorted(paths, key=str))


def list_gemini_chats(home: Path) -> tuple[Path, ...]:
    """列出 Gemini CLI 的 tmp/<project>/chats 记录。"""

    return _list_chats(home, ("tmp",))


def list_qwen_chats(home: Path) -> tuple[Path, ...]:
    """列出 Qwen Code 的 tmp 与 projects 聊天记录。"""

    return _list_chats(home, ("tmp", "projects"))


def list_aider_histories(home: Path) -> tuple[Path, ...]:
    """只认该目录根上的聊天历史，不递归扫描子目录。"""

    path = Path(home) / ".aider.chat.history.md"
    try:
        if path.is_file():
            return (path,)
    except OSError:
        return ()
    return ()


def read_opencode_usage(path: Path) -> tuple[CountedUsage, ...] | None:
    """只读 assistant 消息上的 tokens 字段。打不开时返回 None。"""

    try:
        connection = sqlite3.connect(_sqlite_uri(path), uri=True)
    except sqlite3.Error:
        return None
    try:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            """
            SELECT
                message.id,
                message.time_created,
                json_extract(message.data, '$.modelID'),
                json_extract(message.data, '$.model.modelID'),
                json_extract(message.data, '$.tokens.input'),
                json_extract(message.data, '$.tokens.output'),
                json_extract(message.data, '$.tokens.reasoning'),
                json_extract(message.data, '$.tokens.cache.read'),
                json_extract(message.data, '$.tokens.cache.write'),
                session.directory
            FROM message
            LEFT JOIN session ON session.id = message.session_id
            WHERE json_extract(message.data, '$.role') = 'assistant'
              AND json_type(message.data, '$.tokens') = 'object'
            ORDER BY message.time_created, message.id
            """
        ).fetchall()
    except sqlite3.Error:
        return ()
    finally:
        connection.close()
    events: list[CountedUsage] = []
    seen: set[str] = set()
    for row in rows:
        event = _opencode_row(row)
        if event is None or event.dedupe_key in seen:
            continue
        seen.add(event.dedupe_key)
        events.append(event)
    return tuple(events)


def parse_chat_chunk(
    path: Path,
    offset: int,
    *,
    product: str,
    project: str | None,
    fallback_timestamp: float,
    maximum_bytes: int,
) -> ChatChunk:
    """从偏移量继续读 JSONL。预算用尽时停在行首，下一轮重读未完成的行。"""

    if offset < 0:
        raise ValueError("offset 不能小于 0")
    if maximum_bytes <= 0:
        raise ValueError("maximum_bytes 必须大于 0")
    events: list[CountedUsage] = []
    bytes_read = 0
    reached_eof = True
    line_buffer = b""
    discarding = False
    cursor = offset
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            while True:
                if bytes_read >= maximum_bytes:
                    reached_eof = False
                    break
                chunk = handle.read(64 * 1024)
                if not chunk:
                    reached_eof = True
                    break
                bytes_read += len(chunk)
                line_buffer += chunk
                while True:
                    newline = line_buffer.find(b"\n")
                    if newline < 0:
                        break
                    raw_line = line_buffer[:newline]
                    line_start = cursor
                    cursor += newline + 1
                    line_buffer = line_buffer[newline + 1 :]
                    if discarding:
                        discarding = False
                        continue
                    event = _event_from_line(
                        raw_line,
                        product=product,
                        project=project,
                        fallback_timestamp=fallback_timestamp,
                        line_key=f"line:{line_start}",
                    )
                    if event is not None:
                        events.append(event)
                if len(line_buffer) > _MAX_LINE_BYTES:
                    cursor += len(line_buffer)
                    line_buffer = b""
                    discarding = True
    except OSError:
        return ChatChunk(offset, (), project, 0, False)
    next_offset = offset + bytes_read
    # 短的半行退回行首，下一轮整行重读。半行已经不短于本轮预算时不能退回，
    # 否则下一轮仍读不完，偏移量会停在原地。
    if line_buffer and not discarding and len(line_buffer) < maximum_bytes:
        next_offset -= len(line_buffer)
    return ChatChunk(
        next_offset=next_offset,
        events=tuple(events),
        project=project,
        bytes_read=bytes_read,
        reached_eof=reached_eof,
    )


def parse_chat_blob(
    path: Path,
    *,
    product: str,
    project: str | None,
    fallback_timestamp: float,
) -> tuple[CountedUsage, ...] | None:
    """读取不超过 32MiB 的旧式 JSON 聊天。过大或损坏时返回空，打不开返回 None。"""

    try:
        size = path.stat().st_size
        if size > _BLOB_LIMIT:
            return ()
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        if not path.is_file():
            return None
        return ()
    records = _blob_records(payload)
    events: list[CountedUsage] = []
    for index, record in enumerate(records):
        event = _event_from_record(
            record,
            product=product,
            project=project,
            fallback_timestamp=fallback_timestamp,
            line_key=f"blob:{index}",
        )
        if event is not None:
            events.append(event)
    return tuple(events)


def chat_project(home: Path, path: Path, *, product: str) -> str | None:
    """项目来自目录 slug、projects.json 或文件头，不来自消息正文。"""

    if product == "cursor":
        return _cursor_slug(home / "projects", path)
    folder = _chat_folder_id(home, path)
    if folder is not None:
        mapped = _projects_json(home).get(folder)
        if mapped:
            return mapped
    return _header_project(path)


def cursor_session_id(path: Path) -> str:
    """转录会话 ID：父目录不是 agent-transcripts 时用父目录名。"""

    if path.parent.name == "agent-transcripts":
        return path.stem
    return path.parent.name or path.stem


_AIDER_START = re.compile(
    r"^# aider chat started at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*$"
)
_AIDER_MODEL = re.compile(r"^> (?:Main model|Model): (\S+)")
_AIDER_TOKEN_LINE = re.compile(r"^> Tokens: (.+)$")
_AIDER_COUNT = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?\s+"
    r"(sent|cache write|cache hit|received)\b"
)
_CHAT_FILE_SUFFIXES = frozenset({".json", ".jsonl"})
_ACTIVE_PRODUCTS = frozenset({"cursor", "gemini", "qwen", "aider"})
_START_SLACK_SECONDS = 5.0


def parse_aider_chunk(
    path: Path,
    offset: int,
    *,
    project: str | None,
    fallback_timestamp: float,
    maximum_bytes: int,
    model: str = "",
    session_timestamp: float | None = None,
) -> AiderChunk:
    """继续读聊天历史。只解析 ``Tokens:`` 行，不把正文当成用量。"""

    if offset < 0:
        raise ValueError("offset 不能小于 0")
    if maximum_bytes <= 0:
        raise ValueError("maximum_bytes 必须大于 0")
    events: list[CountedUsage] = []
    bytes_read = 0
    reached_eof = True
    line_buffer = b""
    discarding = False
    cursor = offset
    current_model = model
    current_session = session_timestamp
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            while True:
                if bytes_read >= maximum_bytes:
                    reached_eof = False
                    break
                chunk = handle.read(64 * 1024)
                if not chunk:
                    reached_eof = True
                    break
                bytes_read += len(chunk)
                line_buffer += chunk
                while True:
                    newline = line_buffer.find(b"\n")
                    if newline < 0:
                        break
                    raw_line = line_buffer[:newline]
                    line_start = cursor
                    cursor += newline + 1
                    line_buffer = line_buffer[newline + 1 :]
                    if discarding:
                        discarding = False
                        continue
                    current_model, current_session, event = _aider_from_line(
                        raw_line,
                        project=project,
                        fallback_timestamp=fallback_timestamp,
                        line_key=f"aider:{line_start}",
                        model=current_model,
                        session_timestamp=current_session,
                    )
                    if event is not None:
                        events.append(event)
                if len(line_buffer) > _MAX_LINE_BYTES:
                    cursor += len(line_buffer)
                    line_buffer = b""
                    discarding = True
    except OSError:
        return AiderChunk(
            offset, (), 0, False, current_model, current_session
        )
    next_offset = offset + bytes_read
    # 半行短于本轮预算才退回行首。已经读满预算仍无换行时不能退回，否则偏移停住。
    if line_buffer and not discarding and len(line_buffer) < maximum_bytes:
        next_offset -= len(line_buffer)
    return AiderChunk(
        next_offset=next_offset,
        events=tuple(events),
        bytes_read=bytes_read,
        reached_eof=reached_eof,
        model=current_model,
        session_timestamp=current_session,
    )


def chat_session_file(product: str, home: Path, path: Path) -> Path | None:
    """打开的路径是该产品的会话文件时返回规范化路径，否则返回 None。"""

    if product not in _ACTIVE_PRODUCTS:
        return None
    normalized = _normalize(path)
    if product == "aider":
        if normalized.name == ".aider.chat.history.md":
            return normalized
        return None
    root = _normalize(home)
    if product == "cursor":
        projects = root / "projects"
        if projects not in normalized.parents:
            return None
        if normalized.suffix != ".jsonl":
            return None
        if "agent-transcripts" not in normalized.parts:
            return None
        return normalized
    try:
        relative = normalized.relative_to(root)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) != 4 or parts[2] != "chats":
        return None
    allowed = {"tmp"} if product == "gemini" else {"tmp", "projects"}
    if parts[0] not in allowed or normalized.suffix not in _CHAT_FILE_SUFFIXES:
        return None
    return normalized


def list_cursor_active_sessions(
    home: Path,
    proc_root: Path | None = None,
    now: float | None = None,
) -> tuple[Any, ...]:
    """列出当前 Cursor 进程对应的转录。"""

    return _list_active_chat_sessions("cursor", home, proc_root, now)


def list_gemini_active_sessions(
    home: Path,
    proc_root: Path | None = None,
    now: float | None = None,
) -> tuple[Any, ...]:
    """列出当前 Gemini CLI 进程对应的聊天记录。"""

    return _list_active_chat_sessions("gemini", home, proc_root, now)


def list_qwen_active_sessions(
    home: Path,
    proc_root: Path | None = None,
    now: float | None = None,
) -> tuple[Any, ...]:
    """列出当前 Qwen Code 进程对应的聊天记录。"""

    return _list_active_chat_sessions("qwen", home, proc_root, now)


def list_aider_active_sessions(
    home: Path,
    proc_root: Path | None = None,
    now: float | None = None,
) -> tuple[Any, ...]:
    """列出当前 Aider 进程对应的聊天历史。"""

    return _list_active_chat_sessions("aider", home, proc_root, now)


def _resolve(
    homes: Sequence[Path] | None,
    default_home: Any,
) -> tuple[Path, ...]:
    if homes is not None:
        unique: list[Path] = []
        seen: set[Path] = set()
        for home in homes:
            normalized = _normalize(Path(home))
            if normalized in seen:
                continue
            seen.add(normalized)
            unique.append(normalized)
        return tuple(unique)
    default = _normalize(default_home())
    return (default,) if default.is_dir() else ()


def _normalize(path: Path) -> Path:
    expanded = path.expanduser()
    try:
        return expanded.resolve()
    except (OSError, RuntimeError):
        return expanded.absolute()


def _sqlite_uri(path: Path) -> str:
    text = quote(str(path.resolve()), safe="/:")
    return f"file:{text}?mode=ro"


def _list_chats(home: Path, layouts: tuple[str, ...]) -> tuple[Path, ...]:
    found: list[Path] = []
    root = Path(home)
    parents: list[Path] = []
    if "tmp" in layouts:
        parents.append(root / "tmp")
    if "projects" in layouts:
        parents.append(root / "projects")
    for parent in parents:
        if not parent.is_dir():
            continue
        try:
            projects = [entry for entry in parent.iterdir() if entry.is_dir()]
        except OSError:
            continue
        for project in projects:
            chats = project / "chats"
            if not chats.is_dir():
                continue
            try:
                entries = list(chats.iterdir())
            except OSError:
                continue
            found.extend(
                entry
                for entry in entries
                if entry.is_file() and entry.suffix in {".json", ".jsonl"}
            )
    return tuple(sorted(found, key=str))


def _opencode_row(row: Sequence[Any]) -> CountedUsage | None:
    message_id = row[0] if isinstance(row[0], str) and row[0] else None
    timestamp = _epoch(row[1])
    if timestamp is None:
        return None
    model = row[2] if isinstance(row[2], str) and row[2].strip() else None
    if model is None and isinstance(row[3], str) and row[3].strip():
        model = row[3].strip()
    raw_input = _number(row[4])
    output = _number(row[5])
    reasoning = _number(row[6])
    cached = _number(row[7])
    cache_write = _number(row[8])
    # OpenCode 的 input 不含缓存，这里换成「input = 总输入」。
    total_input = raw_input + cached + cache_write
    total_output = output + reasoning
    total = total_input + total_output
    if total <= 0:
        return None
    project = row[9].strip() if isinstance(row[9], str) and row[9].strip() else None
    key = message_id or f"opencode:{timestamp}:{total}"
    return CountedUsage(
        timestamp=timestamp,
        model=(model or _UNKNOWN_MODEL)[:200],
        project=project[:1024] if project else None,
        input_tokens=total_input,
        cached_input_tokens=cached,
        cache_write_input_tokens=cache_write,
        output_tokens=total_output,
        reasoning_output_tokens=reasoning,
        total_tokens=total,
        dedupe_key=key,
    )


def _event_from_line(
    raw_line: bytes,
    *,
    product: str,
    project: str | None,
    fallback_timestamp: float,
    line_key: str,
) -> CountedUsage | None:
    if not raw_line.strip() or len(raw_line) > _MAX_LINE_BYTES:
        return None
    if product == "cursor":
        if b'"usage"' not in raw_line and b'"tokens"' not in raw_line:
            return None
    elif b'"tokens"' not in raw_line:
        return None
    try:
        record = json.loads(raw_line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    return _event_from_record(
        record,
        product=product,
        project=project,
        fallback_timestamp=fallback_timestamp,
        line_key=line_key,
    )


def _event_from_record(
    record: Mapping[str, Any],
    *,
    product: str,
    project: str | None,
    fallback_timestamp: float,
    line_key: str,
) -> CountedUsage | None:
    if product == "cursor":
        return _cursor_event(
            record,
            project=project,
            fallback_timestamp=fallback_timestamp,
            line_key=line_key,
        )
    return _gemini_event(
        record,
        project=project,
        line_key=line_key,
    )


def _cursor_event(
    record: Mapping[str, Any],
    *,
    project: str | None,
    fallback_timestamp: float,
    line_key: str,
) -> CountedUsage | None:
    usage = _usage_object(record)
    if usage is None:
        return None
    counted = _plain_usage(usage)
    if counted is None:
        return None
    input_tokens, cached, cache_write, output, reasoning, total = counted
    timestamp = _record_timestamp(record)
    if timestamp is None:
        timestamp = fallback_timestamp
    message_id = _text(record.get("id"))
    return CountedUsage(
        timestamp=timestamp,
        model=_model_name(record),
        project=project,
        input_tokens=input_tokens,
        cached_input_tokens=min(cached, input_tokens) if input_tokens else cached,
        cache_write_input_tokens=cache_write,
        output_tokens=output,
        reasoning_output_tokens=reasoning,
        total_tokens=total,
        dedupe_key=message_id or line_key,
    )


def _gemini_event(
    record: Mapping[str, Any],
    *,
    project: str | None,
    line_key: str,
) -> CountedUsage | None:
    kind = str(record.get("type") or "")
    if kind not in _GEMINI_TYPES:
        return None
    tokens = record.get("tokens")
    if not isinstance(tokens, Mapping):
        tokens = record.get("tokensSummary")
    if not isinstance(tokens, Mapping):
        return None
    counted = _gemini_tokens(tokens)
    if counted is None:
        return None
    timestamp = _record_timestamp(record)
    if timestamp is None:
        return None
    input_tokens, cached, output, thoughts, total = counted
    message_id = _text(record.get("id")) or _text(record.get("messageId"))
    return CountedUsage(
        timestamp=timestamp,
        model=_model_name(record),
        project=project,
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        cache_write_input_tokens=0,
        output_tokens=output,
        reasoning_output_tokens=thoughts,
        total_tokens=total,
        dedupe_key=message_id or line_key,
    )


def _gemini_tokens(
    tokens: Mapping[str, Any],
) -> tuple[int, int, int, int, int] | None:
    recognized = ("input", "output", "cached", "thoughts", "tool", "total")
    if not any(key in tokens for key in recognized):
        return None
    raw_input = _number(tokens.get("input"))
    output = _number(tokens.get("output"))
    cached = _number(tokens.get("cached"))
    thoughts = _number(tokens.get("thoughts"))
    tool = _number(tokens.get("tool"))
    recorded_total = tokens.get("total")
    has_total = _number_or_none(recorded_total) is not None
    total_value = _number(recorded_total)
    produced = output + thoughts
    if has_total and total_value > produced:
        input_tokens = max(raw_input, total_value - produced)
    else:
        input_tokens = raw_input + tool
    output_tokens = produced
    cached_input = min(cached, input_tokens)
    total = input_tokens + output_tokens
    if total <= 0:
        return None
    return input_tokens, cached_input, output_tokens, thoughts, total


def _plain_usage(
    usage: Mapping[str, Any],
) -> tuple[int, int, int, int, int, int] | None:
    present = (
        _number_or_none(_first(usage, _INPUT_KEYS)) is not None
        or _number_or_none(_first(usage, _OUTPUT_KEYS)) is not None
        or _number_or_none(_first(usage, _CACHED_KEYS)) is not None
        or _number_or_none(_first(usage, _WRITE_KEYS)) is not None
        or _number_or_none(_first(usage, _TOTAL_KEYS)) is not None
    )
    if not present:
        return None
    input_tokens = _number(_first(usage, _INPUT_KEYS))
    output = _number(_first(usage, _OUTPUT_KEYS))
    cached = _number(_first(usage, _CACHED_KEYS))
    cache_write = _number(_first(usage, _WRITE_KEYS))
    reasoning = _number(_first(usage, _REASON_KEYS))
    recorded = _number_or_none(_first(usage, _TOTAL_KEYS))
    total = recorded if recorded is not None else input_tokens + output
    if input_tokens == 0 and output == 0 and total == 0:
        return None
    return input_tokens, cached, cache_write, output, reasoning, total


def _usage_object(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    usage = record.get("usage")
    if isinstance(usage, Mapping):
        return usage
    tokens = record.get("tokens")
    if isinstance(tokens, Mapping):
        return tokens
    message = record.get("message")
    if isinstance(message, Mapping):
        nested = message.get("usage")
        if isinstance(nested, Mapping):
            return nested
        nested_tokens = message.get("tokens")
        if isinstance(nested_tokens, Mapping):
            return nested_tokens
    return None


def _blob_records(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    return [item for item in messages if isinstance(item, Mapping)]


def _model_name(record: Mapping[str, Any]) -> str:
    for key in ("model", "modelID", "modelId"):
        value = record.get(key)
        if isinstance(value, str) and value.strip() and not value.startswith("<"):
            return value.strip()[:200]
    model = record.get("model")
    if isinstance(model, Mapping):
        for key in ("modelID", "id", "name"):
            value = model.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
    return _UNKNOWN_MODEL


def _record_timestamp(record: Mapping[str, Any]) -> float | None:
    for key in ("timestamp", "startTime", "createdAt", "time"):
        parsed = _timestamp_value(record.get(key))
        if parsed is not None:
            return parsed
    message = record.get("message")
    if isinstance(message, Mapping):
        for key in ("timestamp", "createdAt", "time"):
            parsed = _timestamp_value(message.get(key))
            if parsed is not None:
                return parsed
    return None


def _timestamp_value(value: Any) -> float | None:
    epoch = _epoch(value)
    if epoch is not None:
        return epoch
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _epoch(value: Any) -> float | None:
    number = _number_or_none(value)
    if number is None or number <= 0:
        return None
    if number > 10_000_000_000:
        number /= 1000.0
    return float(number)


def _header_project(path: Path) -> str | None:
    try:
        if path.suffix == ".json":
            if path.stat().st_size > _BLOB_LIMIT:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                for key in _HEADER_KEYS:
                    value = payload.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()[:1024]
            return None
        with path.open("rb") as handle:
            raw = handle.read(65536)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    for line in raw.split(b"\n")[:8]:
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        for key in _HEADER_KEYS:
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:1024]
    return None


def _projects_json(home: Path) -> dict[str, str]:
    path = Path(home) / "projects.json"
    try:
        if path.stat().st_size > _PROJECTS_JSON_LIMIT:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return _project_map(payload)


def _project_map(payload: Any) -> dict[str, str]:
    found: dict[str, str] = {}

    def add(key: object, path: object) -> None:
        if isinstance(key, str) and isinstance(path, str) and path.strip():
            found.setdefault(key, path.strip()[:1024])

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            nested = node.get("projects")
            if isinstance(nested, (dict, list)):
                walk(nested)
            identifier = node.get("id") or node.get("projectId")
            path = (
                node.get("path") or node.get("directory") or node.get("cwd")
            )
            add(identifier, path)
            for key, value in node.items():
                if key == "projects":
                    continue
                if isinstance(value, str):
                    add(key, value)
                elif isinstance(value, dict):
                    inner = (
                        value.get("path")
                        or value.get("directory")
                        or value.get("cwd")
                    )
                    add(value.get("id") or key, inner)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def _chat_folder_id(home: Path, path: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(Path(home).resolve())
    except (OSError, ValueError):
        return None
    parts = relative.parts
    if len(parts) >= 3 and parts[0] in {"tmp", "projects"} and parts[2] == "chats":
        return parts[1]
    return None


def _cursor_slug(projects_root: Path, path: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(projects_root.resolve())
    except (OSError, ValueError):
        return None
    if not relative.parts:
        return None
    return relative.parts[0]


def _first(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _number(value: Any) -> int:
    parsed = _number_or_none(value)
    return 0 if parsed is None else parsed


def _number_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _aider_from_line(
    raw_line: bytes,
    *,
    project: str | None,
    fallback_timestamp: float,
    line_key: str,
    model: str,
    session_timestamp: float | None,
) -> tuple[str, float | None, CountedUsage | None]:
    if not raw_line.strip() or len(raw_line) > _MAX_LINE_BYTES:
        return model, session_timestamp, None
    text = raw_line.decode("utf-8", errors="replace").strip()
    started = _AIDER_START.match(text)
    if started is not None:
        parsed = _aider_clock(started.group(1))
        if parsed is not None:
            session_timestamp = parsed
        return model, session_timestamp, None
    named = _AIDER_MODEL.match(text)
    if named is not None:
        return named.group(1)[:200], session_timestamp, None
    token_line = _AIDER_TOKEN_LINE.match(text)
    if token_line is None:
        return model, session_timestamp, None
    counts = _aider_counts(token_line.group(1))
    if counts is None:
        return model, session_timestamp, None
    sent, cache_write, cache_hit, received = counts
    usage = _aider_usage(sent, cache_write, cache_hit, received)
    if usage is None:
        return model, session_timestamp, None
    input_tokens, cached, write, output, total = usage
    project_text = project[:1024] if project else None
    return model, session_timestamp, CountedUsage(
        timestamp=(
            session_timestamp
            if session_timestamp is not None
            else fallback_timestamp
        ),
        model=(model or _UNKNOWN_MODEL)[:200],
        project=project_text,
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        cache_write_input_tokens=write,
        output_tokens=output,
        reasoning_output_tokens=0,
        total_tokens=total,
        dedupe_key=line_key,
    )


def _aider_clock(text: str) -> float | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def _aider_counts(body: str) -> tuple[int, int, int, int] | None:
    found = {"sent": 0, "cache write": 0, "cache hit": 0, "received": 0}
    seen = False
    for match in _AIDER_COUNT.finditer(body):
        seen = True
        found[match.group(3)] = _aider_number(match.group(1), match.group(2))
    if not seen:
        return None
    return found["sent"], found["cache write"], found["cache hit"], found["received"]


def _aider_number(raw: str, suffix: str | None) -> int:
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return 0
    if suffix is None:
        return max(0, int(value))
    scale = 1_000 if suffix.lower() == "k" else 1_000_000
    return max(0, int(round(value * scale)))


def _aider_usage(
    sent: int,
    cache_write: int,
    cache_hit: int,
    received: int,
) -> tuple[int, int, int, int, int] | None:
    """按 Aider 两条计费路径还原总输入。

    有 cache write，或 cache hit 已经大于 sent 时，hit 不在 sent 里。
    否则 hit 是 sent 的子集（DeepSeek 这条路径）。
    """

    if cache_write > 0 or cache_hit > sent:
        input_tokens = sent + cache_hit
        cached = cache_hit
        write = min(cache_write, sent)
    else:
        input_tokens = sent
        cached = min(cache_hit, sent)
        write = 0
    output = received
    total = input_tokens + output
    if total <= 0:
        return None
    if cached > input_tokens:
        cached = input_tokens
    room = input_tokens - cached
    if write > room:
        write = room
    return input_tokens, cached, write, output, total


def _list_active_chat_sessions(
    product: str,
    home: Path,
    proc_root: Path | None,
    now: float | None,
) -> tuple[Any, ...]:
    from .agents import scan_running_agents
    from .multi_models import DetectionConfidence, SessionStatus, TrackedSession

    root = _normalize(home)
    observed_at = time.time() if now is None else float(now)
    if product == "cursor":
        session_roots = (root / "projects",)
    else:
        session_roots = (root,)
    agents = scan_running_agents(
        proc_root=proc_root,
        products=(product,),
        session_roots=session_roots,
    )
    grouped: dict[str, dict[str, Any]] = {}

    def add(path: Path, pid: int, opened: bool) -> None:
        key = str(path)
        slot = grouped.get(key)
        if slot is None:
            grouped[key] = {"path": path, "pids": [pid], "opened": opened}
            return
        if pid not in slot["pids"]:
            slot["pids"].append(pid)
        if opened:
            slot["opened"] = True

    for agent in agents:
        opened_one = False
        for path in agent.open_paths:
            session_file = chat_session_file(product, root, path)
            if session_file is None:
                continue
            add(session_file, agent.pid, True)
            opened_one = True
        if opened_one or agent.cwd is None:
            continue
        started = _process_started_at(agent.start_token, proc_root)
        fallback = _fallback_session_file(product, root, agent.cwd, started)
        if fallback is not None:
            add(fallback, agent.pid, False)

    sessions = []
    for slot in grouped.values():
        path = slot["path"]
        session_id = (
            cursor_session_id(path) if product == "cursor" else path.stem
        )
        if product == "aider":
            session_id = path.parent.name or path.stem
        project = (
            str(path.parent)
            if product == "aider"
            else chat_project(root, path, product=product)
        )
        sessions.append(
            TrackedSession(
                thread_id=f"{product}:{session_id}",
                session_id=session_id,
                jsonl_path=str(path),
                cwd=project,
                source=f"{product}-cli",
                status=SessionStatus.RUNNING,
                confidence=(
                    DetectionConfidence.OPEN_FILE
                    if slot["opened"]
                    else DetectionConfidence.RECENT_FILE
                ),
                first_seen_at=observed_at,
                last_seen_at=observed_at,
                pids=tuple(sorted(slot["pids"])),
                last_event_type="session",
                product=product,
                project=project,
            )
        )
    sessions.sort(key=lambda item: item.session_id)
    return tuple(sessions)


def _fallback_session_file(
    product: str,
    home: Path,
    cwd: Path,
    started_at: float | None,
) -> Path | None:
    if product == "aider":
        candidate = Path(cwd) / ".aider.chat.history.md"
        return candidate if _fresh_enough(candidate, started_at) else None
    candidates = _cwd_session_files(product, home, cwd)
    best: Path | None = None
    best_mtime = -1.0
    for path in candidates:
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if started_at is not None and modified < started_at - _START_SLACK_SECONDS:
            continue
        if modified >= best_mtime:
            best = path
            best_mtime = modified
    return best


def _fresh_enough(path: Path, started_at: float | None) -> bool:
    try:
        if not path.is_file():
            return False
        modified = path.stat().st_mtime
    except OSError:
        return False
    if started_at is None:
        return True
    return modified >= started_at - _START_SLACK_SECONDS


def _cwd_session_files(product: str, home: Path, cwd: Path) -> tuple[Path, ...]:
    if product == "cursor":
        projects = home / "projects"
        if not projects.is_dir():
            return ()
        slugs = _path_slugs(str(cwd))
        found: list[Path] = []
        try:
            entries = [entry for entry in projects.iterdir() if entry.is_dir()]
        except OSError:
            return ()
        for entry in entries:
            if entry.name not in slugs:
                continue
            try:
                found.extend(
                    path
                    for path in (entry / "agent-transcripts").rglob("*.jsonl")
                    if path.is_file()
                )
            except OSError:
                continue
        return tuple(found)
    files = (
        list_gemini_chats(home) if product == "gemini" else list_qwen_chats(home)
    )
    return tuple(
        path
        for path in files
        if _project_matches_cwd(chat_project(home, path, product=product), cwd)
    )


def _path_slugs(cwd: str) -> set[str]:
    dashed = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    slash = cwd.replace("/", "-").replace("\\", "-")
    return {
        item
        for item in (dashed, dashed.lstrip("-"), slash, slash.lstrip("-"))
        if item
    }


def _project_matches_cwd(project: str | None, cwd: Path) -> bool:
    if not project:
        return False
    try:
        project_path = Path(project).expanduser().resolve()
        cwd_path = Path(cwd).expanduser().resolve()
    except OSError:
        return False
    if len(project_path.parts) < 2:
        return False
    return project_path == cwd_path or project_path in cwd_path.parents


def _process_started_at(start_token: str, proc_root: Path | None) -> float | None:
    if not start_token.isdigit():
        return None
    root = proc_root if proc_root is not None else Path("/proc")
    try:
        text = (root / "stat").read_text(encoding="utf-8", errors="replace")
        ticks = float(os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError):
        return None
    match = re.search(r"\bbtime\s+(\d+)", text)
    if match is None or ticks <= 0:
        return None
    return int(match.group(1)) + int(start_token) / ticks
