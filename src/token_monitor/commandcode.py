"""Command Code CLI 本地身份、活动会话和订阅额度读取。

订阅额度通过与官方 CLI 相同的后台接口读取：

- ``GET /alpha/whoami?limits=1``：账号身份、组织 ID 与组织限额；
- ``GET /alpha/billing/credits``：套餐名额余额和 5 小时 / 每周窗口；
- ``GET /alpha/billing/subscriptions``：订阅状态、账期和套餐 ID；
- ``GET /alpha/usage/summary``：本账期请求数、花费和 token 合计。

接口要求 ``User-Agent`` 等 CLI 头，缺少时服务端返回 403；``auth.json`` 中的
API Key 只用于鉴权请求，不会出现在返回值、日志或异常消息中，也不会读取会话
正文或工具输出。活动会话以进程实际打开的 session JSONL 为准。
"""

from __future__ import annotations

import json
import math
import os
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


# 官方 CLI：dist/cli.mjs 中 Vt.prod / Gy 头常量 / rr 套餐表 / WindowLimitMeter 标签。
_COMMANDCODE_BASE_URL = "https://api.commandcode.ai"
_COMMANDCODE_USER_AGENT = "cli"
_COMMANDCODE_CLI_VERSION = "1.53.1"
_COMMANDCODE_CLI_ENVIRONMENT = "prod"
_COMMANDCODE_PROFILE_NAME = "command-code"

_COMMANDCODE_WHOAMI = "/alpha/whoami"
_COMMANDCODE_CREDITS = "/alpha/billing/credits"
_COMMANDCODE_SUBSCRIPTIONS = "/alpha/billing/subscriptions"
_COMMANDCODE_USAGE_SUMMARY = "/alpha/usage/summary"

_COMMANDCODE_TIMEOUT_SECONDS = 8.0
_COMMANDCODE_MAX_RESPONSE_BYTES = 1024 * 1024
_QUOTA_CACHE_SUCCESS_TTL_SECONDS = 60.0
_QUOTA_CACHE_FAILURE_TTL_SECONDS = 15.0

# 官方套餐 ID 前缀 -> 展示名；用于接口没给 planId 时的兜底。
_PLAN_NAMES: tuple[tuple[str, str], ...] = (
    ("individual-provider", "Provider"),
    ("individual-ultra", "Ultra"),
    ("individual-goat", "GOAT"),
    ("individual-pro", "Pro"),
    ("individual-max", "Max"),
    ("individual-go", "Go"),
    ("teams-pro", "Teams Pro"),
)

# 订阅状态：官方只把这三种视为有效订阅（Xr 集合）。
_ACTIVE_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing", "past_due"})

# 窗口定义：(接口字段名, 展示标签, 窗口分钟数)。标签来自官方 /usage 面板。
_WINDOW_DEFINITIONS: tuple[tuple[str, str, float], ...] = (
    ("fiveHour", "5-hour", 300.0),
    ("weekly", "Weekly", 10_080.0),
)

# 会话 JSONL 第一行的 ``type`` 值；官方 sessionStore 的写入格式。
_SESSION_HEADER_TYPE = "session"
_MAX_SESSION_HEADER_BYTES = 64 * 1024

_HttpJsonCaller = Callable[[str, Mapping[str, str], float], tuple[int, Any]]

_quota_cache: dict[Path, tuple[float, QuotaSnapshot | None]] = {}
_quota_cache_lock = threading.Lock()


@dataclass(frozen=True)
class CommandCodeAccount:
    """一个 Command Code 数据目录的安全身份信息。"""

    home: Path
    account_id: str | None
    display_name: str
    profile_name: str = _COMMANDCODE_PROFILE_NAME
    user_name: str | None = None
    email: str | None = None
    key_name: str | None = None
    logged_in: bool = False

    @property
    def account_key(self) -> str:
        """返回优先使用真实用户 ID 的归组键。"""

        return self.account_id or f"profile:{self.profile_name}"


