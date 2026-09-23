"""DeepSeek Harness 本地身份、活动会话和 projcache 用量。

活动会话以进程实际打开的 ``session.lock`` 为准。用量只读取
``storages/session_projcache`` 中的 token 合计和模型，不读取标题、提示词
或 zstd 会话正文。凭据文件只检查是否存在，不读取 API Key。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .agents import scan_running_agents
from .multi_models import DetectionConfidence, SessionStatus, TrackedSession
from .quota import QuotaSnapshot


@dataclass(frozen=True)
class DshAccount:
    """一个 DSH_HOME 的安全身份信息。"""

    home: Path
    account_id: str | None
    display_name: str
    profile_name: str = "dsh"
    has_credentials: bool = False
    model: str | None = None

    @property
    def account_key(self) -> str:
        """返回优先使用本地匿名 ID 的归组键。"""

        return self.account_id or f"profile:{self.profile_name}"


@dataclass(frozen=True)
class DshUsageSnapshot:
    """一份 projcache 会话的累计 token 快照。"""

    session_id: str
    timestamp: float
    model: str
    project: str | None
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    total_tokens: int


def default_dsh_home() -> Path:
    """返回 DeepSeek Harness 默认主目录。"""

    configured = os.environ.get("DSH_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".dsh"


def resolve_dsh_homes(homes: Sequence[Path] | None = None) -> tuple[Path, ...]:
    """解析要扫描的 DSH_HOME；未传入时若默认目录存在则使用它。"""

    if homes is not None:
        unique: list[Path] = []
        seen: set[Path] = set()
        for home in homes:
            normalized = _normalize_path(home)
            if normalized in seen:
                continue
            seen.add(normalized)
            unique.append(normalized)
        return tuple(unique)
    default_home = _normalize_path(default_dsh_home())
    if default_home.is_dir():
        return (default_home,)
    return ()


def read_dsh_account(dsh_home: Path) -> DshAccount:
    """读取 DSH 身份：匿名 ID、是否已配置凭据、默认模型。"""

    home = _normalize_path(dsh_home)
    account_id = _read_anonymous_id(home / ".anonymous-user-id")
    has_credentials = False
    credentials = home / ".credentials.yaml"
    try:
        has_credentials = credentials.is_file() and credentials.stat().st_size > 0
    except OSError:
        has_credentials = False
    model = _read_default_model(home / "settings.yaml")
    display = account_id or "dsh"
    return DshAccount(
        home=home,
        account_id=account_id,
        display_name=display,
        has_credentials=has_credentials,
        model=model,
    )


def read_dsh_quota(dsh_home: Path, now: float | None = None) -> QuotaSnapshot | None:
    """DeepSeek Harness 本地没有订阅额度窗口，只返回身份与默认模型。"""

    account = read_dsh_account(dsh_home)
    if not account.has_credentials and account.account_id is None:
        return None
    observed_at = time.time() if now is None else float(now)
    metadata = {
        "has_credentials": "true" if account.has_credentials else "false",
    }
    if account.model:
        metadata["model"] = account.model
    return QuotaSnapshot(
        observed_at=observed_at,
        windows=(),
        plan_type=account.model or "dsh",
        source="dsh-local",
        metadata=metadata,
    )


def list_dsh_active_sessions(
    dsh_home: Path,
    proc_root: Path | None = None,
    now: float | None = None,
) -> tuple[TrackedSession, ...]:
    """列出当前有 DSH 进程打开 session.lock 的会话。"""

    home = _normalize_path(dsh_home)
    sessions_root = home / "sessions"
    observed_at = time.time() if now is None else float(now)
    agents = scan_running_agents(proc_root=proc_root, products=("dsh",))
    grouped: dict[str, list[int]] = {}
    lock_by_session: dict[str, Path] = {}
    for agent in agents:
        for path in agent.open_paths:
            session_id = _dsh_session_id_from_lock(path, sessions_root)
            if session_id is None:
                continue
            grouped.setdefault(session_id, [])
            if agent.pid not in grouped[session_id]:
                grouped[session_id].append(agent.pid)
            lock_by_session[session_id] = path
    sessions: list[TrackedSession] = []
    for session_id, pids in grouped.items():
        usage = read_dsh_projcache(home, session_id)
        cwd = usage.project if usage is not None else None
        last_event_at = usage.timestamp if usage is not None else None
        last_event_type = usage.model if usage is not None else None
        lock_path = lock_by_session.get(session_id)
        sessions.append(
            TrackedSession(
                thread_id=f"dsh:{session_id}",
                session_id=session_id,
                jsonl_path=str(lock_path) if lock_path is not None else None,
                cwd=cwd,
                source="dsh",
                status=SessionStatus.RUNNING,
                confidence=DetectionConfidence.OPEN_FILE,
                first_seen_at=last_event_at or observed_at,
                last_seen_at=observed_at,
                pids=tuple(sorted(pids)),
                last_event_at=last_event_at,
                last_event_type=last_event_type,
                product="dsh",
                model=usage.model if usage is not None else None,
                project=cwd,
            )
        )
    sessions.sort(key=lambda item: item.last_seen_at, reverse=True)
    return tuple(sessions)


def list_dsh_projcache_files(dsh_home: Path) -> tuple[Path, ...]:
    """列出用量索引要扫描的 projcache JSON。"""

    root = _normalize_path(dsh_home) / "storages" / "session_projcache" / "sessions"
    try:
        return tuple(sorted(path for path in root.glob("*.json") if path.is_file()))
    except OSError:
        return ()


def read_dsh_projcache(dsh_home: Path, session_id: str) -> DshUsageSnapshot | None:
    """读取一份会话的 token 合计，忽略标题和提示。"""

    path = (
        _normalize_path(dsh_home)
        / "storages"
        / "session_projcache"
        / "sessions"
        / f"{session_id}.json"
    )
    return parse_dsh_projcache(path)


def parse_dsh_projcache(path: Path) -> DshUsageSnapshot | None:
    """从 projcache JSON 提取安全的用量字段。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    record = payload.get("record")
    if not isinstance(record, Mapping):
        return None
    identity = record.get("identity")
    rows = record.get("rows")
    if not isinstance(rows, Mapping):
        return None
    totals = _row_mapping(rows, "tokenUsage").get("totals")
    if not isinstance(totals, Mapping):
        return None
    input_tokens = _token_int(totals.get("uncachedInputTokens")) or 0
    cached_input = _token_int(totals.get("cacheReadTokens")) or 0
    cache_write = _token_int(totals.get("cacheWriteTokens")) or 0
    output_tokens = _token_int(totals.get("outputTokens")) or 0
    if input_tokens + cached_input + cache_write + output_tokens <= 0:
        return None
    last_used = _row_mapping(rows, "modelSelection").get("lastUsed")
    model = None
    if isinstance(last_used, Mapping):
        model = _text(last_used.get("model"))
    metadata = _row_mapping(rows, "sessionListMetadata")
    timestamp = _timestamp(metadata.get("lastPromptAt"))
    if timestamp is None:
        try:
            timestamp = path.stat().st_mtime
        except OSError:
            timestamp = time.time()
    project = None
    if isinstance(identity, Mapping):
        project = _text(identity.get("cwd"))
    session_id = path.stem
    total_tokens = input_tokens + cached_input + cache_write + output_tokens
    return DshUsageSnapshot(
        session_id=session_id,
        timestamp=timestamp,
        model=model or "deepseek-v4.1-flash",
        project=project,
        input_tokens=input_tokens + cached_input + cache_write,
        cached_input_tokens=cached_input,
        cache_write_input_tokens=cache_write,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def dsh_projcache_home(path: Path, homes: Sequence[Path]) -> Path | None:
    """判断路径是否属于某个 DSH_HOME 的 projcache。"""

    if path.suffix.lower() != ".json":
        return None
    for home in homes:
        root = _normalize_path(home) / "storages" / "session_projcache" / "sessions"
        try:
            if path.resolve().is_relative_to(root.resolve()):
                return _normalize_path(home)
        except (OSError, ValueError, RuntimeError):
            try:
                if path.is_relative_to(root):
                    return _normalize_path(home)
            except ValueError:
                continue
    return None


def _dsh_session_id_from_lock(path: Path, sessions_root: Path) -> str | None:
    """从打开的 session.lock 提取会话 ID。"""

    if path.name != "session.lock":
        return None
    try:
        resolved = path.resolve()
        resolved.relative_to(sessions_root.resolve())
    except (OSError, ValueError, RuntimeError):
        try:
            path.relative_to(sessions_root)
        except ValueError:
            return None
    return path.parent.name or None


def _read_anonymous_id(path: Path) -> str | None:
    """读取匿名用户 ID 文件。"""

    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _read_default_model(path: Path) -> str | None:
    """从 settings.yaml 读取默认模型，跳过含密钥的行。"""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if "apikey" in lowered or "api_key" in lowered or "token" in lowered:
            continue
        if stripped.startswith("model:"):
            value = stripped.split(":", 1)[1].strip().strip("'\"")
            if value:
                return value
    return None


def _row_mapping(rows: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """读取 projcache row 的 val 对象。"""

    item = rows.get(key)
    if not isinstance(item, Mapping):
        return {}
    value = item.get("val")
    return value if isinstance(value, Mapping) else {}


def _token_int(value: Any) -> int | None:
    """把 token 字段转换为非负整数。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return int(number)


def _timestamp(value: Any) -> float | None:
    """解析 Unix 秒或毫秒。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 10_000_000_000:
        return number / 1000
    return number


def _text(value: Any) -> str | None:
    """读取非空字符串。"""

    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _normalize_path(path: Path) -> Path:
    """把路径展开为绝对路径。"""

    expanded = path.expanduser()
    try:
        return expanded.resolve()
    except (OSError, RuntimeError):
        return expanded.absolute()
