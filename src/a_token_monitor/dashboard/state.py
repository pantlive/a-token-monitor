"""Dashboard 状态构建：汇总额度、活动会话、用量和磁盘信息，供 /api/state 输出。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, Sequence

from ..accounts import read_codex_plan_type
from ..housekeeping import _SESSION_SPECS
from ..housekeeping import (
    HousekeepingMonitor,
)
from ..health import HealthTracker
from ..local_time import local_day_key
from ..multi_models import TrackedSession, session_view
from ..speed import SpeedTracker
from ..quota import (
    QuotaDayBaseline,
    QuotaSnapshot,
    project_quota_exhaustion,
    quota_period,
    quota_period_label,
    quota_window_duration,
)
from ..providers import PROVIDER_SPECS, ProviderSpec, home_providers
from ..provider_quota_baseline import (
    ProviderQuotaBaselineError,
    ProviderQuotaBaselineStore,
)
from ..registry import MultiSessionRegistry
from ..traffic import TrafficSnapshot, empty_traffic_snapshot
from ..usage import (
    SessionSwitchThresholds,
    UsageAggregator,
    enrich_session_views,
)


# 中文注释：结束不超过该时间的会话仍显示在会话表里，方便单独归档。
_RECENT_FINISHED_SECONDS = 24 * 3600.0
# 中文注释：页面每 5 秒刷新一次，文件发现缓存 30 秒，避免每个请求都 glob 全部目录。
_RECENT_FILE_TTL_SECONDS = 30.0


class _RecentFileCache:
    """按 provider 目录缓存最近会话文件的发现结果（含「没有文件」的负缓存）。

    缓存的是「目录 → 文件路径」本身，账号身份在每次返回时重新合并，这样
    Dashboard 热更新账号后不会继续使用旧的账号键。缓存只存在于一个 Dashboard
    实例里，测试或一次性状态构建不传缓存时完全保持原行为。
    """

    def __init__(self, ttl_seconds: float = _RECENT_FILE_TTL_SECONDS) -> None:
        self._ttl_seconds = max(0.0, float(ttl_seconds))
        self._lock = Lock()
        self._entries: dict[tuple[str, str, float], tuple[float, Path | None]] = {}

    def get(
        self,
        provider_key: str,
        home: Path,
        window_seconds: float,
        loader: Callable[[], Path | None],
    ) -> Path | None:
        """返回缓存中的最新文件；过期或不存在的条目由 ``loader`` 重新发现。"""

        key = (provider_key, str(home), float(window_seconds))
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None and time.monotonic() < cached[0]:
                return cached[1]
        value = loader()
        with self._lock:
            # 中文注释：TTL 从发现完成时开始算，慢目录不会让缓存刚写入就过期。
            self._entries[key] = (time.monotonic() + self._ttl_seconds, value)
        return value

    def clear(self) -> None:
        """清空缓存；目录热更新后测试或调用方可显式丢弃旧结果。"""

        with self._lock:
            self._entries.clear()


def _provider_session_files(spec: ProviderSpec, home: Path) -> tuple[Path, ...]:
    """返回一个 provider 目录里可采样速度的会话文件。"""

    if spec.speed_files is not None:
        return tuple(spec.speed_files(home))
    archive_spec = _SESSION_SPECS.get(spec.product_id)
    if archive_spec is None:
        return ()
    root = (
        Path(home) / archive_spec.sessions_dir
        if archive_spec.sessions_dir
        else Path(home)
    )
    return tuple(item for item in root.glob(archive_spec.pattern) if item.is_file())


def _newest_provider_file(
    spec: ProviderSpec,
    home: Path,
    moment: float,
    window_seconds: float,
) -> Path | None:
    """返回目录里窗口内最近修改的会话文件；同一文件只 stat 一次。"""

    try:
        matches = _provider_session_files(spec, home)
    except OSError:
        return None
    fresh: list[tuple[float, Path]] = []
    for item in matches:
        try:
            modified = item.stat().st_mtime
        except OSError:
            continue
        if moment - modified <= window_seconds:
            fresh.append((modified, item))
    if not fresh:
        return None
    return max(fresh, key=lambda entry: entry[0])[1]


def _recent_provider_files(
    homes: Mapping[str, Sequence[Path]] | None,
    *,
    accounts_by_home: Mapping[str, Mapping[str, str]] | None = None,
    window_seconds: float = 600.0,
    cache: _RecentFileCache | None = None,
) -> list[dict[str, str]]:
    """返回各 provider 目录里最近改动过的会话文件（只读路径，不解析内容）。

    ``accounts_by_home`` 把数据目录路径映射到账号卡片使用的身份（账号 ID /
    展示名），让这里采到的速度与会话列表里的账号对上号——否则同一份日志会以
    「账号 ID」和「产品名」两个键各采一次，概览 KPI 就会把同一个账号算两遍。

    ``homes`` 的键是配置键（如 ``commandcode``），而归档布局、会话记录和速度
    采样使用产品 ID（如 ``command-code``）；两者必须经 ``ProviderSpec`` 映射，
    不能把配置键当产品 ID 直接查。``cache`` 由 Dashboard 请求上下文传入，直接
    调用本函数时不传缓存，行为与以前一致。
    """

    if not homes:
        return []
    moment = time.time()
    candidates: list[dict[str, str]] = []
    for key, home_paths in homes.items():
        spec = PROVIDER_SPECS.get(key)
        if spec is None:
            continue
        product = spec.product_id
        for home in home_paths:
            if cache is None:
                newest = _newest_provider_file(spec, home, moment, window_seconds)
            else:
                newest = cache.get(
                    spec.key,
                    Path(home),
                    window_seconds,
                    lambda: _newest_provider_file(spec, home, moment, window_seconds),
                )
            if newest is None:
                continue
            candidate = {"product": product, "jsonl_path": str(newest)}
            candidate.update((accounts_by_home or {}).get(str(home), {}))
            candidates.append(candidate)
    return candidates


class _AccountSet:
    """Dashboard 运行期间可热替换的注册表与账号元数据集合（线程安全）。"""

    def __init__(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        account_metadata: Mapping[str, Mapping[str, str | None]],
    ) -> None:
        self._lock = Lock()
        self._registries = dict(registries)
        self._account_metadata = dict(account_metadata)
        self._provider_homes: dict[str, tuple[Path, ...]] = {}
        self._revision = 0

    def configuration(
        self,
    ) -> tuple[
        dict[str, MultiSessionRegistry],
        dict[str, Mapping[str, str | None]],
        dict[str, tuple[Path, ...]],
        int,
    ]:
        """原子读取账号和 provider 目录，供状态缓存判定配置是否变化。"""

        with self._lock:
            return (
                dict(self._registries),
                dict(self._account_metadata),
                dict(self._provider_homes),
                self._revision,
            )

    def provider_homes(self) -> dict[str, tuple[Path, ...]]:
        """读取非 Codex 数据目录的当前快照，避免请求闭包保留启动配置。"""

        with self._lock:
            return dict(self._provider_homes)

    def update_homes(self, homes: Mapping[str, Sequence[Path]]) -> None:
        """原子替换所有 provider 的目录集合。"""

        with self._lock:
            self._provider_homes = {key: tuple(value) for key, value in homes.items()}
            self._revision += 1

    def snapshot(
        self,
    ) -> tuple[
        dict[str, MultiSessionRegistry],
        dict[str, Mapping[str, str | None]],
    ]:
        """返回当前生效的注册表与元数据副本，保证单次请求读到一致的组合。"""

        with self._lock:
            return dict(self._registries), dict(self._account_metadata)

    def update(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        account_metadata: Mapping[str, Mapping[str, str | None]] | None,
    ) -> None:
        """原子替换注册表与账号元数据；之后的请求立即使用新账号集合。"""

        with self._lock:
            self._registries = dict(registries)
            self._account_metadata = dict(account_metadata or {})
            self._revision += 1


def _visible_sessions(sessions: Sequence[TrackedSession]) -> list[TrackedSession]:
    """返回会话表要展示的会话：活动会话 + 最近结束但仍可归档的会话。"""

    now = time.time()
    visible: list[TrackedSession] = []
    for session in sessions:
        if session.is_active:
            visible.append(session)
            continue
        if not session.jsonl_path:
            continue
        # 中文注释：刚结束的会话留在表里，方便直接归档单个会话。
        if now - float(session.last_seen_at or 0.0) <= _RECENT_FINISHED_SECONDS:
            visible.append(session)
    return visible


def build_dashboard_state(
    registry: MultiSessionRegistry,
    account_name: str = "codex",
    account_id: str | None = None,
    profile_name: str | None = None,
    codex_home: str | None = None,
) -> dict[str, Any]:
    """读取 SQLite 并构造不包含提示词的 Dashboard 数据。"""

    now = time.time()
    quota = registry.load_quota()
    # 中文注释：今日段依赖本地自然日的基线；只读当天，跨日后的旧行不参与计算。
    quota_day_baselines = (
        registry.load_quota_day_baseline(local_day_key(now))
        if quota is not None
        else {}
    )
    all_sessions = registry.list_sessions(active_only=False)
    sessions = _visible_sessions(all_sessions)
    status_counts: dict[str, int] = {}
    for session in sessions:
        status_counts[session.status.value] = (
            status_counts.get(session.status.value, 0) + 1
        )
    active_sessions = [item for item in sessions if item.is_active]
    status_counts["active"] = len(active_sessions)
    status_counts["recent"] = len(sessions) - len(active_sessions)
    status_counts["process_backed"] = sum(
        session.is_process_backed for session in active_sessions
    )
    return {
        "updated_at": now,
        "account": account_name,
        "account_id": account_id,
        "profile_name": profile_name or account_name,
        "codex_home": codex_home,
        "quota": _quota_summary(quota, quota_day_baselines, now),
        "counts": status_counts,
        "sessions": [
            session_view(
                session,
                account_name,
                account_id=account_id,
                profile_name=profile_name,
                codex_home=codex_home,
            )
            for session in sessions
        ],
    }


def _guard_provider(
    label: str,
    home: Path,
    health: HealthTracker | None = None,
    provider_key: str | None = None,
    error: BaseException | None = None,
) -> None:
    """记录单个 provider 目录的读取失败，继续构建其他 provider 的状态。"""

    if health is not None and provider_key is not None and error is not None:
        health.record_failure(f"provider:{provider_key}", error)
    logging.getLogger(__name__).exception(
        "%s 目录读取失败，已跳过该目录（其他 provider 不受影响）: %s",
        label,
        home,
    )


def _record_provider_success(
    health: HealthTracker | None,
    provider_key: str,
) -> None:
    """provider 读取成功后登记健康状态；无 tracker 时跳过。"""

    if health is not None:
        health.record_success(f"provider:{provider_key}")


def _profile_plan_type(metadata: Mapping[str, object]) -> str | None:
    """返回一个 profile 的订阅类型。

    优先用扫描目录元数据里已有的值；缺失时按 ``CODEX_HOME`` 读一次
    ``auth.json``（读取按 mtime 缓存，不会每 5 秒重复解析）。Grok / Kimi /
    Command Code 等 provider 的订阅类型由各自的额度快照提供。
    """

    codex_home = metadata.get("codex_home")
    if isinstance(codex_home, str) and codex_home.strip():
        # 中文注释：auth.json 是 Codex 自己写入的，优先级高于账号构造时的快照，
        # 这样续费或换号后不必重启 daemon（读取按 mtime 缓存）。
        live = read_codex_plan_type(Path(codex_home))
        if live:
            return live
    declared = metadata.get("plan_type")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    return None


def build_multi_dashboard_state(
    registries: Mapping[str, MultiSessionRegistry],
    account_metadata: Mapping[str, Mapping[str, str | None]] | None = None,
    usage_aggregator: UsageAggregator | None = None,
    homes: Mapping[str, Sequence[Path]] | None = None,
    budget_usd: float | None = None,
    traffic: TrafficSnapshot | None = None,
    health: HealthTracker | None = None,
    speed: SpeedTracker | None = None,
    recent_files: _RecentFileCache | None = None,
    *,
    quota_baselines: ProviderQuotaBaselineStore | None = None,
) -> dict[str, Any]:
    """合并多个账号状态，同时保留每个账号独立的额度快照。"""

    metadata_by_profile = account_metadata or {}
    account_states: list[dict[str, Any]] = []
    for profile_name, registry in registries.items():
        profile_metadata = metadata_by_profile.get(profile_name, {})
        account_id = profile_metadata.get("account_id")
        display_name = account_id or profile_name
        plan_type = _profile_plan_type(profile_metadata)
        prepared = build_dashboard_state(
            registry,
            account_name=display_name,
            account_id=account_id,
            profile_name=profile_metadata.get(
                "profile_name",
                profile_name,
            ),
            codex_home=profile_metadata.get("codex_home"),
        )
        # 中文注释：把订阅类型挂在各自的 state 上。第二个循环按账号遍历，
        # 若在这里依赖外层变量，多账号时会全部用成最后一个 profile 的套餐
        # （曾经因此把两个 Codex 账号都显示成同一个套餐）。
        prepared["plan_type"] = plan_type
        account_states.append(prepared)
    quotas: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    accounts_by_key: dict[str, dict[str, Any]] = {}
    quota_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for state in account_states:
        account_name = str(state["account"])
        account_id = state.get("account_id")
        profile_name = str(state.get("profile_name") or account_name)
        codex_home = state.get("codex_home")
        plan_type = state.get("plan_type")
        account_key = str(account_id or f"profile:{profile_name}")
        quota = state.get("quota")
        if isinstance(quota, dict):
            quota_with_account = dict(quota)
            quota_with_account["account"] = account_name
            quota_with_account["account_id"] = account_id
            quota_with_account["profile_name"] = profile_name
            quota_with_account["codex_home"] = codex_home
            if not quota_with_account.get("plan_type") and plan_type:
                quota_with_account["plan_type"] = plan_type
            quota_key = (account_key, "snapshot", "snapshot")
            previous_quota = quota_by_key.get(quota_key)
            if previous_quota is None or float(
                quota_with_account.get("observed_at", 0)
            ) >= float(previous_quota.get("observed_at", 0)):
                quota_by_key[quota_key] = quota_with_account
        state_sessions = state.get("sessions")
        if isinstance(state_sessions, list):
            sessions.extend(item for item in state_sessions if isinstance(item, dict))
        account = accounts_by_key.setdefault(
            account_key,
            {
                "name": account_name,
                "account_id": account_id,
                "plan_type": plan_type,
                "profiles": [],
                "quota": None,
                "counts": {},
            },
        )
        if plan_type and not account.get("plan_type"):
            account["plan_type"] = plan_type
        profile = {
            "name": profile_name,
            "codex_home": codex_home,
        }
        if profile not in account["profiles"]:
            account["profiles"].append(profile)

    # 会话记录中的 account_id 优先级高于当前 profile 的 ID，避免 profile
    # 重新登录后把旧账号的活动会话和额度归到新账号下面。
    for record in sessions:
        record_account_id = _record_account_id(record)
        record_profile_name = str(record.get("profile_name") or "codex")
        record_codex_home = record.get("codex_home")
        account_key = _record_account_key(record)
        account = accounts_by_key.setdefault(
            account_key,
            {
                "name": _record_account_name(record),
                "account_id": record_account_id,
                "profiles": [],
                "quota": None,
                "counts": {},
            },
        )
        profile = {
            "name": record_profile_name,
            "codex_home": record_codex_home,
        }
        if profile not in account["profiles"]:
            account["profiles"].append(profile)

    # 中文注释：登记了账号读取的 provider（Claude Code、Command Code、DSH、Grok、
    # Kimi）各自成为账号卡片；额度经各家官方接口读取（带缓存），失败时只展示身份。
    account_keys_by_home: dict[str, dict[str, str]] = {}
    for spec in home_providers():
        if spec.read_account is None:
            continue
        for home in (homes or {}).get(spec.key, ()):
            if not home.is_dir():
                continue
            try:
                identity = _merge_provider_home(
                    spec, home, accounts_by_key, quota_by_key, sessions,
                    quota_baselines=quota_baselines,
                )
                if identity:
                    account_keys_by_home[str(home)] = identity
                _record_provider_success(health, spec.product_id)
            except Exception as error:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
                _guard_provider(
                    spec.display_name,
                    home,
                    health=health,
                    provider_key=spec.product_id,
                    error=error,
                )
    counts: dict[str, int] = {}
    for account in accounts_by_key.values():
        account["counts"] = {}
    for session in sessions:
        account = accounts_by_key[_record_account_key(session)]
        status = session.get("status")
        if isinstance(status, str) and status:
            account["counts"][status] = account["counts"].get(status, 0) + 1
            counts[status] = counts.get(status, 0) + 1
        # 中文注释：会话表里也会列出最近结束的会话，但它们不计入活动数。
        if session.get("active", True):
            account["counts"]["active"] = account["counts"].get("active", 0) + 1
            counts["active"] = counts.get("active", 0) + 1
            if session.get("process_backed"):
                account["counts"]["process_backed"] = (
                    account["counts"].get("process_backed", 0) + 1
                )
                counts["process_backed"] = counts.get("process_backed", 0) + 1
        else:
            account["counts"]["recent"] = account["counts"].get("recent", 0) + 1
            counts["recent"] = counts.get("recent", 0) + 1

    quotas = list(quota_by_key.values())
    accounts = list(accounts_by_key.values())
    for account_key, account in accounts_by_key.items():
        account["quota"] = quota_by_key.get((account_key, "snapshot", "snapshot"))
    usage = (
        usage_aggregator.snapshot(
            registries,
            account_metadata=metadata_by_profile,
        )
        if usage_aggregator is not None
        else UsageAggregator.empty_snapshot()
    )
    # 中文注释：实时速度只从本地会话日志采样（每次状态刷新一次），不读提示词。
    if speed is not None:
        speed.sample_sessions(sessions)
        # 中文注释：Claude / Grok / Kimi / DSH / Command Code 的会话不一定在
        # ``sessions`` 里（没有活动进程就不入表），按 provider 目录直接找最近
        # 改动的会话文件补采，否则这些账号永远没有速度。补采必须沿用账号卡片
        # 的归组键，不然同一份日志会被当成两个账号重复计入概览合计。
        speed.sample_sessions(
            _recent_provider_files(
                homes,
                accounts_by_home=account_keys_by_home,
                cache=recent_files,
            )
        )
        speed_state = speed.snapshot()
        product_counts: dict[str, int] = {}
        for account in accounts:
            product = str(account.get("product") or "")
            if product:
                product_counts[product] = product_counts.get(product, 0) + 1
        for account in accounts:
            account["speed"] = _account_speed(
                speed_state["accounts"], account, product_counts
            )
    else:
        speed_state = {
            "sampled_at": time.time(),
            "window_seconds": 0,
            "accounts": {},
            "totals": {"tps": 0.0, "accounts": 0, "live_accounts": 0, "ttft_ms": None},
        }
    return {
        "updated_at": time.time(),
        "accounts": accounts,
        "speed": speed_state,
        # 保留单账号旧字段，新的页面使用 quotas 以免混淆不同账号。
        "quota": (quotas[0] if len(registries) == 1 and len(quotas) == 1 else None),
        "quotas": quotas,
        "counts": counts,
        "sessions": sessions,
        "usage": usage,
        "budget_usd": budget_usd,
        "traffic": (
            traffic.to_dict()
            if traffic is not None
            else empty_traffic_snapshot().to_dict()
        ),
    }


def _account_speed(
    speeds: Mapping[str, dict[str, Any]],
    account: Mapping[str, Any],
    product_counts: Mapping[str, int],
) -> dict[str, Any] | None:
    """按账号卡片上的身份找到它的速度。

    速度的键来自会话记录（通常是账号 ID）；卡片上有 ``account_id`` 和展示名，
    两个都要试。产品名只在没有别的账号用该产品时兜底，避免多个 Claude/Kimi
    目录互相串数据。
    """

    name = str(account.get("name") or "")
    account_id = str(account.get("account_id") or "")
    for key in (account_id, name):
        if key and key in speeds:
            return speeds[key]
    product = str(account.get("product") or "")
    if product and product_counts.get(product, 0) <= 1 and product in speeds:
        return speeds[product]
    return None


def _merge_provider_home(
    spec: ProviderSpec,
    home: Path,
    accounts_by_key: dict[str, dict[str, Any]],
    quota_by_key: dict[tuple[str, str, str], dict[str, Any]],
    sessions: list[dict[str, Any]],
    *,
    quota_baselines: ProviderQuotaBaselineStore | None = None,
) -> dict[str, str]:
    """把一个 provider 数据目录的账号、额度和活动会话并入 Dashboard 状态。

    返回该目录对应的速度身份（账号 ID 与展示名），供速度采样复用同一账号键。
    """

    product = spec.product_id
    if spec.read_account is None:
        return {}
    provider_account = spec.read_account(home)
    provider_quota = spec.read_quota(home) if spec.read_quota is not None else None
    account_key = provider_account.account_key
    account = accounts_by_key.setdefault(
        account_key,
        {
            "name": provider_account.display_name,
            "account_id": provider_account.account_id,
            "product": product,
            "profiles": [],
            "quota": None,
            "counts": {},
        },
    )
    account["product"] = product
    profile = {
        "name": provider_account.profile_name,
        "codex_home": str(home),
    }
    if profile not in account["profiles"]:
        account["profiles"].append(profile)
    if provider_quota is not None:
        baselines = None
        if quota_baselines is not None:
            try:
                baselines = quota_baselines.observe(
                    product, account_key, provider_quota
                )
            except ProviderQuotaBaselineError:
                # 中文注释：基线存储失败只隐藏今日段，账号与当前额度仍正常展示。
                logging.getLogger(__name__).warning(
                    "%s 今日额度基线不可用，已降级", spec.display_name
                )
        quota_with_account = _quota_summary(provider_quota, baselines, time.time())
        if quota_with_account is not None:
            quota_with_account["account"] = provider_account.display_name
            quota_with_account["account_id"] = provider_account.account_id
            quota_with_account["profile_name"] = provider_account.profile_name
            quota_with_account["codex_home"] = str(home)
            quota_with_account["product"] = product
            quota_by_key[(account_key, "snapshot", "snapshot")] = quota_with_account
    if spec.active_sessions is None:
        return _provider_speed_identity(provider_account)
    for session in spec.active_sessions(home):
        sessions.append(
            session_view(
                session,
                account_name=provider_account.display_name,
                account_id=provider_account.account_id,
                profile_name=provider_account.profile_name,
                codex_home=str(home),
                product=product,
            )
        )
    return _provider_speed_identity(provider_account)


def _provider_speed_identity(provider_account: Any) -> dict[str, str]:
    """返回速度采样要用的账号身份：账号 ID 优先，展示名兜底。"""

    identity: dict[str, str] = {}
    account_id = getattr(provider_account, "account_id", None)
    display_name = getattr(provider_account, "display_name", None)
    if isinstance(account_id, str) and account_id:
        identity["account_id"] = account_id
    if isinstance(display_name, str) and display_name:
        identity["account"] = display_name
    return identity


def _record_account_id(record: Mapping[str, Any]) -> str | None:
    """读取会话摘要中已经持久化的真实账号 ID。"""

    value = record.get("account_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _record_account_key(record: Mapping[str, Any]) -> str:
    """生成会话记录的稳定账号归组键。"""

    account_id = _record_account_id(record)
    if account_id is not None:
        return account_id
    profile_name = record.get("profile_name")
    if isinstance(profile_name, str) and profile_name.strip():
        return f"profile:{profile_name.strip()}"
    account_name = record.get("account")
    if isinstance(account_name, str) and account_name.strip():
        return f"profile:{account_name.strip()}"
    return "profile:codex"


def _record_account_name(record: Mapping[str, Any]) -> str:
    """读取会话摘要适合展示的账号名称。"""

    account_id = _record_account_id(record)
    if account_id is not None:
        return account_id
    account_name = record.get("account")
    if isinstance(account_name, str) and account_name.strip():
        return account_name.strip()
    return "codex"


def _quota_resets_match(left: float | None, right: float | None) -> bool:
    """判断窗口重置时间与基线是否属于同一周期（容差 1 秒）。"""

    if left is None or right is None:
        return left is None and right is None
    return abs(left - right) <= 1.0


def _quota_summary(
    snapshot: QuotaSnapshot | None,
    day_baselines: Mapping[tuple[str, str], QuotaDayBaseline] | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """把额度快照转换成网页需要的安全字段。

    ``day_baselines`` 与 ``now`` 由 Dashboard 状态构建传入；只有快照属于 ``now``
    所在的本地自然日、重置周期与基线一致时，才输出今日段。其他调用方（例如
    未配置基线存储的 provider）不传基线，字段保持 ``None``，前端继续画单色进度条。
    """

    if snapshot is None:
        return None
    same_local_day = (
        now is not None
        and local_day_key(snapshot.observed_at) == local_day_key(now)
    )
    windows: list[dict[str, Any]] = []
    for window in snapshot.windows:
        # 中文注释：预测固定使用快照的观察时间，页面刷新时不会因为「现在」变化
        # 而让同一份额度数据反复改口。
        projection = project_quota_exhaustion(window, snapshot.observed_at)
        day_start_used: float | None = None
        today_used: float | None = None
        baseline = (
            day_baselines.get((window.limit_id, window.name))
            if day_baselines
            else None
        )
        if (
            baseline is not None
            and same_local_day
            and _quota_resets_match(window.resets_at, baseline.resets_at)
            and window.used_percent is not None
        ):
            day_start_used = baseline.used_percent
            today_used = max(
                0.0,
                min(
                    window.used_percent,
                    100.0,
                    window.used_percent - baseline.used_percent,
                ),
            )
        windows.append(
            {
                "limit_id": window.limit_id,
                "name": window.name,
                "used_percent": window.used_percent,
                "window_minutes": window.window_minutes,
                "resets_at": window.resets_at,
                "is_exhausted": window.is_exhausted,
                # 中文注释：统一周期口径在这里定死，面板只按 period 画固定行，
                # 不再依赖上游五花八门的窗口名（primary / limit_month_total / 5-hour…）。
                "period": quota_period(window),
                "period_label": quota_period_label(quota_period(window)),
                "duration_label": quota_window_duration(window),
                "projection_status": projection.status,
                "projected_exhausted_at": projection.projected_exhausted_at,
                # 中文注释：今日段 = 当前已用 - 本地日基线；缺失时不画第二段。
                "day_start_used_percent": day_start_used,
                "today_used_percent": today_used,
            }
        )
    return {
        "observed_at": snapshot.observed_at,
        "plan_type": snapshot.plan_type,
        "source": snapshot.source,
        "metadata": dict(snapshot.metadata),
        "windows": windows,
    }


def _housekeeping_summary(
    monitor: HousekeepingMonitor | None,
) -> dict[str, Any]:
    """返回 /api/state 使用的紧凑磁盘摘要，避免每 5 秒回传完整目录树。"""

    if monitor is None:
        return {"available": False, "totals": {}, "reminders": [], "preview": None}
    report = monitor.latest()
    if report.get("observed_at") is None:
        # 中文注释：首次访问时补一次扫描；refresh 自带刷新间隔节流。
        try:
            report = monitor.refresh()
        except (OSError, ValueError):
            # 中文注释：磁盘扫描失败不应击穿整个 /api/state，降级为不可用摘要。
            logger = logging.getLogger(__name__)
            logger.exception("Dashboard 磁盘摘要刷新失败，已降级")
            return {
                "available": False,
                "totals": {},
                "reminders": [],
                "preview": None,
            }
    return {
        "available": True,
        "observed_at": report.get("observed_at"),
        "thresholds": report.get("thresholds", {}),
        "archive_dir": report.get("archive_dir"),
        "totals": report.get("totals", {}),
        "reminders": list(report.get("reminders") or []),
        "preview": report.get("preview"),
        "directories": [
            {
                "label": item.get("label"),
                "path": item.get("path"),
                "bytes": item.get("bytes"),
                "files": item.get("files"),
                "session_bytes": item.get("session_bytes"),
                "session_files": item.get("session_files"),
                "cleanable": item.get("cleanable"),
                "top_children": list(item.get("top_children") or [])[:4],
            }
            for item in report.get("directories", [])
        ],
    }


def _usage_index_summary(
    aggregator: UsageAggregator | None,
) -> dict[str, Any]:
    """返回用量索引的紧凑摘要，供折叠状态下的用量检索分区展示。"""

    if aggregator is None:
        return {
            "available": False,
            "records": 0,
            "sessions": 0,
            "models": 0,
            "accounts": 0,
            "first_at": None,
            "last_at": None,
        }
    try:
        facets = aggregator.usage_facets()
    except (OSError, ValueError):
        return {
            "available": False,
            "records": 0,
            "sessions": 0,
            "models": 0,
            "accounts": 0,
            "first_at": None,
            "last_at": None,
        }
    return {
        "available": bool(facets.get("available")),
        "records": int(facets.get("records") or 0),
        "sessions": int(facets.get("sessions") or 0),
        "models": len(facets.get("models") or ()),
        "accounts": len(facets.get("accounts") or ()),
        "first_at": facets.get("first_at"),
        "last_at": facets.get("last_at"),
    }


def _attach_session_archive(
    state: dict[str, Any],
    monitor: HousekeepingMonitor | None,
) -> None:
    """给活动会话标注能否单独归档，供会话表里的「归档」按钮使用。"""

    sessions = state.get("sessions")
    if monitor is None or not isinstance(sessions, list):
        return
    paths = [
        str(item.get("jsonl_path"))
        for item in sessions
        if isinstance(item, dict) and item.get("jsonl_path")
    ]
    if not paths:
        return
    try:
        states = monitor.session_archive_state(paths)
    except (OSError, ValueError):
        return
    for session in sessions:
        if not isinstance(session, dict):
            continue
        raw = session.get("jsonl_path")
        if not raw:
            continue
        entry = states.get(str(raw))
        if entry is not None:
            session["archive"] = entry


def _attach_session_advice(
    state: dict[str, Any],
    aggregator: UsageAggregator | None,
    thresholds: SessionSwitchThresholds,
) -> None:
    """把统一会话视图的 token 与长会话提醒并入 /api/state。"""

    sessions = state.get("sessions")
    views = [
        item for item in sessions if isinstance(item, dict)
    ] if isinstance(sessions, list) else []
    _, reminders = enrich_session_views(views, aggregator, thresholds)
    state["session_advice"] = {
        "thresholds": thresholds.to_dict(),
        "count": len(reminders),
        "sessions": reminders,
    }