@dataclass(frozen=True)
class CommandCodeSessionInfo:
    """从 session JSONL 头提取的工作目录和模型。"""

    session_id: str
    cwd: str | None
    model: str | None
    started_at: float | None


def default_commandcode_home() -> Path:
    """返回 Command Code 默认主目录。

    官方 CLI 固定使用 ``$HOME/.commandcode``，不识别环境变量；这里额外支持
    ``COMMANDCODE_HOME``，方便同一台机器监控多个独立数据目录。
    """

    configured = os.environ.get("COMMANDCODE_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".commandcode"


def resolve_commandcode_homes(
    homes: Sequence[Path] | None = None,
) -> tuple[Path, ...]:
    """解析要扫描的 Command Code 数据目录；未传入时若默认目录存在则使用它。"""

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
    default_home = _normalize_path(default_commandcode_home())
    if default_home.is_dir():
        return (default_home,)
    return ()


def read_commandcode_account(commandcode_home: Path) -> CommandCodeAccount:
    """读取 Command Code 身份，只读取 auth.json 的身份字段。

    ``apiKey`` 仅用于判断登录状态，不会出现在返回值中。
    """

    home = _normalize_path(commandcode_home)
    payload = _read_json_object(home / "auth.json")
    api_key = _text(payload.get("apiKey")) if payload else None
    account_id = _text(payload.get("userId")) if payload else None
    user_name = _text(payload.get("userName")) if payload else None
    email = _text(payload.get("email")) if payload else None
    logged_in = api_key is not None
    display_name = user_name or email or account_id or _COMMANDCODE_PROFILE_NAME
    return CommandCodeAccount(
        home=home,
        account_id=account_id,
        display_name=display_name,
        user_name=user_name,
        email=email,
        key_name=_text(payload.get("keyName")) if payload else None,
        logged_in=logged_in,
    )


def read_commandcode_quota(
    commandcode_home: Path,
    now: float | None = None,
    *,
    http_get_json: _HttpJsonCaller | None = None,
) -> QuotaSnapshot | None:
    """通过官方后台接口读取 Command Code 订阅额度。

    任何网络、鉴权或格式问题都返回 ``None``，绝不抛出；API Key 只用于鉴权
    请求，不会出现在返回值或异常消息中。使用真实 HTTP 时结果带缓存（成功
    60 秒、失败 15 秒），避免 Dashboard 轮询反复请求接口。
    """

    home = _normalize_path(commandcode_home)
    moment = time.time() if now is None else now
    use_cache = http_get_json is None
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
        snapshot = _fetch_commandcode_quota(
            home,
            moment,
            http_get_json=http_get_json or _http_get_json,
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


def _fetch_commandcode_quota(
    home: Path,
    now: float,
    *,
    http_get_json: _HttpJsonCaller,
) -> QuotaSnapshot | None:
    """发起官方 CLI 相同的额度请求并解析结果。"""

    payload = _read_json_object(home / "auth.json")
    api_key = _text(payload.get("apiKey")) if payload else None
    if api_key is None:
        return None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": _COMMANDCODE_USER_AGENT,
        # 缺少 CLI 版本头时服务端会返回 403，因此始终带上。
        "x-command-code-version": _COMMANDCODE_CLI_VERSION,
        "x-cli-environment": _COMMANDCODE_CLI_ENVIRONMENT,
    }
    whoami_status, whoami = http_get_json(
        f"{_COMMANDCODE_BASE_URL}{_COMMANDCODE_WHOAMI}?limits=1",
        headers,
        _COMMANDCODE_TIMEOUT_SECONDS,
    )
    if whoami_status != 200 or not isinstance(whoami, Mapping):
        return None
    if not isinstance(whoami.get("user"), Mapping):
        # 缺少用户对象通常意味着 Key 失效，交给调用方展示登录状态。
        return None
    org_id = _org_id(whoami)

    credits_status, credits = http_get_json(
        _with_org_id(_COMMANDCODE_CREDITS, org_id),
        headers,
        _COMMANDCODE_TIMEOUT_SECONDS,
    )
    if credits_status != 200 or not isinstance(credits, Mapping):
        return None
    subscription_status, subscription = http_get_json(
        _with_org_id(_COMMANDCODE_SUBSCRIPTIONS, org_id),
        headers,
        _COMMANDCODE_TIMEOUT_SECONDS,
    )
    subscription_data = (
        subscription.get("data")
        if subscription_status == 200 and isinstance(subscription, Mapping)
        else None
    )
    subscription_data = (
        subscription_data if isinstance(subscription_data, Mapping) else None
    )
    period_start = (
        subscription_data.get("currentPeriodStart")
        if subscription_data is not None
        else None
    )
    period_start_text = period_start if isinstance(period_start, str) else None
    summary_status, summary = http_get_json(
        _with_org_id(_COMMANDCODE_USAGE_SUMMARY, org_id, since=period_start_text),
        headers,
        _COMMANDCODE_TIMEOUT_SECONDS,
    )
    summary_data = (
        summary if summary_status == 200 and isinstance(summary, Mapping) else None
    )
    return _parse_commandcode_quota(
        whoami=whoami,
        credits=credits,
        subscription=subscription_data,
        summary=summary_data,
        observed_at=now,
    )


def _org_id(whoami: Mapping[str, Any]) -> str | None:
    """读取组织 ID；个人账号返回 None。"""

    org = whoami.get("org")
    if not isinstance(org, Mapping):
        return None
    return _text(org.get("id"))


def _with_org_id(
    endpoint: str,
    org_id: str | None,
    since: str | None = None,
) -> str:
    """按官方 buildUsageEndpoint 拼接查询参数，跳过空值。"""

    params: list[tuple[str, str]] = []
    if org_id is not None:
        params.append(("orgId", org_id))
    if since is not None:
        params.append(("since", since))
    if not params:
        return f"{_COMMANDCODE_BASE_URL}{endpoint}"
    query = urllib.parse.urlencode(params)
    return f"{_COMMANDCODE_BASE_URL}{endpoint}?{query}"


def _parse_commandcode_quota(
    whoami: Mapping[str, Any],
    credits: Mapping[str, Any],
    subscription: Mapping[str, Any] | None,
    summary: Mapping[str, Any] | None,
    observed_at: float,
) -> QuotaSnapshot | None:
    """把官方 /usage 四份响应转换成额度窗口。

    窗口定义与官方面板一致：5-hour、Weekly 两个硬上限窗口，以及一个带账期
    重置时间的本月订阅窗口；金额与请求数放在 metadata 中供 Dashboard 展示。
    """

    raw_window_limits = credits.get("windowLimits")
    window_limits = raw_window_limits if isinstance(raw_window_limits, Mapping) else {}
    windows: list[QuotaWindow] = []
    if window_limits.get("limited") is not False:
        for key, label, window_minutes in _WINDOW_DEFINITIONS:
            entry = window_limits.get(key)
            if not isinstance(entry, Mapping):
                continue
            used = _number(entry.get("used"))
            cap = _number(entry.get("cap"))
            if used is None or cap is None or cap <= 0:
                continue
            windows.append(
                QuotaWindow(
                    limit_id=_COMMANDCODE_PROFILE_NAME,
                    name=label,
                    used_percent=max(0.0, min(used / cap * 100.0, 100.0)),
                    window_minutes=window_minutes,
                    resets_at=_timestamp(entry.get("resetAt")),
                    reached_type="limit" if entry.get("exceeded") is True else None,
                )
            )

    metadata = _quota_metadata(
        whoami=whoami,
        credits=credits,
        subscription=subscription,
        summary=summary,
        observed_at=observed_at,
    )
    monthly_window = _monthly_credit_window(
        credits=credits,
        subscription=subscription,
        summary=summary,
    )
    if monthly_window is not None:
        windows.append(monthly_window)
    if not windows and not metadata:
        return None
    return QuotaSnapshot(
        observed_at=observed_at,
        windows=tuple(windows),
        plan_type=_plan_name(subscription) or _plan_id_from_credits(credits),
        source="command-code-api",
        raw_limit_ids=(_COMMANDCODE_PROFILE_NAME,),
        metadata=metadata,
    )


def _monthly_credit_window(
    credits: Mapping[str, Any],
    subscription: Mapping[str, Any] | None,
    summary: Mapping[str, Any] | None,
) -> QuotaWindow | None:
    """构造本月订阅窗口：已用金额占本月总名额的比例。"""

    remaining = _number(_mapping(credits.get("credits")).get("monthlyCredits")) or 0.0
    spent = _number(summary.get("totalCredits")) if summary is not None else None
    if spent is None and summary is not None:
        spent = _number(summary.get("totalCost"))
    spent = spent or 0.0
    pool = remaining + spent
    if pool <= 0:
        return None
    resets_at = (
        _timestamp(subscription.get("currentPeriodEnd"))
        if subscription is not None
        else None
    )
    return QuotaWindow(
        limit_id=_COMMANDCODE_PROFILE_NAME,
        name="monthly",
        used_percent=max(0.0, min(spent / pool * 100.0, 100.0)),
        window_minutes=None,
        resets_at=resets_at,
    )


def _quota_metadata(
    whoami: Mapping[str, Any],
    credits: Mapping[str, Any],
    subscription: Mapping[str, Any] | None,
    summary: Mapping[str, Any] | None,
    observed_at: float,
) -> dict[str, str]:
    """提取 Dashboard 需要的账号、套餐和本月金额信息。"""

    user = _mapping(whoami.get("user"))
    metadata: dict[str, str] = {}
    for key, value in (
        ("user_name", _text(user.get("userName"))),
        ("email", _text(user.get("email"))),
        ("plan_id", _plan_id_from_subscription(subscription)),
        ("subscription_status", _plan_status(subscription)),
    ):
        if value:
            metadata[key] = value

    raw_credits = _mapping(credits.get("credits"))
    credits_available = False
    for key, value in (
        ("monthly_credits_remaining", _number(raw_credits.get("monthlyCredits"))),
        ("purchased_credits_remaining", _number(raw_credits.get("purchasedCredits"))),
        ("free_credits_remaining", _number(raw_credits.get("freeCredits"))),
        ("credit_threshold", _number(raw_credits.get("creditThreshold"))),
    ):
        if value is None:
            continue
        credits_available = True
        metadata[key] = _format_credits(value)
    if credits_available:
        metadata["credits_below_threshold"] = (
            "true" if raw_credits.get("belowThreshold") is True else "false"
        )

    period_end = (
        _timestamp(subscription.get("currentPeriodEnd"))
        if subscription is not None
        else None
    )
    if period_end is not None:
        metadata["period_end"] = _iso_timestamp(period_end)
        metadata["days_left"] = str(_days_remaining(period_end, observed_at))

    if summary is not None:
        spent = _number(summary.get("totalCredits"))
        if spent is None:
            spent = _number(summary.get("totalCost"))
        if spent is not None:
            metadata["period_credits_spent"] = _format_credits(spent)
        requests = _int(summary.get("totalCount"))
        if requests is not None:
            metadata["period_requests"] = str(requests)
        for key, value in (
            ("period_tokens_in", _int(summary.get("totalTokensIn"))),
            ("period_tokens_out", _int(summary.get("totalTokensOut"))),
        ):
            if value is not None:
                metadata[key] = str(value)
    return metadata


def _plan_id_from_credits(credits: Mapping[str, Any]) -> str | None:
    """从 credits 响应的 planId 读取套餐 ID。"""

    return _text(_mapping(credits.get("credits")).get("planId"))


def _plan_id_from_subscription(
    subscription: Mapping[str, Any] | None,
) -> str | None:
    """从订阅响应读取套餐 ID。"""

    if subscription is None:
        return None
    return _text(subscription.get("planId"))


def _plan_status(subscription: Mapping[str, Any] | None) -> str | None:
    """读取订阅状态；非有效订阅返回空。"""

    if subscription is None:
        return None
    status = _text(subscription.get("status"))
    if status is None or status.lower() not in _ACTIVE_SUBSCRIPTION_STATUSES:
        return None
    return status.lower()


def _plan_name(subscription: Mapping[str, Any] | None) -> str | None:
    """按官方套餐表把 planId 转换为展示名。"""

    plan_id = _plan_id_from_subscription(subscription)
    if plan_id is None:
        return None
    normalized = plan_id.lower().replace("_", "-")
    for prefix, name in _PLAN_NAMES:
        if normalized.startswith(prefix):
            return name
    return plan_id


def list_commandcode_active_sessions(
    commandcode_home: Path,
    proc_root: Path = Path("/proc"),
    now: float | None = None,
) -> tuple[TrackedSession, ...]:
    """列出当前有 Command Code 进程打开的活动会话，不读取提示词。"""

    home = _normalize_path(commandcode_home)
    projects_root = home / "projects"
    observed_at = time.time() if now is None else float(now)
    agents = scan_running_agents(
        proc_root=proc_root,
        products=(_COMMANDCODE_PROFILE_NAME,),
    )
    grouped: dict[str, list[int]] = {}
    paths_by_session: dict[str, Path] = {}
    for agent in agents:
        for path in agent.open_paths:
            session_id = _commandcode_session_id_from_path(path, projects_root)
            if session_id is None:
                continue
            grouped.setdefault(session_id, [])
            if agent.pid not in grouped[session_id]:
                grouped[session_id].append(agent.pid)
            # 会话头只能从消息文件读取；meta.json 只作为兜底，避免抢占。
            if path.name.endswith(".jsonl"):
                paths_by_session[session_id] = path
            else:
                paths_by_session.setdefault(session_id, path)
    sessions: list[TrackedSession] = []
    for session_id, pids in grouped.items():
        jsonl_path = paths_by_session.get(session_id)
        info = read_commandcode_session_info(jsonl_path) if jsonl_path else None
        sessions.append(
            TrackedSession(
                thread_id=f"command-code:{session_id}",
                session_id=session_id,
                jsonl_path=str(jsonl_path) if jsonl_path is not None else None,
                cwd=info.cwd if info is not None else None,
                source="command-code-cli",
                status=SessionStatus.RUNNING,
                confidence=DetectionConfidence.OPEN_FILE,
                first_seen_at=(
                    info.started_at
                    if info is not None and info.started_at is not None
                    else observed_at
                ),
                last_seen_at=observed_at,
                pids=tuple(sorted(pids)),
                last_event_at=(
                    info.started_at if info is not None else None
                ),
                last_event_type=(
                    info.model if info is not None and info.model else "session"
                ),
                product="command-code",
                model=info.model if info is not None else None,
                project=info.cwd if info is not None else None,
            )
        )
    sessions.sort(key=lambda item: item.last_seen_at, reverse=True)
    return tuple(sessions)


def read_commandcode_session_info(path: Path | None) -> CommandCodeSessionInfo | None:
    """读取 session JSONL 头和相邻 meta.json，只取 cwd 与模型。

    官方 sessionStore 把工作目录写在第一行 ``type: "session"`` 记录里，模型写在
    同目录的 ``<id>.meta.json``。两者都不包含对话正文。
    """

    if path is None:
        return None
    session_id = _session_id_from_filename(path.name)
    if session_id is None:
        return None
    messages_path = (
        path if path.name.endswith(".jsonl") else path.with_name(f"{session_id}.jsonl")
    )
    header = _read_session_header(messages_path)
    model = None
    meta = _read_json_object(path.with_name(f"{session_id}.meta.json"))
    if meta is not None:
        model = _text(meta.get("model"))
    return CommandCodeSessionInfo(
        session_id=session_id,
        cwd=_text(header.get("cwd")) if header is not None else None,
        model=model,
        started_at=(
            _timestamp(header.get("timestamp")) if header is not None else None
        ),
    )


def _read_session_header(path: Path) -> Mapping[str, Any] | None:
    """只读取 JSONL 第一行，避免扫描整份会话文件。"""

    try:
        with path.open("rb") as handle:
            raw = handle.readline(_MAX_SESSION_HEADER_BYTES)
    except OSError:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping) or payload.get("type") != _SESSION_HEADER_TYPE:
        return None
    return payload


