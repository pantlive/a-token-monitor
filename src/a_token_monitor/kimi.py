"""Kimi Code CLI 本地身份、会话元数据、wire 日志用量和配额读取。

用量数据来自 ``KIMI_CODE_HOME/sessions/<工作目录>/<session>/agents/*/wire.jsonl``
中的 ``usage.record`` 事件，以及 session 目录下的 ``state.json``。

配额通过与官方 CLI 相同的 ``GET {base}/usages`` 接口读取；access token 过期时
按官方相同的目录锁协议刷新并原子写回凭据。token 只用于鉴权请求，永远不会
出现在返回值、日志或异常消息中；不读取会话正文或工具输出。
"""

from __future__ import annotations

import json
import math
import os
import random
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .agents import scan_running_agents
from .multi_models import DetectionConfidence, SessionStatus, TrackedSession
from .quota import QuotaSnapshot, QuotaWindow


_KIMI_USAGE_RECORD = b'"usage.record"'
_MAX_LINE_BYTES = 64 * 1024


@dataclass(frozen=True)
class KimiAccount:
    """一个 KIMI_CODE_HOME 的安全身份信息。"""

    home: Path
    account_id: str | None
    display_name: str
    profile_name: str = "kimi"
    logged_in: bool = False

    @property
    def account_key(self) -> str:
        """返回优先使用真实用户 ID 的归组键。"""

        return self.account_id or f"profile:{self.profile_name}"


@dataclass(frozen=True)
class KimiSessionInfo:
    """从 state.json 提取的会话工作目录。"""

    session_id: str
    cwd: str | None


@dataclass(frozen=True)
class KimiUsageEvent:
    """一次 Kimi 模型请求的 token 用量。"""

    timestamp: float
    model: str
    project: str | None
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class KimiWireParseResult:
    """wire.jsonl 一段追加内容的解析结果。"""

    next_offset: int
    events: tuple[KimiUsageEvent, ...]
    bytes_read: int
    reached_eof: bool
    discarding_oversized_line: bool


