"""DeepSeek Harness：会话定位与（可能 zstd 压缩的）转写记录提取。"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from functools import partial
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .common import (
    _Candidate,
    _DecodeError,
    _Extraction,
    _MTIME_SLACK_SECONDS,
    _ToolCall,
    _UNREADABLE,
    _as_text,
    _content_text,
    _cwd_matches,
    _epoch_seconds,
    _extraction_from_scan,
    _mapping_lines,
    _output_item,
    _scan_parsed,
    _tool_item,
    _user_item,
)


_DSH_TRANSCRIPTS = (
    "session.v4.jsonl.zstd",
    "session.v3.jsonl.zstd",
    "session.v2.jsonl.zstd",
    "session.v4.jsonl",
    "session.v3.jsonl",
    "session.v2.jsonl",
    "session.jsonl",
)


def _dsh_transcript(session_dir: Path) -> Path | None:
    """同一会话目录里取版本最高的那份记录。"""

    for name in _DSH_TRANSCRIPTS:
        path = session_dir / name
        if path.is_file():
            return path
    return None


def _zstd_lines(path: Path) -> Iterable[str]:
    """解开 zstd JSONL。标准库、可选第三方库、本机 zstd 命令依次尝试。"""

    try:
        zstd_mod = importlib.import_module("compression.zstd")
    except ImportError:
        zstd_mod = None
    if zstd_mod is not None:
        try:
            with path.open("rb") as raw, zstd_mod.ZstdFile(raw) as decoded:
                for line in decoded:
                    yield line.decode("utf-8", errors="replace")
            return
        except (OSError, AttributeError) as error:
            raise _DecodeError(str(error)) from error
    for module_name in ("zstandard", "backports.zstd"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        try:
            with path.open("rb") as raw:
                if module_name == "zstandard":
                    reader = module.ZstdDecompressor().stream_reader(raw)
                else:
                    reader = module.open(raw, "rb")
                try:
                    for line in reader:
                        yield line.decode("utf-8", errors="replace")
                finally:
                    reader.close()
            return
        except (OSError, AttributeError) as error:
            raise _DecodeError(str(error)) from error
    if shutil.which("zstd") is None:
        raise _DecodeError("没有可用的 zstd 解码器")
    process = subprocess.Popen(
        ["zstd", "-dc", "--", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    stdout = process.stdout
    if stdout is None:
        process.kill()
        process.wait()
        raise _DecodeError("zstd 没有输出")
    try:
        for line in stdout:
            yield line.decode("utf-8", errors="replace")
    finally:
        stdout.close()
        process.kill()
        process.wait()


def _dsh_lines(path: Path) -> Iterable[str]:
    """逐行读出会话。压缩帧不能按偏移截断，读完或关闭生成器即停止。"""

    if path.name.endswith(".zstd"):
        yield from _zstd_lines(path)
        return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            yield from handle
    except OSError as error:
        raise _DecodeError(str(error)) from error


def _dsh_records(path: Path) -> Iterable[Mapping[str, Any]]:
    for line in _dsh_lines(path):
        text = line.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(record, Mapping):
            yield record


def _dsh_first_record(path: Path) -> Mapping[str, Any] | None | object:
    """只读第一条记录拿 cwd；解不开时返回哨兵，调用方仍保留这个候选。"""

    lines = _dsh_lines(path)
    try:
        for record in _mapping_lines(lines):
            return record
        return None
    except _DecodeError:
        return _UNREADABLE
    finally:
        close = getattr(lines, "close", None)
        if close is not None:
            close()


def _dsh_cwd(record: Mapping[str, Any]) -> str | None:
    data = record.get("data")
    if isinstance(data, Mapping):
        cwd = data.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    cwd = record.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def _dsh_record_ts(record: Mapping[str, Any]) -> float | None:
    return _epoch_seconds(record.get("time"))


def _dsh_candidates(
    roots: Sequence[Path],
    cwd: str | None,
    start: float,
) -> list[_Candidate]:
    """会话目录名不可逆，cwd 在第一条 session 记录上。"""

    out: list[_Candidate] = []
    cutoff = start - _MTIME_SLACK_SECONDS
    for root in roots:
        if not root.is_dir():
            continue
        try:
            projects = [entry for entry in root.iterdir() if entry.is_dir()]
        except OSError:
            continue
        for project in projects:
            try:
                sessions = [entry for entry in project.iterdir() if entry.is_dir()]
            except OSError:
                continue
            for session_dir in sessions:
                path = _dsh_transcript(session_dir)
                if path is None:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime < cutoff:
                    continue
                header = _dsh_first_record(path)
                if header is _UNREADABLE:
                    out.append(_Candidate(path, stat.st_mtime, stat.st_size))
                    continue
                session_cwd = _dsh_cwd(header) if isinstance(header, Mapping) else None
                if cwd and not (session_cwd and _cwd_matches(cwd, session_cwd)):
                    continue
                out.append(_Candidate(path, stat.st_mtime, stat.st_size))
    return out


def _dsh_map(
    record: Mapping[str, Any],
    timestamp: float,
    *,
    tool_calls: dict[str, _ToolCall] | None = None,
) -> list[tuple[dict[str, Any], int, int]]:
    kind = record.get("type")
    data = record.get("data")
    if not isinstance(data, Mapping):
        return []
    if kind == "user/message":
        source = data.get("source")
        if not isinstance(source, Mapping) or source.get("kind") != "user":
            return []
        item = _user_item(_content_text(data.get("content")), timestamp)
        return [item] if item is not None else []
    if kind == "tool/call":
        return [
            _tool_item(
                str(data.get("name") or ""),
                data.get("arguments"),
                timestamp,
                data.get("callId"),
                tool_calls,
            )
        ]
    if kind != "tool/result":
        return []
    call_id = data.get("callId")
    message = data.get("message")
    content: object = data.get("content")
    if isinstance(message, Mapping):
        source = message.get("source")
        if isinstance(source, Mapping) and source.get("callId"):
            call_id = source.get("callId")
        content = message.get("content")
    item = _output_item(_content_text(content) or _as_text(content), timestamp, call_id, tool_calls)
    return [item] if item is not None else []


def _extract_dsh(path: Path, start: float, end: float) -> _Extraction:
    tool_calls: dict[str, _ToolCall] = {}
    try:
        return _extraction_from_scan(
            _scan_parsed(
                _dsh_records(path),
                parse_ts=_dsh_record_ts,
                map_record=partial(_dsh_map, tool_calls=tool_calls),
                start=start,
                end=end,
            )
        )
    except _DecodeError:
        return _Extraction(None, False, 0, 0)