def _commandcode_session_id_from_path(path: Path, projects_root: Path) -> str | None:
    """从打开的会话文件路径提取 session ID。

    只接受 ``projects/<项目>/<session-id>.jsonl`` 消息文件及其相邻的
    ``<session-id>.meta.json``；``.checkpoints.jsonl`` 等文件会被文件名规则过滤。
    """

    try:
        relative = path.resolve().relative_to(projects_root.resolve())
    except (OSError, ValueError, RuntimeError):
        try:
            relative = path.relative_to(projects_root)
        except ValueError:
            return None
    if len(relative.parts) != 2:
        return None
    return _session_id_from_filename(relative.parts[1])


def _session_id_from_filename(name: str) -> str | None:
    """从 ``<session-id>.jsonl`` 或 ``<session-id>.meta.json`` 提取 session ID。"""

    for suffix in (".meta.json", ".jsonl"):
        if not name.endswith(suffix):
            continue
        stem = name[: -len(suffix)]
        # session ID 是单个 UUID 段；带点的文件名属于其他 sidecar。
        if not stem or "." in stem:
            return None
        return stem
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    """只接受 JSON 对象，避免把异常值当成数据。"""

    return value if isinstance(value, Mapping) else {}


def _read_json_object(path: Path) -> Mapping[str, Any] | None:
    """读取一个 JSON 对象；文件缺失或格式不符时返回 None。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _text(value: Any) -> str | None:
    """把可空字段转换为去空白的字符串。"""

    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _number(value: Any) -> float | None:
    """把可空字段转换为浮点数，布尔值不算数字。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    """把可空字段转换为整数。"""

    number = _number(value)
    return None if number is None else int(number)