def default_kimi_home() -> Path:
    """返回 Kimi Code 默认主目录。"""

    configured = os.environ.get("KIMI_CODE_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".kimi-code"


def resolve_kimi_homes(homes: Sequence[Path] | None = None) -> tuple[Path, ...]:
    """解析要扫描的 KIMI_CODE_HOME；未传入时若默认目录存在则使用它。"""

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
    default_home = _normalize_path(default_kimi_home())
    if default_home.is_dir():
        return (default_home,)
    return ()


def read_kimi_account(kimi_home: Path) -> KimiAccount:
    """读取 Kimi 登录状态，只检查凭据文件存在性，不读取令牌内容。"""

    home = _normalize_path(kimi_home)
    logged_in = False
    credentials_dir = home / "credentials"
    try:
        for credential in credentials_dir.glob("*.json"):
            try:
                if credential.is_file() and credential.stat().st_size > 0:
                    logged_in = True
                    break
            except OSError:
                continue
    except OSError:
        logged_in = False
    return KimiAccount(
        home=home,
        account_id=None,
        display_name="kimi",
        logged_in=logged_in,
    )


def load_kimi_session_index(kimi_home: Path) -> dict[str, KimiSessionInfo]:
    """扫描 state.json，建立 session id 到工作目录的映射。"""

    index: dict[str, KimiSessionInfo] = {}
    sessions_root = _normalize_path(kimi_home) / "sessions"
    try:
        candidates = list(sessions_root.glob("*/*/state.json"))
    except OSError:
        return index
    for path in candidates:
        session_id = path.parent.name
        if not session_id:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        index[session_id] = KimiSessionInfo(
            session_id=session_id,
            cwd=_text(payload.get("cwd")),
        )
    return index


def list_kimi_active_sessions(
    kimi_home: Path,
    proc_root: Path | None = None,
    now: float | None = None,
) -> tuple[TrackedSession, ...]:
    """列出当前有 Kimi 进程打开的活动会话，不读取提示词。"""

    home = _normalize_path(kimi_home)
    sessions_root = home / "sessions"
    observed_at = time.time() if now is None else float(now)
    agents = scan_running_agents(
        proc_root=proc_root,
        products=("kimi",),
        session_roots=(sessions_root,),
    )
    index = load_kimi_session_index(home)
    grouped: dict[str, list[int]] = {}
    paths_by_session: dict[str, Path] = {}
    for agent in agents:
        for path in agent.open_paths:
            session_id = _kimi_session_id_from_path(path, sessions_root)
            if session_id is None:
                continue
            grouped.setdefault(session_id, [])
            if agent.pid not in grouped[session_id]:
                grouped[session_id].append(agent.pid)
            paths_by_session.setdefault(session_id, path)
        if agent.cwd is not None:
            for session_id, info in index.items():
                if info.cwd and Path(info.cwd) == agent.cwd:
                    grouped.setdefault(session_id, [])
                    if agent.pid not in grouped[session_id]:
                        grouped[session_id].append(agent.pid)
    sessions: list[TrackedSession] = []
    for session_id, pids in grouped.items():
        state_path = _kimi_state_path(sessions_root, session_id)
        cwd, updated_at, event_type = _kimi_state_meta(state_path)
        jsonl_path = paths_by_session.get(session_id)
        if jsonl_path is None and state_path is not None:
            jsonl_path = state_path
        sessions.append(
            TrackedSession(
                thread_id=f"kimi:{session_id}",
                session_id=session_id,
                jsonl_path=str(jsonl_path) if jsonl_path is not None else None,
                cwd=cwd,
                source="kimi-cli",
                status=SessionStatus.RUNNING,
                confidence=DetectionConfidence.OPEN_FILE,
                first_seen_at=updated_at or observed_at,
                last_seen_at=observed_at,
                pids=tuple(sorted(pids)),
                last_event_at=updated_at,
                last_event_type=event_type,
                product="kimi",
                project=cwd,
            )
        )
    sessions.sort(key=lambda item: item.last_seen_at, reverse=True)
    return tuple(sessions)


def _kimi_session_id_from_path(path: Path, sessions_root: Path) -> str | None:
    """从打开的会话文件路径提取 session 目录名。"""

    try:
        relative = path.resolve().relative_to(sessions_root.resolve())
    except (OSError, ValueError, RuntimeError):
        try:
            relative = path.relative_to(sessions_root)
        except ValueError:
            return None
    parts = relative.parts
    if len(parts) >= 2 and parts[1].startswith("session"):
        return parts[1]
    return None


def _kimi_state_path(sessions_root: Path, session_id: str) -> Path | None:
    """定位一个 session 的 state.json。"""

    try:
        matches = list(sessions_root.glob(f"*/{session_id}/state.json"))
    except OSError:
        return None
    return matches[0] if matches else None


def _kimi_state_meta(
    path: Path | None,
) -> tuple[str | None, float | None, str | None]:
    """读取 cwd、更新时间和最近一轮状态，忽略提示词。"""

    if path is None:
        return None, None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None, None
    if not isinstance(payload, Mapping):
        return None, None, None
    cwd = _text(payload.get("cwd"))
    updated_at = _timestamp(payload.get("updatedAt"))
    event_type = _text(payload.get("lastTurnReason"))
    return cwd, updated_at, event_type


def kimi_wire_session_id(path: Path) -> str | None:
    """从 wire.jsonl 路径提取所属 session 目录名。"""

    # 布局：sessions/<工作目录>/<session>/agents/<agent>/wire.jsonl
    if path.name != "wire.jsonl":
        return None
    try:
        session_dir = path.parents[2]
    except IndexError:
        return None
    if path.parents[1].name != "agents":
        return None
    return session_dir.name or None


def parse_kimi_wire_chunk(
    path: Path,
    offset: int,
    session_index: Mapping[str, KimiSessionInfo],
    default_model: str,
    discarding_oversized_line: bool = False,
    maximum_bytes: int | None = None,
) -> KimiWireParseResult:
    """从已确认偏移继续解析 wire.jsonl 中的单次请求用量。"""

    if offset < 0:
        raise ValueError("offset 不能小于 0")
    if maximum_bytes is not None and maximum_bytes <= 0:
        raise ValueError("maximum_bytes 必须大于 0")
    events: list[KimiUsageEvent] = []
    next_offset = offset
    bytes_read = 0
    reached_eof = False
    discarding = discarding_oversized_line
    session_id = kimi_wire_session_id(path)
    session = session_index.get(session_id or "")
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            content = (
                handle.read(maximum_bytes)
                if maximum_bytes is not None
                else handle.read()
            )
            bytes_read = len(content)
            reached_physical_eof = maximum_bytes is None or bytes_read < maximum_bytes
            content_offset = 0
            if discarding:
                first_newline = content.find(b"\n")
                if first_newline < 0:
                    return KimiWireParseResult(
                        next_offset=offset + bytes_read,
                        events=(),
                        bytes_read=bytes_read,
                        reached_eof=reached_physical_eof,
                        discarding_oversized_line=True,
                    )
                content_offset = first_newline + 1
                discarding = False
            remaining = content[content_offset:]
            last_newline = remaining.rfind(b"\n")
            if last_newline < 0:
                if reached_physical_eof:
                    next_offset = offset + content_offset
                    reached_eof = True
                    complete_content = b""
                else:
                    next_offset = offset + bytes_read
                    discarding = True
                    complete_content = b""
            else:
                complete_content = remaining[: last_newline + 1]
                next_offset = offset + content_offset + len(complete_content)
                reached_eof = reached_physical_eof
            for raw_line in complete_content.splitlines(keepends=True):
                if _KIMI_USAGE_RECORD not in raw_line:
                    continue
                if len(raw_line) > _MAX_LINE_BYTES:
                    continue
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                parsed = _usage_event_from_wire(
                    event,
                    session,
                    default_model,
                )
                if parsed is not None:
                    events.append(parsed)
    except (OSError, UnicodeError):
        return KimiWireParseResult(
            next_offset=offset,
            events=(),
            bytes_read=0,
            reached_eof=False,
            discarding_oversized_line=discarding_oversized_line,
        )
    return KimiWireParseResult(
        next_offset=next_offset,
        events=tuple(events),
        bytes_read=bytes_read,
        reached_eof=reached_eof,
        discarding_oversized_line=discarding,
    )


def _usage_event_from_wire(
    event: Any,
    session: KimiSessionInfo | None,
    default_model: str,
) -> KimiUsageEvent | None:
    """从 usage.record 事件提取一次请求的 token 计数。"""

    if not isinstance(event, Mapping):
        return None
    if event.get("type") != "usage.record":
        return None
    # 中文注释：只接受单次请求粒度的 turn 增量；未来若出现 session 级
    # 累计快照，直接跳过可以避免把同一上下文重复计入用量。
    if event.get("usageScope") != "turn":
        return None
    usage = event.get("usage")
    if not isinstance(usage, Mapping):
        return None
    input_other = _token_int(usage.get("inputOther")) or 0
    cached_input = _token_int(usage.get("inputCacheRead")) or 0
    cache_write_input = _token_int(usage.get("inputCacheCreation")) or 0
    output_tokens = _token_int(usage.get("output")) or 0
    input_tokens = input_other + cached_input + cache_write_input
    if input_tokens == 0 and output_tokens == 0:
        return None
    timestamp = _timestamp(event.get("time"))
    if timestamp is None:
        return None
    model = _text(event.get("model")) or default_model
    return KimiUsageEvent(
        timestamp=timestamp,
        model=model,
        project=session.cwd if session is not None else None,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input,
        cache_write_input_tokens=cache_write_input,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def _timestamp(value: Any) -> float | None:
    """解析 Unix 秒、毫秒或 ISO 8601 时间。"""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            return number / 1000
        return number
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            number = float(stripped)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        return number / 1000 if number > 10_000_000_000 else number
    return None


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


# --- 官方 /usages 配额读取 ---
#
# 契约来自官方 CLI（packages/oauth/src/managed-usage.ts 与 oauth-manager.ts）：
# - GET {KIMI_CODE_BASE_URL 或按 region 推导的 base}/usages，Bearer 鉴权，8 秒超时；
# - access token 只有约 15 分钟寿命，刷新走 POST {oauth host}/api/oauth/token
#   （form: client_id/grant_type=refresh_token/refresh_token），refresh token 会轮换；
# - 跨进程刷新用 proper-lockfile 目录锁 ``<home>/oauth/kimi-code.lock`` 协调，
#   拿不到锁时 fail closed，绝不无锁刷新。

_KIMI_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
_KIMI_MAINLAND_BASE_URL = "https://api.kimi.com/coding/v1"
_KIMI_GLOBAL_BASE_URL = "https://api.kimi.ai/coding/v1"
_KIMI_MAINLAND_OAUTH_HOST = "https://auth.kimi.com"
_KIMI_GLOBAL_OAUTH_HOST = "https://auth.kimi.ai"
_KIMI_CREDENTIALS_NAME = "kimi-code.json"
_KIMI_USAGE_TIMEOUT_SECONDS = 8.0
_KIMI_REFRESH_TIMEOUT_SECONDS = 10.0
_KIMI_REFRESH_MIN_THRESHOLD_SECONDS = 300.0
_KIMI_REFRESH_MAX_ATTEMPTS = 3
_KIMI_REFRESH_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_KIMI_LOCK_RETRIES = 120
_KIMI_LOCK_STALE_SECONDS = 5.0
_KIMI_LOCK_MIN_DELAY_SECONDS = 0.5
_KIMI_LOCK_MAX_DELAY_SECONDS = 1.0
_KIMI_FIXED_POINT_CENTS = 1_000_000
_KIMI_MAX_RESPONSE_BYTES = 1024 * 1024
_QUOTA_CACHE_SUCCESS_TTL_SECONDS = 60.0
_QUOTA_CACHE_FAILURE_TTL_SECONDS = 15.0

_HttpJsonCaller = Callable[[str, Mapping[str, str], float], tuple[int, Any]]

_quota_cache: dict[Path, tuple[float, QuotaSnapshot | None]] = {}
_quota_cache_lock = threading.Lock()


def read_kimi_quota(
    kimi_home: Path,
    now: float | None = None,
    *,
    http_get_json: _HttpJsonCaller | None = None,
    http_post_form: _HttpJsonCaller | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> QuotaSnapshot | None:
    """通过官方 ``GET /usages`` 接口读取 Kimi 账号配额。

    access token 过期时按官方 CLI 相同的目录锁协议刷新并原子写回凭据后再读。
    任何网络、锁或凭据问题都返回 ``None``，绝不抛出；token 只用于鉴权请求，
    不会出现在返回值或异常消息中。使用真实 HTTP 时结果带缓存（成功 60 秒、
    失败 15 秒），避免 dashboard 轮询反复请求接口。
    """

    home = _normalize_path(kimi_home)
    moment = time.time() if now is None else now
    use_cache = http_get_json is None and http_post_form is None
    if use_cache:
        with _quota_cache_lock:
            cached = _quota_cache.get(home)
        if cached is not None:
            cached_at, cached_value = cached
            ttl = (
                _QUOTA_CACHE_SUCCESS_TTL_SECONDS
                if cached_value is not None
                else _QUOTA_CACHE_FAILURE_TTL_SECONDS
            )
            if moment - cached_at < ttl:
                return cached_value
    try:
        snapshot = _fetch_kimi_quota(
            home,
            moment,
            http_get_json=http_get_json or _http_get_json,
            http_post_form=http_post_form or _http_post_form,
            sleep=sleep,
        )
    except Exception:
        # 防御：配额读取是可选增强，任何意外都不能让监控崩溃。
        snapshot = None
    if use_cache:
        with _quota_cache_lock:
            _quota_cache[home] = (moment, snapshot)
    return snapshot


def _clear_quota_cache() -> None:
    """清空配额缓存（测试用）。"""

    with _quota_cache_lock:
        _quota_cache.clear()


def _fetch_kimi_quota(
    home: Path,
    now: float,
    *,
    http_get_json: _HttpJsonCaller,
    http_post_form: _HttpJsonCaller,
    sleep: Callable[[float], None],
) -> QuotaSnapshot | None:
    credentials = _read_kimi_credentials(home)
    if credentials is None:
        return None
    if not _credentials_fresh(credentials, now):
        credentials = _refresh_kimi_credentials(
            home,
            credentials,
            now,
            http_post_form=http_post_form,
            sleep=sleep,
        )
        if credentials is None:
            return None
    access_token = credentials.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None
    status, payload = http_get_json(
        f"{_kimi_base_url(home)}/usages",
        {"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        _KIMI_USAGE_TIMEOUT_SECONDS,
    )
    if status != 200:
        return None
    return _parse_kimi_usages(payload, observed_at=now)


def _credentials_fresh(credentials: Mapping[str, Any], now: float) -> bool:
    """按官方 ensureFresh 规则判断 access token 是否可直接使用。"""

    access_token = credentials.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return False
    expires_at = _float_value(credentials.get("expires_at"))
    if expires_at is None:
        return True
    expires_in = _float_value(credentials.get("expires_in"))
    threshold = _KIMI_REFRESH_MIN_THRESHOLD_SECONDS
    if expires_in is not None and expires_in > 0:
        threshold = max(threshold, expires_in * 0.5)
    return expires_at - now > threshold


def _read_kimi_credentials(home: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(
            (home / "credentials" / _KIMI_CREDENTIALS_NAME).read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _write_kimi_credentials(home: Path, credentials: Mapping[str, Any]) -> None:
    """以 0600 权限原子写回凭据文件。"""

    credentials_dir = home / "credentials"
    credentials_dir.mkdir(parents=True, exist_ok=True)
    target = credentials_dir / _KIMI_CREDENTIALS_NAME
    descriptor, temporary = tempfile.mkstemp(
        dir=credentials_dir,
        prefix=".kimi-code-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(credentials), handle, ensure_ascii=False)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _refresh_kimi_credentials(
    home: Path,
    credentials: Mapping[str, Any],
    now: float,
    *,
    http_post_form: _HttpJsonCaller,
    sleep: Callable[[float], None],
) -> dict[str, Any] | None:
    """在官方同款目录锁内刷新 token 并原子写回；失败返回 None。"""

    refresh_token = credentials.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        return None
    lock = _KimiRefreshLock(home, sleep)
    if not lock.acquire():
        return None
    try:
        # 持锁后重读：等待锁期间其它进程可能已经完成刷新。
        latest = _read_kimi_credentials(home) or dict(credentials)
        if _credentials_fresh(latest, now):
            return latest
        current_refresh = latest.get("refresh_token")
        if not isinstance(current_refresh, str) or not current_refresh:
            return None
        payload: Mapping[str, Any] | None = None
        for attempt in range(_KIMI_REFRESH_MAX_ATTEMPTS):
            if attempt:
                sleep(float(2 ** (attempt - 1)))
            lock.touch()
            status, data = http_post_form(
                f"{_kimi_oauth_host(home)}/api/oauth/token",
                {
                    "client_id": _KIMI_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": current_refresh,
                },
                _KIMI_REFRESH_TIMEOUT_SECONDS,
            )
            if status == 200 and isinstance(data, Mapping):
                payload = data
                break
            if status in _KIMI_REFRESH_RETRYABLE_STATUSES:
                continue
            # 401/403/invalid_grant 等明确失败不重试。
            return None
        if payload is None:
            return None
        updated = _merged_credentials(latest, payload)
        if updated is None:
            return None
        _write_kimi_credentials(home, updated)
        return updated
    finally:
        lock.release()


def _merged_credentials(
    existing: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    """把刷新响应合并进现有凭据；响应缺关键字段时放弃刷新。"""

    access_token = payload.get("access_token")
    new_refresh = payload.get("refresh_token")
    expires_in = _float_value(payload.get("expires_in"))
    if (
        not isinstance(access_token, str)
        or not access_token
        or not isinstance(new_refresh, str)
        or not new_refresh
        or expires_in is None
        or expires_in <= 0
    ):
        return None
    updated = dict(existing)
    updated["access_token"] = access_token
    updated["refresh_token"] = new_refresh
    updated["expires_at"] = int(time.time()) + int(expires_in)
    updated["expires_in"] = int(expires_in)
    scope = payload.get("scope")
    if isinstance(scope, str) and scope:
        updated["scope"] = scope
    token_type = payload.get("token_type")
    if isinstance(token_type, str) and token_type:
        updated["token_type"] = token_type
    return updated


class _KimiRefreshLock:
    """与官方 CLI（proper-lockfile）兼容的目录锁：``<target>.lock/``。

    mkdir 即取锁；锁目录 mtime 超过 5 秒未更新视为陈旧锁并打破；取不到时按
    0.5–1 秒随机间隔重试，最多 120 次后放弃（与官方一致 fail closed）。
    """

    def __init__(
        self,
        home: Path,
        sleep: Callable[[float], None],
        stale_seconds: float = _KIMI_LOCK_STALE_SECONDS,
        retries: int = _KIMI_LOCK_RETRIES,
    ) -> None:
        self._target = home / "oauth" / "kimi-code"
        self._lock_dir = home / "oauth" / "kimi-code.lock"
        self._sleep = sleep
        self._stale_seconds = stale_seconds
        self._retries = retries
        self._held = False

    def acquire(self) -> bool:
        try:
            self._target.parent.mkdir(parents=True, exist_ok=True)
            self._target.touch(exist_ok=True)
        except OSError:
            return False
        for _ in range(self._retries):
            try:
                os.mkdir(self._lock_dir)
            except FileExistsError:
                self._break_if_stale()
            except OSError:
                return False
            else:
                self._held = True
                return True
            self._sleep(
                random.uniform(
                    _KIMI_LOCK_MIN_DELAY_SECONDS,
                    _KIMI_LOCK_MAX_DELAY_SECONDS,
                )
            )
        return False

    def _break_if_stale(self) -> None:
        try:
            mtime = self._lock_dir.stat().st_mtime
        except OSError:
            return
        if mtime >= time.time() - self._stale_seconds:
            return
        try:
            os.rmdir(self._lock_dir)
        except OSError:
            pass

    def touch(self) -> None:
        """刷新锁 mtime，避免慢请求期间被其它进程当成陈旧锁。"""

        if not self._held:
            return
        try:
            os.utime(self._lock_dir)
        except OSError:
            pass

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            os.rmdir(self._lock_dir)
        except OSError:
            pass


def _parse_kimi_usages(payload: Any, observed_at: float) -> QuotaSnapshot | None:
    """把 /usages 响应转换成额度窗口。"""

    if not isinstance(payload, Mapping):
        return None
    raw_usages = payload.get("usages")
    usages = raw_usages if isinstance(raw_usages, Mapping) else {}
    windows: list[QuotaWindow] = []
    for key, window_minutes in (
        ("limit_5h", 300.0),
        ("limit_7d", 10_080.0),
        ("limit_month_total", None),
        ("limit_month_code", None),
    ):
        raw_entry = usages.get(key)
        entry = raw_entry if isinstance(raw_entry, Mapping) else None
        if not entry:
            continue
        used_ratio = _float_value(entry.get("used_ratio"))
        if used_ratio is None:
            continue
        windows.append(
            QuotaWindow(
                limit_id="kimi",
                name=key,
                used_percent=used_ratio * 100,
                window_minutes=window_minutes,
                resets_at=_timestamp(entry.get("reset_time")),
            )
        )
    metadata = _booster_wallet_metadata(payload.get("boosterWallet"))
    if not windows and not metadata:
        return None
    return QuotaSnapshot(
        observed_at=observed_at,
        windows=tuple(windows),
        plan_type=None,
        source="kimi-api",
        raw_limit_ids=("kimi",),
        metadata=metadata,
    )


def _booster_wallet_metadata(value: Any) -> dict[str, str]:
    """提取 booster 钱包信息；结构不符时返回空。"""

    data = value if isinstance(value, Mapping) else None
    if not data:
        return {}
    raw_balance = data.get("balance")
    balance = raw_balance if isinstance(raw_balance, Mapping) else None
    if not balance or balance.get("type") != "BOOSTER":
        return {}
    amount = _int_value(balance.get("amount"))
    if amount is None or amount <= 0:
        return {}
    amount_left = _int_value(balance.get("amountLeft"))
    monthly_limit = _money_cents(data.get("monthlyChargeLimit"))
    monthly_used = _money_cents(data.get("monthlyUsed"))
    currency = "USD"
    for money in (monthly_limit, monthly_used):
        if money is not None and money[1]:
            currency = money[1]
            break
    return {
        "booster_balance_cents": str(
            _fixed_point_to_cents(amount_left) if amount_left is not None else 0
        ),
        "booster_total_cents": str(_fixed_point_to_cents(amount)),
        "booster_monthly_charge_limit_cents": str(
            monthly_limit[0] if monthly_limit is not None else 0
        ),
        "booster_monthly_used_cents": str(
            monthly_used[0] if monthly_used is not None else 0
        ),
        "booster_monthly_charge_limit_enabled": (
            "true" if data.get("monthlyChargeLimitEnabled") is True else "false"
        ),
        "booster_currency": currency,
    }


def _money_cents(value: Any) -> tuple[int, str] | None:
    data = value if isinstance(value, Mapping) else None
    if not data:
        return None
    cents = _int_value(data.get("priceInCents"))
    if cents is None:
        return None
    currency = data.get("currency")
    return cents, currency if isinstance(currency, str) else ""


def _fixed_point_to_cents(value: int) -> int:
    """官方定点数换算：1e6 对应 1 美分，0–1 美分之间按 1 美分计。"""

    cents = value / _KIMI_FIXED_POINT_CENTS
    if 0 < cents < 1:
        return 1
    return round(cents)


def _kimi_region(home: Path) -> str:
    """读取安装脚本写入的 region 标记；缺失或异常时按 mainland-cn。"""

    try:
        text = (home / "region").read_text(encoding="utf-8").strip().lower()
    except (OSError, UnicodeDecodeError):
        return "mainland-cn"
    return "global" if text == "global" else "mainland-cn"


def _kimi_base_url(home: Path) -> str:
    override = os.environ.get("KIMI_CODE_BASE_URL")
    if override and override.strip():
        return override.strip().rstrip("/")
    if _kimi_region(home) == "global":
        return _KIMI_GLOBAL_BASE_URL
    return _KIMI_MAINLAND_BASE_URL


def _kimi_oauth_host(home: Path) -> str:
    override = os.environ.get("KIMI_CODE_OAUTH_HOST") or os.environ.get(
        "KIMI_OAUTH_HOST"
    )
    if override and override.strip():
        return override.strip().rstrip("/")
    if _kimi_region(home) == "global":
        return _KIMI_GLOBAL_OAUTH_HOST
    return _KIMI_MAINLAND_OAUTH_HOST


def _float_value(value: Any) -> float | None:
    """接受数字或数字字符串（官方 ratioValue 同样兼容字符串）。"""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number):
        return None
    return number


def _int_value(value: Any) -> int | None:
    number = _float_value(value)
    if number is None:
        return None
    return int(number)


def _http_get_json(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
) -> tuple[int, Any]:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    return _open_json(request, timeout)


def _http_post_form(
    url: str,
    params: Mapping[str, str],
    timeout: float,
) -> tuple[int, Any]:
    body = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    return _open_json(request, timeout)


def _open_json(request: urllib.request.Request, timeout: float) -> tuple[int, Any]:
    """执行请求并解析 JSON；所有网络错误都收敛为 ``(0, None)``。"""

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _read_json_body(response)
    except urllib.error.HTTPError as error:
        return error.code, _read_json_body(error)
    except OSError:
        return 0, None


def _read_json_body(response: Any) -> Any:
    try:
        body = response.read(_KIMI_MAX_RESPONSE_BYTES + 1)
    except OSError:
        return None
    if len(body) > _KIMI_MAX_RESPONSE_BYTES:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
