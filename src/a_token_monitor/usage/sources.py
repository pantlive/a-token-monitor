"""扫描来源：把账号目录展开成会话文件清单并去重（``UsageAggregator`` 的发现部分）。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Mapping

from ..commandcode import (
    list_commandcode_transcripts,
    read_commandcode_account,
)
from ..grok import (
    grok_unified_log,
    read_grok_account,
)
from ..dsh import (
    list_dsh_projcache_files,
    read_dsh_account,
)
from ..claude import (
    list_claude_transcripts,
    read_claude_account,
    resolve_sidechain_policy,
)
from ..kimi import (
    read_kimi_account,
)
from ..local_agents import (
    chat_project,
    list_aider_histories,
    list_cursor_transcripts,
    list_gemini_chats,
    list_opencode_dbs,
    list_qwen_chats,
)
from ..registry import MultiSessionRegistry
from .parsing import (
    _canonical_source_rank,
    _normalized_path,
    _safe_file_size,
)
from .records import (
    _UsageSource,
    _codex_session_id,
    _text_value,
)


class _SourceDiscoveryMixin:
    """``UsageAggregator`` 的混入类；依赖其 ``__init__`` 建立的实例属性。"""

    def _scope_key(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        metadata_by_profile: Mapping[str, Mapping[str, str | None]],
    ) -> tuple[tuple[str, str, str, str], ...]:
        """生成缓存作用域，避免复用到另一组账号目录。"""

        registry_scope = tuple(
            sorted(
                (
                    profile_name,
                    str(registry.state_dir),
                    str(
                        metadata_by_profile.get(profile_name, {}).get("account_id")
                        or ""
                    ),
                    str(
                        metadata_by_profile.get(profile_name, {}).get("codex_home")
                        or ""
                    ),
                )
                for profile_name, registry in registries.items()
            )
        )
        grok_scope = tuple(("grok", str(home), "", "") for home in self._grok_homes)
        kimi_scope = tuple(("kimi", str(home), "", "") for home in self._kimi_homes)
        dsh_scope = tuple(("dsh", str(home), "", "") for home in self._dsh_homes)
        claude_scope = tuple(
            ("claude", str(home), "", "") for home in self._claude_homes
        )
        commandcode_scope = tuple(
            ("command-code", str(home), "", "") for home in self._commandcode_homes
        )
        opencode_scope = tuple(
            ("opencode", str(home), "", "") for home in self._opencode_homes
        )
        cursor_scope = tuple(
            ("cursor", str(home), "", "") for home in self._cursor_homes
        )
        gemini_scope = tuple(
            ("gemini", str(home), "", "") for home in self._gemini_homes
        )
        qwen_scope = tuple(
            ("qwen", str(home), "", "") for home in self._qwen_homes
        )
        aider_scope = tuple(
            ("aider", str(home), "", "") for home in self._aider_homes
        )
        return (
            registry_scope
            + grok_scope
            + kimi_scope
            + dsh_scope
            + claude_scope
            + commandcode_scope
            + opencode_scope
            + cursor_scope
            + gemini_scope
            + qwen_scope
            + aider_scope
        )

    def _build_sources(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        metadata_by_profile: Mapping[str, Mapping[str, str | None]],
    ) -> dict[Path, _UsageSource]:
        """建立 JSONL 路径到账号身份的映射。"""

        sources: dict[Path, _UsageSource] = {}
        for profile_name, registry in registries.items():
            metadata = metadata_by_profile.get(profile_name, {})
            account_id = _text_value(metadata.get("account_id"))
            codex_home = _text_value(metadata.get("codex_home"))
            default_source = _UsageSource(
                profile_name=_text_value(metadata.get("profile_name")) or profile_name,
                account_id=account_id,
                codex_home=codex_home,
                product="codex",
            )
            if codex_home is not None:
                root = Path(codex_home).expanduser() / "sessions"
                for path in self._discover_jsonl(root):
                    sources.setdefault(path, default_source)

            # 注册表中的路径优先于当前 auth.json 的账号 ID。这样 profile
            # 换号后，旧会话的历史用量仍归到它原来记录的账号。
            for session in registry.list_sessions(active_only=False):
                path = _normalized_path(session.jsonl_path)
                if path is None:
                    continue
                session_source = _UsageSource(
                    profile_name=default_source.profile_name,
                    account_id=session.account_id or account_id,
                    codex_home=codex_home,
                    project=_text_value(session.cwd),
                    product="codex",
                )
                current_source = sources.get(path)
                if (
                    current_source is None
                    or session.account_id is not None
                    or (
                        current_source.project is None
                        and session_source.project is not None
                    )
                ):
                    sources[path] = session_source
        # 中文注释：同一 Codex session 可能在多个 CODEX_HOME 中留下前缀副本。
        # Grok 的 unified.jsonl 没有 Codex session UUID，必须在加入 Grok 前去重。
        sources = self._deduplicate_codex_sources(sources)
        for grok_home in self._grok_homes:
            log_path = grok_unified_log(grok_home)
            if not log_path.is_file():
                continue
            account = read_grok_account(grok_home)
            sources[log_path] = _UsageSource(
                profile_name=account.profile_name,
                account_id=account.account_id,
                codex_home=str(grok_home),
                product="grok",
            )
        for kimi_home in self._kimi_homes:
            account = read_kimi_account(kimi_home)
            for path in self._discover_kimi_wires(kimi_home):
                sources[path] = _UsageSource(
                    profile_name=account.profile_name,
                    account_id=account.account_id,
                    codex_home=str(kimi_home),
                    product="kimi",
                )
        for dsh_home in self._dsh_homes:
            account = read_dsh_account(dsh_home)
            for path in list_dsh_projcache_files(dsh_home):
                sources[path] = _UsageSource(
                    profile_name=account.profile_name,
                    account_id=account.account_id,
                    codex_home=str(dsh_home),
                    product="dsh",
                )
        for claude_home in self._claude_homes:
            account = read_claude_account(claude_home)
            include_subagents = self._claude_include_subagents(claude_home)
            for path in list_claude_transcripts(
                claude_home,
                include_subagents=include_subagents,
            ):
                sources[path] = _UsageSource(
                    profile_name=account.profile_name,
                    account_id=account.account_id,
                    codex_home=str(claude_home),
                    product="claude",
                )
        for home in self._commandcode_homes:
            account = read_commandcode_account(home)
            for path in list_commandcode_transcripts(home):
                sources[path.resolve()] = _UsageSource(
                    profile_name=account.profile_name,
                    account_id=account.account_id,
                    codex_home=str(home),
                    product="command-code",
                )
        for home in self._opencode_homes:
            for path in self._cached_paths(
                home / ".usage-opencode",
                lambda home=home: list_opencode_dbs((home,)),
            ):
                sources[path] = _UsageSource(
                    profile_name="opencode",
                    account_id=None,
                    codex_home=str(home),
                    product="opencode",
                )
        for home in self._cursor_homes:
            for path in self._cached_paths(
                home / ".usage-cursor",
                lambda home=home: list_cursor_transcripts(home),
            ):
                sources[path] = _UsageSource(
                    profile_name="cursor",
                    account_id=None,
                    codex_home=str(home),
                    project=chat_project(home, path, product="cursor"),
                    product="cursor",
                )
        for home in self._gemini_homes:
            for path in self._cached_paths(
                home / ".usage-gemini",
                lambda home=home: list_gemini_chats(home),
            ):
                sources[path] = _UsageSource(
                    profile_name="gemini",
                    account_id=None,
                    codex_home=str(home),
                    project=chat_project(home, path, product="gemini"),
                    product="gemini",
                )
        for home in self._qwen_homes:
            for path in self._cached_paths(
                home / ".usage-qwen",
                lambda home=home: list_qwen_chats(home),
            ):
                sources[path] = _UsageSource(
                    profile_name="qwen",
                    account_id=None,
                    codex_home=str(home),
                    project=chat_project(home, path, product="qwen"),
                    product="qwen",
                )
        for home in self._aider_homes:
            for path in self._cached_paths(
                home / ".usage-aider",
                lambda home=home: list_aider_histories(home),
            ):
                sources[path] = _UsageSource(
                    profile_name="aider",
                    account_id=None,
                    codex_home=str(home),
                    project=str(home),
                    product="aider",
                )
        return sources

    def _claude_include_subagents(self, claude_home: Path) -> bool:
        """判断是否要索引 subagents 目录，带 10 分钟缓存。"""

        now = time.monotonic()
        with self._lock:
            cached = self._claude_sidechain_cache.get(claude_home)
            if cached is not None and now - cached[0] < 600.0:
                return cached[1]
        include = resolve_sidechain_policy([claude_home]).get(claude_home, True)
        with self._lock:
            self._claude_sidechain_cache[claude_home] = (now, include)
        return include

    def _deduplicate_codex_sources(
        self,
        sources: Mapping[Path, _UsageSource],
    ) -> dict[Path, _UsageSource]:
        """按 Codex session UUID 保留最长副本，避免跨 profile 重复统计。"""

        canonical_by_session: dict[str, Path] = {}
        unique_sources: dict[Path, _UsageSource] = {}
        duplicate_paths: list[Path] = []
        for path, source in sorted(sources.items(), key=lambda item: str(item[0])):
            session_id = _codex_session_id(path)
            if session_id is None:
                unique_sources[path] = source
                continue
            previous_path = canonical_by_session.get(session_id)
            if previous_path is None:
                canonical_by_session[session_id] = path
                unique_sources[path] = source
                continue
            if _canonical_source_rank(path) > _canonical_source_rank(previous_path):
                unique_sources.pop(previous_path)
                duplicate_paths.append(previous_path)
                canonical_by_session[session_id] = path
                unique_sources[path] = source
            else:
                duplicate_paths.append(path)
        self._deduplicated_files = len(duplicate_paths)
        self._deduplicated_bytes = sum(
            _safe_file_size(path) for path in duplicate_paths
        )
        return unique_sources

    def _discover_jsonl(self, root: Path) -> tuple[Path, ...]:
        """发现一个 session 根目录下的 JSONL，并按时间缓存目录遍历。"""

        normalized_root = _normalized_path(str(root)) or root
        current_time = time.time()
        previous_time = self._discovered_at.get(normalized_root, 0)
        if current_time - previous_time < self.discovery_interval:
            return self._discovered.get(normalized_root, ())
        try:
            paths = tuple(
                sorted(
                    (
                        _normalized_path(str(path))
                        for path in normalized_root.rglob("*.jsonl")
                    ),
                    key=lambda item: str(item),
                )
            )
        except OSError:
            paths = ()
        normalized_paths = tuple(path for path in paths if path is not None)
        self._discovered[normalized_root] = normalized_paths
        self._discovered_at[normalized_root] = current_time
        return normalized_paths

    def _discover_kimi_wires(self, kimi_home: Path) -> tuple[Path, ...]:
        """发现一个 KIMI_CODE_HOME 下的 wire.jsonl，并按时间缓存遍历。"""

        root = kimi_home / "sessions"
        normalized_root = _normalized_path(str(root)) or root
        current_time = time.time()
        previous_time = self._discovered_at.get(normalized_root, 0)
        if current_time - previous_time < self.discovery_interval:
            return self._discovered.get(normalized_root, ())
        try:
            paths = tuple(
                sorted(
                    (
                        _normalized_path(str(path))
                        for path in normalized_root.rglob("wire.jsonl")
                    ),
                    key=lambda item: str(item),
                )
            )
        except OSError:
            paths = ()
        normalized_paths = tuple(path for path in paths if path is not None)
        self._discovered[normalized_root] = normalized_paths
        self._discovered_at[normalized_root] = current_time
        return normalized_paths