def _timestamp(value: Any) -> float | None:
    """解析 Unix 毫秒、Unix 秒或 ISO 8601 时间。"""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 10_000_000_000 else number
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


def _iso_timestamp(value: float) -> str:
    """把 Unix 秒转换成官方接口使用的 ISO 8601 UTC 字符串。"""

    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _days_remaining(period_end: float, now: float) -> int:
    """按官方 getDaysRemainingFromNow 计算剩余天数。"""

    return max(0, math.ceil((period_end - now) / 86_400))


def _format_credits(value: float) -> str:
    """按官方 formatCredits 保留两位小数。"""

    return f"{value:.2f}"


def _normalize_path(path: Path) -> Path:
    """展开用户目录并尽量生成稳定的绝对路径。"""

    expanded = path.expanduser()
    try:
        return expanded.resolve()
    except (OSError, RuntimeError):
        return expanded.absolute()


def _http_get_json(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
) -> tuple[int, Any]:
    """发起一次 GET 请求并解析 JSON；错误响应返回状态码与空值。"""

    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), _read_json_body(response)
    except urllib.error.HTTPError as error:
        return int(error.code), None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


def _read_json_body(response: Any) -> Any:
    """读取受限长度的 JSON 响应体。"""

    try:
        raw = response.read(_COMMANDCODE_MAX_RESPONSE_BYTES)
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
