"""按文件类型有界增量读取各家会话日志（``UsageAggregator`` 的读取部分）。"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..commandcode import (
    commandcode_home_for,
)
from ..grok import (
    GrokSessionInfo,
    grok_unified_log,
    load_session_index,
    parse_grok_log_chunk,
)
from ..dsh import (
    dsh_projcache_home,
    parse_dsh_projcache,
)
from ..claude import (
    claude_home_for,
    parse_claude_chunk,
)
from ..kimi import (
    KimiSessionInfo,
    load_kimi_session_index,
    parse_kimi_wire_chunk,
)
from ..local_agents import (
    chat_project,
    opencode_db_path,
    parse_aider_chunk,
    parse_chat_blob,
    parse_chat_chunk,
    read_opencode_usage,
)
from .parsing import (
    _delta_from_counted,
    _merge_counted,
    _owning_home,
    _parse_usage_chunk,
    _path_under,
    _single_project,
    _token_usage_delta,
)
from .records import (
    TokenUsage,
    UsageDelta,
    _CachedFile,
    _UsageParseState,
)


class _FileReaderMixin:
    """``UsageAggregator`` 的混入类；依赖其 ``__init__`` 建立的实例属性。"""

    def _read_file(
        self,
        path: Path,
        maximum_bytes: int,
    ) -> _CachedFile | None:
        """按偏移量读取追加内容；只有替换或截断时才重新解析。"""

        if maximum_bytes <= 0:
            raise ValueError("maximum_bytes 必须大于 0")

        grok_home = self._grok_home_for_log(path)
        if grok_home is not None:
            return self._read_grok_log(path, grok_home, maximum_bytes)

        kimi_home = self._kimi_home_for_wire(path)
        if kimi_home is not None:
            return self._read_kimi_wire(path, kimi_home, maximum_bytes)

        dsh_home = dsh_projcache_home(path, self._dsh_homes)
        if dsh_home is not None:
            return self._read_dsh_projcache(path, maximum_bytes)

        if claude_home_for(path, self._claude_homes) is not None:
            return self._read_claude_transcript(path, maximum_bytes)

        if self._opencode_home_for(path) is not None:
            return self._read_opencode_db(path, maximum_bytes)

        if self._aider_home_for(path) is not None:
            return self._read_aider_history(path, maximum_bytes)

        chat_product = self._chat_product_for(path)
        if chat_product is not None:
            return self._read_chat_file(path, maximum_bytes, chat_product)

        try:
            stat_result = path.stat()
        except OSError:
            return None
        signature = (
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )
        cached = self._cache.get(path)
        if cached is None and self._persistent is not None:
            cached = self._persistent.load(path)
            if cached is not None:
                self._cache[path] = cached
        if cached is not None and cached.signature == signature and cached.complete:
            unchanged = replace(cached, last_read_bytes=0)
            self._cache[path] = unchanged
            return unchanged

        can_append = (
            cached is not None
            and cached.signature[0] == signature[0]
            and (not cached.complete or stat_result.st_size > cached.signature[2])
            and stat_result.st_size >= cached.next_offset
        )
        if can_append and cached is not None:
            parsed = _parse_usage_chunk(
                path,
                offset=cached.next_offset,
                state=cached.state,
                maximum_bytes=maximum_bytes,
                commandcode=commandcode_home_for(path, self._commandcode_homes)
                is not None,
            )
            cached_file = _CachedFile(
                signature=signature,
                next_offset=parsed.next_offset,
                total_deltas=cached.total_deltas + parsed.total_deltas,
                fallback_deltas=(cached.fallback_deltas + parsed.fallback_deltas),
                state=parsed.state,
                last_read_bytes=parsed.bytes_read,
                complete=parsed.reached_eof,
            )
            replace_deltas = False
        else:
            parsed = _parse_usage_chunk(
                path,
                offset=0,
                state=_UsageParseState(
                    previous_timestamp=stat_result.st_mtime,
                ),
                maximum_bytes=maximum_bytes,
                commandcode=commandcode_home_for(path, self._commandcode_homes)
                is not None,
            )
            cached_file = _CachedFile(
                signature=signature,
                next_offset=parsed.next_offset,
                total_deltas=parsed.total_deltas,
                fallback_deltas=parsed.fallback_deltas,
                state=parsed.state,
                last_read_bytes=parsed.bytes_read,
                complete=parsed.reached_eof,
            )
            replace_deltas = True
        self._cache[path] = cached_file
        if self._persistent is not None:
            self._persistent.save(
                path,
                cached_file,
                parsed.total_deltas,
                parsed.fallback_deltas,
                replace_deltas=replace_deltas,
            )
        return cached_file

    def _cached_paths(
        self,
        key: Path,
        loader: Any,
    ) -> tuple[Path, ...]:
        """按发现间隔缓存一次目录列举。key 只做缓存身份，不是扫描根。"""

        current_time = time.time()
        previous_time = self._discovered_at.get(key, 0)
        if current_time - previous_time < self.discovery_interval:
            return self._discovered.get(key, ())
        try:
            paths = tuple(loader())
        except OSError:
            paths = ()
        self._discovered[key] = paths
        self._discovered_at[key] = current_time
        return paths

    def _aider_home_for(self, path: Path) -> Path | None:
        for home in self._aider_homes:
            if path == home / ".aider.chat.history.md":
                return home
        return None

    def _read_aider_history(
        self,
        path: Path,
        maximum_bytes: int,
    ) -> _CachedFile | None:
        """签名变化后从上次偏移继续。模型名和会话起点随偏移一起记住。"""

        try:
            stat_result = path.stat()
        except OSError:
            return None
        signature = (
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )
        cached = self._load_cached(path)
        if cached is not None and cached.signature == signature and cached.complete:
            unchanged = replace(cached, last_read_bytes=0)
            self._cache[path] = unchanged
            return unchanged
        home = self._aider_home_for(path)
        project = str(home) if home is not None else None
        partial = self._chat_partial.get(path)
        if (
            partial is not None
            and partial[0] == signature[0]
            and stat_result.st_size >= partial[1]
        ):
            offset = partial[1]
            order = partial[2]
            current = dict(partial[3])
            model = partial[4] if len(partial) > 4 else ""
            session_timestamp = partial[5] if len(partial) > 5 else None
        else:
            offset = 0
            order = ()
            current = {}
            model = ""
            session_timestamp = None
        parsed = parse_aider_chunk(
            path,
            offset,
            project=project,
            fallback_timestamp=stat_result.st_mtime,
            maximum_bytes=maximum_bytes,
            model=model if isinstance(model, str) else "",
            session_timestamp=(
                session_timestamp
                if isinstance(session_timestamp, float)
                else None
            ),
        )
        order, current = _merge_counted(
            order,
            current,
            parsed.events,
            replace=False,
        )
        self._chat_partial[path] = (
            signature[0],
            parsed.next_offset,
            order,
            current,
            parsed.model,
            parsed.session_timestamp,
        )
        deltas = tuple(current[key] for key in order)
        cached_file = _CachedFile(
            signature=signature,
            next_offset=parsed.next_offset,
            total_deltas=deltas,
            fallback_deltas=(),
            state=_UsageParseState(
                has_total_usage=True,
                project=project or _single_project(deltas),
                previous_timestamp=stat_result.st_mtime,
            ),
            last_read_bytes=parsed.bytes_read,
            complete=parsed.reached_eof,
        )
        self._store_replaced(path, cached_file, deltas)
        return cached_file

    def _opencode_home_for(self, path: Path) -> Path | None:
        for home in self._opencode_homes:
            if path == opencode_db_path(home):
                return home
        return None

    def _chat_product_for(self, path: Path) -> str | None:
        if _path_under(path, self._cursor_homes) and "agent-transcripts" in path.parts:
            return "cursor"
        if _path_under(path, self._gemini_homes):
            return "gemini"
        if _path_under(path, self._qwen_homes):
            return "qwen"
        return None

    def _read_opencode_db(
        self,
        path: Path,
        maximum_bytes: int,
    ) -> _CachedFile | None:
        """签名变化时整库重读。SQL 不能按偏移追加，字节数按文件大小计入预算。"""

        del maximum_bytes
        try:
            stat_result = path.stat()
        except OSError:
            return None
        signature = (
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )
        cached = self._load_cached(path)
        if cached is not None and cached.signature == signature and cached.complete:
            unchanged = replace(cached, last_read_bytes=0)
            self._cache[path] = unchanged
            return unchanged
        events = read_opencode_usage(path)
        if events is None:
            return None
        deltas = tuple(_delta_from_counted(event) for event in events)
        cached_file = _CachedFile(
            signature=signature,
            next_offset=stat_result.st_size,
            total_deltas=deltas,
            fallback_deltas=(),
            state=_UsageParseState(
                has_total_usage=True,
                project=_single_project(deltas),
                previous_timestamp=stat_result.st_mtime,
            ),
            last_read_bytes=stat_result.st_size,
            complete=True,
        )
        self._store_replaced(path, cached_file, deltas)
        return cached_file

    def _read_chat_file(
        self,
        path: Path,
        maximum_bytes: int,
        product: str,
    ) -> _CachedFile | None:
        """Gemini / Qwen 按消息 ID 后者覆盖前者；Cursor 只累计带 usage 的行。"""

        try:
            stat_result = path.stat()
        except OSError:
            return None
        signature = (
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )
        cached = self._load_cached(path)
        if cached is not None and cached.signature == signature and cached.complete:
            unchanged = replace(cached, last_read_bytes=0)
            self._cache[path] = unchanged
            return unchanged
        home = _owning_home(
            path,
            self._cursor_homes
            if product == "cursor"
            else self._gemini_homes
            if product == "gemini"
            else self._qwen_homes,
        )
        project = (
            chat_project(home, path, product=product) if home is not None else None
        )
        if path.suffix == ".json":
            return self._read_chat_blob(
                path,
                signature,
                stat_result.st_size,
                stat_result.st_mtime,
                product,
                project,
            )
        partial = self._chat_partial.get(path)
        if (
            partial is not None
            and partial[0] == signature[0]
            and stat_result.st_size >= partial[1]
        ):
            offset = partial[1]
            order: tuple[str, ...] = partial[2]
            current = dict(partial[3])
        else:
            # 内存里没有 ID 映射时从头读，避免只把尾部写回索引、丢掉前半段。
            offset = 0
            order = ()
            current = {}
        parsed = parse_chat_chunk(
            path,
            offset,
            product="cursor" if product == "cursor" else "gemini",
            project=project,
            fallback_timestamp=stat_result.st_mtime,
            maximum_bytes=maximum_bytes,
        )
        order, current = _merge_counted(
            order,
            current,
            parsed.events,
            replace=product != "cursor",
        )
        self._chat_partial[path] = (signature[0], parsed.next_offset, order, current)
        deltas = tuple(current[key] for key in order)
        cached_file = _CachedFile(
            signature=signature,
            next_offset=parsed.next_offset,
            total_deltas=deltas,
            fallback_deltas=(),
            state=_UsageParseState(
                has_total_usage=True,
                project=project or _single_project(deltas),
                previous_timestamp=stat_result.st_mtime,
            ),
            last_read_bytes=parsed.bytes_read,
            complete=parsed.reached_eof,
        )
        self._store_replaced(path, cached_file, deltas)
        return cached_file

    def _read_chat_blob(
        self,
        path: Path,
        signature: tuple[int, int, int],
        size: int,
        modified_at: float,
        product: str,
        project: str | None,
    ) -> _CachedFile:
        if size > 32 * 1024 * 1024:
            cached_file = _CachedFile(
                signature=signature,
                next_offset=size,
                total_deltas=(),
                fallback_deltas=(),
                state=_UsageParseState(has_total_usage=True, project=project),
                last_read_bytes=0,
                complete=True,
            )
            self._store_replaced(path, cached_file, ())
            return cached_file
        events = parse_chat_blob(
            path,
            product="cursor" if product == "cursor" else "gemini",
            project=project,
            fallback_timestamp=modified_at,
        )
        if events is None:
            events = ()
        order, current = _merge_counted(
            (),
            {},
            events,
            replace=product != "cursor",
        )
        deltas = tuple(current[key] for key in order)
        cached_file = _CachedFile(
            signature=signature,
            next_offset=size,
            total_deltas=deltas,
            fallback_deltas=(),
            state=_UsageParseState(
                has_total_usage=True,
                project=project or _single_project(deltas),
                previous_timestamp=modified_at,
            ),
            last_read_bytes=size,
            complete=True,
        )
        self._store_replaced(path, cached_file, deltas)
        return cached_file

    def _load_cached(self, path: Path) -> _CachedFile | None:
        cached = self._cache.get(path)
        if cached is None and self._persistent is not None:
            cached = self._persistent.load(path)
            if cached is not None:
                self._cache[path] = cached
        return cached

    def _store_replaced(
        self,
        path: Path,
        cached_file: _CachedFile,
        deltas: tuple[UsageDelta, ...],
    ) -> None:
        """整表替换这一文件的增量。同 ID 覆盖后不能再按尾部追加。"""

        self._cache[path] = cached_file
        self._rollups.pop(path, None)
        if self._persistent is not None:
            self._persistent.save(
                path,
                cached_file,
                deltas,
                (),
                replace_deltas=True,
            )

    def _grok_home_for_log(self, path: Path) -> Path | None:
        """判断路径是否为某个 GROK_HOME 的统一用量日志。"""

        for home in self._grok_homes:
            if path == grok_unified_log(home):
                return home
        return None

    def _grok_session_index(
        self,
        grok_home: Path,
    ) -> dict[str, GrokSessionInfo]:
        """按发现间隔缓存 Grok session 的模型和项目。"""

        current_time = time.time()
        previous_time = self._grok_sessions_at.get(grok_home, 0)
        cached = self._grok_sessions.get(grok_home)
        if cached is not None and current_time - previous_time < self.discovery_interval:
            return cached
        index = load_session_index(grok_home)
        self._grok_sessions[grok_home] = index
        self._grok_sessions_at[grok_home] = current_time
        return index

    def _read_grok_log(
        self,
        path: Path,
        grok_home: Path,
        maximum_bytes: int,
    ) -> _CachedFile | None:
        """增量解析 Grok unified.jsonl 中的单次请求用量。"""

        try:
            stat_result = path.stat()
        except OSError:
            return None
        signature = (
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )
        cached = self._cache.get(path)
        if cached is None and self._persistent is not None:
            cached = self._persistent.load(path)
            if cached is not None:
                self._cache[path] = cached
        if cached is not None and cached.signature == signature and cached.complete:
            unchanged = replace(cached, last_read_bytes=0)
            self._cache[path] = unchanged
            return unchanged

        can_append = (
            cached is not None
            and cached.signature[0] == signature[0]
            and (not cached.complete or stat_result.st_size > cached.signature[2])
            and stat_result.st_size >= cached.next_offset
        )
        session_index = self._grok_session_index(grok_home)
        default_model = "grok-4.6"
        start_state = (
            cached.state
            if can_append and cached is not None
            else _UsageParseState(previous_timestamp=stat_result.st_mtime)
        )
        parsed = parse_grok_log_chunk(
            path,
            offset=cached.next_offset if can_append and cached is not None else 0,
            session_index=session_index,
            default_model=default_model,
            discarding_oversized_line=start_state.discarding_oversized_line,
            maximum_bytes=maximum_bytes,
        )
        new_deltas = tuple(
            UsageDelta(
                timestamp=event.timestamp,
                model=event.model,
                usage=TokenUsage(
                    input_tokens=event.input_tokens,
                    cached_input_tokens=event.cached_input_tokens,
                    output_tokens=event.output_tokens,
                    reasoning_output_tokens=event.reasoning_output_tokens,
                    total_tokens=event.total_tokens,
                ),
                billing_usage=TokenUsage(
                    input_tokens=event.input_tokens,
                    cached_input_tokens=event.cached_input_tokens,
                    output_tokens=event.output_tokens,
                    reasoning_output_tokens=event.reasoning_output_tokens,
                    total_tokens=event.total_tokens,
                ),
                project=event.project,
            )
            for event in parsed.events
        )
        if can_append and cached is not None:
            cached_file = _CachedFile(
                signature=signature,
                next_offset=parsed.next_offset,
                total_deltas=cached.total_deltas + new_deltas,
                fallback_deltas=cached.fallback_deltas,
                state=_UsageParseState(
                    has_total_usage=True,
                    current_model=default_model,
                    previous_timestamp=stat_result.st_mtime,
                    discarding_oversized_line=parsed.discarding_oversized_line,
                ),
                last_read_bytes=parsed.bytes_read,
                complete=parsed.reached_eof,
            )
            replace_deltas = False
            persist_deltas = new_deltas
        else:
            cached_file = _CachedFile(
                signature=signature,
                next_offset=parsed.next_offset,
                total_deltas=new_deltas,
                fallback_deltas=(),
                state=_UsageParseState(
                    has_total_usage=True,
                    current_model=default_model,
                    previous_timestamp=stat_result.st_mtime,
                    discarding_oversized_line=parsed.discarding_oversized_line,
                ),
                last_read_bytes=parsed.bytes_read,
                complete=parsed.reached_eof,
            )
            replace_deltas = True
            persist_deltas = new_deltas
        self._cache[path] = cached_file
        if self._persistent is not None:
            self._persistent.save(
                path,
                cached_file,
                persist_deltas,
                (),
                replace_deltas=replace_deltas,
            )
        return cached_file

    def _kimi_home_for_wire(self, path: Path) -> Path | None:
        """判断路径是否为某个 KIMI_CODE_HOME 的会话 wire 日志。"""

        if path.name != "wire.jsonl":
            return None
        for home in self._kimi_homes:
            try:
                if path.is_relative_to(home / "sessions"):
                    return home
            except ValueError:
                continue
        return None

    def _kimi_session_index(
        self,
        kimi_home: Path,
    ) -> dict[str, KimiSessionInfo]:
        """按发现间隔缓存 Kimi session 的工作目录。"""

        current_time = time.time()
        previous_time = self._kimi_sessions_at.get(kimi_home, 0)
        cached = self._kimi_sessions.get(kimi_home)
        if cached is not None and current_time - previous_time < self.discovery_interval:
            return cached
        index = load_kimi_session_index(kimi_home)
        self._kimi_sessions[kimi_home] = index
        self._kimi_sessions_at[kimi_home] = current_time
        return index

    def _read_kimi_wire(
        self,
        path: Path,
        kimi_home: Path,
        maximum_bytes: int,
    ) -> _CachedFile | None:
        """增量解析 Kimi wire.jsonl 中的单次请求用量。"""

        try:
            stat_result = path.stat()
        except OSError:
            return None
        signature = (
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )
        cached = self._cache.get(path)
        if cached is None and self._persistent is not None:
            cached = self._persistent.load(path)
            if cached is not None:
                self._cache[path] = cached
        if cached is not None and cached.signature == signature and cached.complete:
            unchanged = replace(cached, last_read_bytes=0)
            self._cache[path] = unchanged
            return unchanged

        can_append = (
            cached is not None
            and cached.signature[0] == signature[0]
            and (not cached.complete or stat_result.st_size > cached.signature[2])
            and stat_result.st_size >= cached.next_offset
        )
        session_index = self._kimi_session_index(kimi_home)
        default_model = "kimi-code/k3-256k"
        start_state = (
            cached.state
            if can_append and cached is not None
            else _UsageParseState(previous_timestamp=stat_result.st_mtime)
        )
        parsed = parse_kimi_wire_chunk(
            path,
            offset=cached.next_offset if can_append and cached is not None else 0,
            session_index=session_index,
            default_model=default_model,
            discarding_oversized_line=start_state.discarding_oversized_line,
            maximum_bytes=maximum_bytes,
        )
        new_deltas = tuple(
            UsageDelta(
                timestamp=event.timestamp,
                model=event.model,
                usage=TokenUsage(
                    input_tokens=event.input_tokens,
                    cached_input_tokens=event.cached_input_tokens,
                    cache_write_input_tokens=event.cache_write_input_tokens,
                    output_tokens=event.output_tokens,
                    total_tokens=event.total_tokens,
                ),
                billing_usage=TokenUsage(
                    input_tokens=event.input_tokens,
                    cached_input_tokens=event.cached_input_tokens,
                    cache_write_input_tokens=event.cache_write_input_tokens,
                    output_tokens=event.output_tokens,
                    total_tokens=event.total_tokens,
                ),
                project=event.project,
            )
            for event in parsed.events
        )
        if can_append and cached is not None:
            cached_file = _CachedFile(
                signature=signature,
                next_offset=parsed.next_offset,
                total_deltas=cached.total_deltas + new_deltas,
                fallback_deltas=cached.fallback_deltas,
                state=_UsageParseState(
                    has_total_usage=True,
                    current_model=default_model,
                    previous_timestamp=stat_result.st_mtime,
                    discarding_oversized_line=parsed.discarding_oversized_line,
                ),
                last_read_bytes=parsed.bytes_read,
                complete=parsed.reached_eof,
            )
            replace_deltas = False
            persist_deltas = new_deltas
        else:
            cached_file = _CachedFile(
                signature=signature,
                next_offset=parsed.next_offset,
                total_deltas=new_deltas,
                fallback_deltas=(),
                state=_UsageParseState(
                    has_total_usage=True,
                    current_model=default_model,
                    previous_timestamp=stat_result.st_mtime,
                    discarding_oversized_line=parsed.discarding_oversized_line,
                ),
                last_read_bytes=parsed.bytes_read,
                complete=parsed.reached_eof,
            )
            replace_deltas = True
            persist_deltas = new_deltas
        self._cache[path] = cached_file
        if self._persistent is not None:
            self._persistent.save(
                path,
                cached_file,
                persist_deltas,
                (),
                replace_deltas=replace_deltas,
            )
        return cached_file

    def _read_claude_transcript(
        self,
        path: Path,
        maximum_bytes: int,
    ) -> _CachedFile | None:
        """增量解析 Claude Code 会话 JSONL 中的单次请求用量。"""

        try:
            stat_result = path.stat()
        except OSError:
            return None
        signature = (
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )
        cached = self._cache.get(path)
        if cached is None and self._persistent is not None:
            cached = self._persistent.load(path)
            if cached is not None:
                self._cache[path] = cached
        if cached is not None and cached.signature == signature and cached.complete:
            unchanged = replace(cached, last_read_bytes=0)
            self._cache[path] = unchanged
            return unchanged

        can_append = (
            cached is not None
            and cached.signature[0] == signature[0]
            and (not cached.complete or stat_result.st_size > cached.signature[2])
            and stat_result.st_size >= cached.next_offset
        )
        start_state = (
            cached.state
            if can_append and cached is not None
            else _UsageParseState(previous_timestamp=stat_result.st_mtime)
        )
        parsed = parse_claude_chunk(
            path,
            offset=cached.next_offset if can_append and cached is not None else 0,
            seen_ids=start_state.recent_ids,
            discarding_oversized_line=start_state.discarding_oversized_line,
            maximum_bytes=maximum_bytes,
        )
        new_deltas = tuple(
            UsageDelta(
                timestamp=event.timestamp,
                model=event.model,
                usage=TokenUsage(
                    input_tokens=event.input_tokens,
                    cached_input_tokens=event.cached_input_tokens,
                    cache_write_input_tokens=event.cache_write_input_tokens,
                    output_tokens=event.output_tokens,
                    total_tokens=event.total_tokens,
                ),
                billing_usage=TokenUsage(
                    input_tokens=event.input_tokens,
                    cached_input_tokens=event.cached_input_tokens,
                    cache_write_input_tokens=event.cache_write_input_tokens,
                    output_tokens=event.output_tokens,
                    total_tokens=event.total_tokens,
                ),
                project=event.project,
            )
            for event in parsed.events
        )
        next_state = _UsageParseState(
            has_total_usage=True,
            current_model=(
                parsed.events[-1].model
                if parsed.events
                else start_state.current_model
            ),
            project=parsed.project or start_state.project,
            previous_timestamp=stat_result.st_mtime,
            discarding_oversized_line=parsed.discarding_oversized_line,
            recent_ids=parsed.seen_ids,
        )
        if can_append and cached is not None:
            cached_file = _CachedFile(
                signature=signature,
                next_offset=parsed.next_offset,
                total_deltas=cached.total_deltas + new_deltas,
                fallback_deltas=cached.fallback_deltas,
                state=next_state,
                last_read_bytes=parsed.bytes_read,
                complete=parsed.reached_eof,
            )
            replace_deltas = False
            persist_deltas = new_deltas
        else:
            cached_file = _CachedFile(
                signature=signature,
                next_offset=parsed.next_offset,
                total_deltas=new_deltas,
                fallback_deltas=(),
                state=next_state,
                last_read_bytes=parsed.bytes_read,
                complete=parsed.reached_eof,
            )
            replace_deltas = True
            persist_deltas = new_deltas
        self._cache[path] = cached_file
        if self._persistent is not None:
            self._persistent.save(
                path,
                cached_file,
                persist_deltas,
                (),
                replace_deltas=replace_deltas,
            )
        return cached_file

    def _read_dsh_projcache(
        self,
        path: Path,
        maximum_bytes: int,
    ) -> _CachedFile | None:
        """把 DeepSeek Harness projcache 的累计 token 转成增量。"""

        try:
            stat_result = path.stat()
        except OSError:
            return None
        if stat_result.st_size > maximum_bytes:
            return None
        signature = (
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )
        cached = self._cache.get(path)
        if cached is None and self._persistent is not None:
            cached = self._persistent.load(path)
            if cached is not None:
                self._cache[path] = cached
        if cached is not None and cached.signature == signature and cached.complete:
            unchanged = replace(cached, last_read_bytes=0)
            self._cache[path] = unchanged
            return unchanged
        snapshot = parse_dsh_projcache(path)
        if snapshot is None:
            empty = _CachedFile(
                signature=signature,
                next_offset=stat_result.st_size,
                total_deltas=cached.total_deltas if cached is not None else (),
                fallback_deltas=(),
                state=cached.state if cached is not None else _UsageParseState(),
                last_read_bytes=stat_result.st_size,
                complete=True,
            )
            self._cache[path] = empty
            return empty
        current = TokenUsage(
            input_tokens=snapshot.input_tokens,
            cached_input_tokens=snapshot.cached_input_tokens,
            cache_write_input_tokens=snapshot.cache_write_input_tokens,
            output_tokens=snapshot.output_tokens,
            total_tokens=snapshot.total_tokens,
        )
        previous = cached.state.total_baseline if cached is not None else None
        delta_usage = _token_usage_delta(current, previous)
        new_deltas: tuple[UsageDelta, ...] = ()
        if delta_usage is not None:
            new_deltas = (
                UsageDelta(
                    timestamp=snapshot.timestamp,
                    model=snapshot.model,
                    usage=delta_usage,
                    billing_usage=delta_usage,
                    project=snapshot.project,
                ),
            )
        if cached is not None and previous is not None:
            cached_file = _CachedFile(
                signature=signature,
                next_offset=stat_result.st_size,
                total_deltas=cached.total_deltas + new_deltas,
                fallback_deltas=cached.fallback_deltas,
                state=_UsageParseState(
                    total_baseline=current,
                    has_total_usage=True,
                    current_model=snapshot.model,
                    project=snapshot.project,
                    previous_timestamp=snapshot.timestamp,
                ),
                last_read_bytes=stat_result.st_size,
                complete=True,
            )
            replace_deltas = False
            persist_deltas = new_deltas
        else:
            cached_file = _CachedFile(
                signature=signature,
                next_offset=stat_result.st_size,
                total_deltas=new_deltas,
                fallback_deltas=(),
                state=_UsageParseState(
                    total_baseline=current,
                    has_total_usage=True,
                    current_model=snapshot.model,
                    project=snapshot.project,
                    previous_timestamp=snapshot.timestamp,
                ),
                last_read_bytes=stat_result.st_size,
                complete=True,
            )
            replace_deltas = True
            persist_deltas = new_deltas
        self._cache[path] = cached_file
        if self._persistent is not None:
            self._persistent.save(
                path,
                cached_file,
                persist_deltas,
                (),
                replace_deltas=replace_deltas,
            )
        return cached_file
