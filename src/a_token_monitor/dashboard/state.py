"""Dashboard 状态构建：汇总额度、活动会话、用量和磁盘信息，供 /api/state 输出。"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, Sequence

from ..accounts import read_codex_plan_type
from ..housekeeping import (
    HousekeepingMonitor,
)
from ..claude import (
    list_claude_active_sessions,
    read_claude_account,
    read_claude_quota,
)
from ..commandcode import (
    list_commandcode_active_sessions,
    read_commandcode_account,
    read_commandcode_quota,
)
from ..dsh import (
    list_dsh_active_sessions,
    read_dsh_account,
    read_dsh_quota,
)
from ..grok import (
    list_grok_active_sessions,
    read_grok_account,
    read_grok_quota,
)
from ..health import HealthTracker
from ..kimi import (
    list_kimi_active_sessions,
    read_kimi_account,
    read_kimi_quota,
)
from ..multi_models import TrackedSession, session_view
from ..quota import (
    QuotaSnapshot,
    quota_period,
    quota_period_label,
    quota_window_duration,
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

    quota = registry.load_quota()
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
        "updated_at": time.time(),
        "account": account_name,
        "account_id": account_id,
        "profile_name": profile_name or account_name,
        "codex_home": codex_home,
        "quota": _quota_summary(quota),
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
    grok_homes: Sequence[Path] | None = None,
    kimi_homes: Sequence[Path] | None = None,
    dsh_homes: Sequence[Path] | None = None,
    commandcode_homes: Sequence[Path] | None = None,
    claude_homes: Sequence[Path] | None = None,
    budget_usd: float | None = None,
    traffic: TrafficSnapshot | None = None,
    health: HealthTracker | None = None,
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

    for grok_home in grok_homes or ():
        if not grok_home.is_dir():
            continue
        try:
            grok_account = read_grok_account(grok_home)
            grok_quota = read_grok_quota(grok_home)
            account_key = grok_account.account_key
            account = accounts_by_key.setdefault(
                account_key,
                {
                    "name": grok_account.display_name,
                    "account_id": grok_account.account_id,
                    "product": "grok",
                    "profiles": [],
                    "quota": None,
                    "counts": {},
                },
            )
            account["product"] = "grok"
            profile = {
                "name": grok_account.profile_name,
                "codex_home": str(grok_home),
            }
            if profile not in account["profiles"]:
                account["profiles"].append(profile)
            if grok_quota is not None:
                quota_with_account = _quota_summary(grok_quota)
                if quota_with_account is not None:
                    quota_with_account["account"] = grok_account.display_name
                    quota_with_account["account_id"] = grok_account.account_id
                    quota_with_account["profile_name"] = grok_account.profile_name
                    quota_with_account["codex_home"] = str(grok_home)
                    quota_with_account["product"] = "grok"
                    quota_by_key[(account_key, "snapshot", "snapshot")] = (
                        quota_with_account
                    )
            for session in list_grok_active_sessions(grok_home):
                sessions.append(
                    session_view(
                        session,
                        account_name=grok_account.display_name,
                        account_id=grok_account.account_id,
                        profile_name=grok_account.profile_name,
                        codex_home=str(grok_home),
                        product="grok",
                    )
                )
            _record_provider_success(health, 'grok')
        except Exception as error:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider(
                'Grok',
                grok_home,
                health=health,
                provider_key='grok',
                error=error,
            )
    # Kimi 配额经官方 /usages 接口读取（带缓存）；失败时账号卡片只展示身份。
    for kimi_home in kimi_homes or ():
        if not kimi_home.is_dir():
            continue
        try:
            kimi_account = read_kimi_account(kimi_home)
            kimi_quota = read_kimi_quota(kimi_home)
            account_key = kimi_account.account_key
            account = accounts_by_key.setdefault(
                account_key,
                {
                    "name": kimi_account.display_name,
                    "account_id": kimi_account.account_id,
                    "product": "kimi",
                    "profiles": [],
                    "quota": None,
                    "counts": {},
                },
            )
            account["product"] = "kimi"
            profile = {
                "name": kimi_account.profile_name,
                "codex_home": str(kimi_home),
            }
            if profile not in account["profiles"]:
                account["profiles"].append(profile)
            if kimi_quota is not None:
                quota_with_account = _quota_summary(kimi_quota)
                if quota_with_account is not None:
                    quota_with_account["account"] = kimi_account.display_name
                    quota_with_account["account_id"] = kimi_account.account_id
                    quota_with_account["profile_name"] = kimi_account.profile_name
                    quota_with_account["codex_home"] = str(kimi_home)
                    quota_with_account["product"] = "kimi"
                    quota_by_key[(account_key, "snapshot", "snapshot")] = (
                        quota_with_account
                    )
            for session in list_kimi_active_sessions(kimi_home):
                sessions.append(
                    session_view(
                        session,
                        account_name=kimi_account.display_name,
                        account_id=kimi_account.account_id,
                        profile_name=kimi_account.profile_name,
                        codex_home=str(kimi_home),
                        product="kimi",
                    )
                )
            _record_provider_success(health, 'kimi')
        except Exception as error:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider(
                'Kimi',
                kimi_home,
                health=health,
                provider_key='kimi',
                error=error,
            )
    for dsh_home in dsh_homes or ():
        if not dsh_home.is_dir():
            continue
        try:
            dsh_account = read_dsh_account(dsh_home)
            dsh_quota = read_dsh_quota(dsh_home)
            account_key = dsh_account.account_key
            account = accounts_by_key.setdefault(
                account_key,
                {
                    "name": dsh_account.display_name,
                    "account_id": dsh_account.account_id,
                    "product": "dsh",
                    "profiles": [],
                    "quota": None,
                    "counts": {},
                },
            )
            account["product"] = "dsh"
            profile = {
                "name": dsh_account.profile_name,
                "codex_home": str(dsh_home),
            }
            if profile not in account["profiles"]:
                account["profiles"].append(profile)
            if dsh_quota is not None:
                quota_with_account = _quota_summary(dsh_quota)
                if quota_with_account is not None:
                    quota_with_account["account"] = dsh_account.display_name
                    quota_with_account["account_id"] = dsh_account.account_id
                    quota_with_account["profile_name"] = dsh_account.profile_name
                    quota_with_account["codex_home"] = str(dsh_home)
                    quota_with_account["product"] = "dsh"
                    quota_by_key[(account_key, "snapshot", "snapshot")] = (
                        quota_with_account
                    )
            for session in list_dsh_active_sessions(dsh_home):
                sessions.append(
                    session_view(
                        session,
                        account_name=dsh_account.display_name,
                        account_id=dsh_account.account_id,
                        profile_name=dsh_account.profile_name,
                        codex_home=str(dsh_home),
                        product="dsh",
                    )
                )
            _record_provider_success(health, 'dsh')
        except Exception as error:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider(
                'DeepSeek Harness',
                dsh_home,
                health=health,
                provider_key='dsh',
                error=error,
            )
    # Claude Code 订阅额度经 OAuth usage 接口读取（带缓存）；失败时只展示账号身份。
    for claude_home in claude_homes or ():
        if not claude_home.is_dir():
            continue
        try:
            claude_account = read_claude_account(claude_home)
            claude_quota = read_claude_quota(claude_home)
            account_key = claude_account.account_key
            account = accounts_by_key.setdefault(
                account_key,
                {
                    "name": claude_account.display_name,
                    "account_id": claude_account.account_id,
                    "product": "claude",
                    "profiles": [],
                    "quota": None,
                    "counts": {},
                },
            )
            account["product"] = "claude"
            profile = {
                "name": claude_account.profile_name,
                "codex_home": str(claude_home),
            }
            if profile not in account["profiles"]:
                account["profiles"].append(profile)
            if claude_quota is not None:
                quota_with_account = _quota_summary(claude_quota)
                if quota_with_account is not None:
                    quota_with_account["account"] = claude_account.display_name
                    quota_with_account["account_id"] = claude_account.account_id
                    quota_with_account["profile_name"] = (
                        claude_account.profile_name
                    )
                    quota_with_account["codex_home"] = str(claude_home)
                    quota_with_account["product"] = "claude"
                    quota_by_key[(account_key, "snapshot", "snapshot")] = (
                        quota_with_account
                    )
            for session in list_claude_active_sessions(claude_home):
                sessions.append(
                    session_view(
                        session,
                        claude_account.display_name,
                        account_id=claude_account.account_id,
                        profile_name=claude_account.profile_name,
                        codex_home=str(claude_home),
                    )
                )
            _record_provider_success(health, 'claude')
        except Exception as error:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider(
                'Claude Code',
                claude_home,
                health=health,
                provider_key='claude',
                error=error,
            )
    # Command Code 订阅额度经官方后台接口读取（带缓存）；失败时只展示账号身份。
    for commandcode_home in commandcode_homes or ():
        if not commandcode_home.is_dir():
            continue
        try:
            commandcode_account = read_commandcode_account(commandcode_home)
            commandcode_quota = read_commandcode_quota(commandcode_home)
            account_key = commandcode_account.account_key
            account = accounts_by_key.setdefault(
                account_key,
                {
                    "name": commandcode_account.display_name,
                    "account_id": commandcode_account.account_id,
                    "product": "command-code",
                    "profiles": [],
                    "quota": None,
                    "counts": {},
                },
            )
            account["product"] = "command-code"
            profile = {
                "name": commandcode_account.profile_name,
                "codex_home": str(commandcode_home),
            }
            if profile not in account["profiles"]:
                account["profiles"].append(profile)
            if commandcode_quota is not None:
                quota_with_account = _quota_summary(commandcode_quota)
                if quota_with_account is not None:
                    quota_with_account["account"] = commandcode_account.display_name
                    quota_with_account["account_id"] = commandcode_account.account_id
                    quota_with_account["profile_name"] = (
                        commandcode_account.profile_name
                    )
                    quota_with_account["codex_home"] = str(commandcode_home)
                    quota_with_account["product"] = "command-code"
                    quota_by_key[(account_key, "snapshot", "snapshot")] = (
                        quota_with_account
                    )
            for session in list_commandcode_active_sessions(commandcode_home):
                sessions.append(
                    session_view(
                        session,
                        account_name=commandcode_account.display_name,
                        account_id=commandcode_account.account_id,
                        profile_name=commandcode_account.profile_name,
                        codex_home=str(commandcode_home),
                        product="command-code",
                    )
                )
            _record_provider_success(health, 'command-code')
        except Exception as error:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider(
                'Command Code',
                commandcode_home,
                health=health,
                provider_key='command-code',
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
    return {
        "updated_at": time.time(),
        "accounts": accounts,
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


def _quota_summary(snapshot: QuotaSnapshot | None) -> dict[str, Any] | None:
    """把额度快照转换成网页需要的安全字段。"""

    if snapshot is None:
        return None
    return {
        "observed_at": snapshot.observed_at,
        "plan_type": snapshot.plan_type,
        "source": snapshot.source,
        "metadata": dict(snapshot.metadata),
        "windows": [
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
            }
            for window in snapshot.windows
        ],
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
