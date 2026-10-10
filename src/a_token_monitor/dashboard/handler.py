"""Dashboard HTTP 请求处理器。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from threading import Lock
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

from ..alerts import (
    AlertStoreError,
    TrafficAlertStore,
)
from ..alert_context import configured_alert_context_roots, load_alert_context
from ..discovery import default_session_root
from ..i18n import localize_payload, resolve_language
from ..housekeeping import (
    CleanupCriteria,
    HousekeepingError,
    HousekeepingMonitor,
)
from ..health import HealthTracker, sanitize_error
from ..providers import ProviderHomes, home_keys
from ..registry import RegistryError
from ..retention import HistoryDataManager, RetentionController, RetentionError
from ..scan_dirs import PROVIDER_SPECS, ScanDirsController, ScanDirsError
from ..traffic import TrafficMonitor
from ..updates import UpdateChecker
from ..usage import (
    SessionSwitchThresholds,
    UsageAggregator,
)
from .assets import (
    _DASHBOARD_HTML,
    _LANGUAGE_COOKIE_NAME,
    _SETTINGS_HTML,
    _localize_page,
)
from .favicon import (
    _FAVICON_CACHE_SECONDS,
    favicon_response,
)
from .payloads import (
    _MAX_REQUEST_BYTES,
    _alert_query_from_url,
    _apply_alert_action,
    _archive_path_for,
    _bounded_int,
    _healthz_payload,
    _history_get_payload,
    _housekeeping_arguments,
    _insights_window,
    _readyz_payload,
    _usage_search_arguments,
    empty_alert_history_payload,
    empty_housekeeping_payload,
)
from .state import (
    _AccountSet,
    _attach_session_advice,
    _attach_session_archive,
    _housekeeping_summary,
    _usage_index_summary,
    build_multi_dashboard_state,
)


# 中文注释：请求体最多读这么多字节（正常请求上限是 64 KiB，这里留足余量）。
# 超过硬上限的部分不再读取，避免异常的超大 body 把内存或线程占满。
_BODY_DRAIN_LIMIT = 8 * 1024 * 1024


@dataclass
class _DashboardContext:
    """请求处理器共享的依赖与状态缓存；一个 Dashboard 服务对应一个实例。

    注册表与账号元数据经由 accounts 容器读取，DashboardServer 可以在运行中
    热替换账号集合而无需重启 HTTP 服务。
    """

    accounts: _AccountSet
    logger: logging.Logger
    usage_aggregator: UsageAggregator | None = None
    budget_usd: float | None = None
    alert_context_content: bool = False
    traffic_monitor: TrafficMonitor | None = None
    alert_store: TrafficAlertStore | None = None
    housekeeping: HousekeepingMonitor | None = None
    updates: UpdateChecker | None = None
    thresholds_in_use: SessionSwitchThresholds = field(
        default_factory=SessionSwitchThresholds
    )
    scan_dirs: ScanDirsController | None = None
    health: HealthTracker | None = None
    history: HistoryDataManager | None = None
    retention: RetentionController | None = None
    state_cache_lock: Lock = field(default_factory=Lock, repr=False)
    state_cache: dict[str, Any] = field(default_factory=dict, repr=False)

    def alert_stats(self) -> dict[str, Any]:
        """返回落盘告警的统计；数据库不可用时降级为空统计。"""

        if self.alert_store is None:
            return {
                "available": False,
                "total": 0,
                "unread": 0,
                "danger": 0,
                "warn": 0,
                "last_alert_at": None,
            }
        try:
            return {"available": True, **self.alert_store.stats()}
        except AlertStoreError:
            self.logger.exception("Dashboard 读取告警统计失败")
            return {
                "available": False,
                "total": 0,
                "unread": 0,
                "danger": 0,
                "warn": 0,
                "last_alert_at": None,
            }

    def state_payload(self) -> dict[str, Any]:
        """合并并发刷新，只构建一次状态；目录热更新立即使缓存失效。"""

        registries, metadata, homes, revision = self.accounts.configuration()
        with self.state_cache_lock:
            moment = time.monotonic()
            if self.state_cache.get("revision") == revision and moment < self.state_cache.get(
                "expires", 0
            ):
                return self.state_cache["payload"]
            state = build_multi_dashboard_state(
                registries,
                account_metadata=metadata,
                homes=homes,
                budget_usd=self.budget_usd,
                traffic=self.traffic_monitor.latest()
                if self.traffic_monitor is not None
                else None,
                health=self.health,
            )
            state["alert_history"] = self.alert_stats()
            _attach_session_advice(state, self.usage_aggregator, self.thresholds_in_use)
            _attach_session_archive(state, self.housekeeping)
            state["housekeeping"] = _housekeeping_summary(self.housekeeping)
            state["usage_index"] = _usage_index_summary(self.usage_aggregator)
            state["health"] = self.health.snapshot() if self.health is not None else None
            state["update"] = (
                self.updates.snapshot() if self.updates is not None else None
            )
            # 中文注释：TTL 从构建完成后计算，慢查询也不会使排队请求重复扫描。
            self.state_cache.update(
                revision=revision, expires=time.monotonic() + 2, payload=state
            )
            return state


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """处理 Dashboard 页面、只读状态和告警历史请求。

    路由表把路径映射到方法名；GET 与 HEAD 共用页面和健康检查的实现，HEAD 的
    数据接口只回响应头、不触发扫描。``_make_handler`` 为每个服务生成绑定了
    ``context`` 的子类。
    """

    server_version = "ATokenMonitorDashboard/0.9"
    context: ClassVar[_DashboardContext]
    # 中文注释：do_POST 每次请求都会重新赋值；GET/HEAD 与直接调用处理器方法时为空。
    # 不能用 ClassVar：mypy 不允许通过实例给类变量赋值。
    _request_body: bytes = b""

    _GET_ROUTES: ClassVar[dict[str, str]] = {
        "/": "_get_dashboard_page",
        "/settings": "_get_settings_page",
        "/healthz": "_get_healthz",
        "/readyz": "_get_readyz",
        "/api/state": "_get_state",
        "/api/alerts": "_get_alerts",
        "/api/alerts/context": "_get_alert_context",
        "/api/scan-dirs": "_get_scan_dirs",
        "/api/history": "_get_history",
        "/api/usage": "_get_usage",
        "/api/usage/search": "_get_usage_search",
        "/api/housekeeping": "_get_housekeeping",
        "/api/insights": "_get_insights",
        "/api/update": "_get_update",
    }
    _HEAD_ROUTES: ClassVar[dict[str, str]] = {
        "/": "_head_dashboard_page",
        "/settings": "_head_settings_page",
        "/healthz": "_head_healthz",
        "/readyz": "_head_readyz",
        "/api/state": "_head_state",
        "/api/alerts": "_head_alerts",
        "/api/scan-dirs": "_head_scan_dirs",
        "/api/history": "_head_history",
        "/api/usage": "_head_usage",
        "/api/usage/search": "_head_usage_search",
        "/api/housekeeping": "_head_housekeeping",
        "/api/insights": "_head_insights",
        "/api/update": "_head_update",
    }
    _POST_ROUTES: ClassVar[dict[str, str]] = {
        "/api/alerts": "_post_alerts",
        "/api/housekeeping": "_post_housekeeping",
        "/api/scan-dirs": "_post_scan_dirs",
        "/api/history": "_post_history",
        "/api/update": "_post_update",
    }

    def do_GET(self) -> None:
        """返回静态页面或当前监控状态。"""

        if not self._dispatch(self._GET_ROUTES, include_body=True):
            self._send_json(status=404, payload={"error": "not_found"})

    def do_HEAD(self) -> None:
        """返回 GET 的响应头，便于使用 curl 做健康检查。"""

        if not self._dispatch(self._HEAD_ROUTES, include_body=False):
            self._send_json(
                status=404,
                payload={"error": "not_found"},
                include_body=False,
            )

    def do_POST(self) -> None:
        """处理告警历史、磁盘管理、扫描目录和历史数据的写请求，其余路径仍为只读。"""

        # 中文注释：先把请求体读干净再分发。带 body 的 POST 如果服务端没读完就回响应
        # 并关闭连接，Windows 会直接 RST，客户端拿到 WinError 10053（连接中止）而不是
        # 我们的状态码；提前返回的错误分支（405 / 503 / 400）尤其容易踩到。
        self._request_body = self._take_request_body()
        route = self._POST_ROUTES.get(urlsplit(self.path).path)
        if route is None:
            self._send_json(status=405, payload={"error": "read_only"})
            return
        getattr(self, route)()

    def _take_request_body(self) -> bytes:
        """按 Content-Length 读完请求体；只保留正文上限以内的字节。

        超过硬上限的部分不再读取（连接也标记为不可复用），避免异常的超大 body
        把内存或线程占满。
        """

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if length <= 0:
            return b""
        kept = bytearray()
        remaining = min(length, _BODY_DRAIN_LIMIT)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            if len(kept) < _MAX_REQUEST_BYTES:
                kept.extend(chunk[: _MAX_REQUEST_BYTES - len(kept)])
            remaining -= len(chunk)
        if length > _BODY_DRAIN_LIMIT:
            self.close_connection = True
        return bytes(kept)

    def _dispatch(self, routes: dict[str, str], include_body: bool) -> bool:
        """按路由表分发；未命中路由时再尝试 favicon，都不匹配返回 False。"""

        path = urlsplit(self.path).path
        route = routes.get(path)
        if route is not None:
            getattr(self, route)()
            return True
        favicon = favicon_response(path)
        if favicon is None:
            return False
        self._send_bytes(
            status=200,
            content_type=favicon[0],
            body=favicon[1],
            include_body=include_body,
            cache_seconds=_FAVICON_CACHE_SECONDS,
        )
        return True

    @property
    def _query(self) -> str:
        """当前请求的查询字符串。"""

        return urlsplit(self.path).query

    def _send_page(self, page: str, include_body: bool) -> None:
        """按请求语言本地化并发送 HTML 页面。"""

        self._send_bytes(
            status=200,
            content_type="text/html; charset=utf-8",
            body=_localize_page(page, self._request_language()).encode("utf-8"),
            include_body=include_body,
        )

    def _get_dashboard_page(self) -> None:
        self._send_page(_DASHBOARD_HTML, include_body=True)

    def _head_dashboard_page(self) -> None:
        self._send_page(_DASHBOARD_HTML, include_body=False)

    def _get_settings_page(self) -> None:
        self._send_page(_SETTINGS_HTML, include_body=True)

    def _head_settings_page(self) -> None:
        self._send_page(_SETTINGS_HTML, include_body=False)

    def _send_probe(self, payload_builder: Any, include_body: bool) -> None:
        """健康检查：GET 与 HEAD 返回同样的状态码。"""

        status, payload = payload_builder(self.context.health)
        self._send_json(status=status, payload=payload, include_body=include_body)

    def _get_healthz(self) -> None:
        self._send_probe(_healthz_payload, include_body=True)

    def _head_healthz(self) -> None:
        self._send_probe(_healthz_payload, include_body=False)

    def _get_readyz(self) -> None:
        self._send_probe(_readyz_payload, include_body=True)

    def _head_readyz(self) -> None:
        self._send_probe(_readyz_payload, include_body=False)

    def _send_state(self, include_body: bool) -> None:
        """GET / HEAD /api/state：合并后的监控状态快照。"""

        try:
            state = self.context.state_payload()
        except RegistryError:
            self.context.logger.exception("Dashboard 读取状态失败")
            self._send_json(
                status=503,
                payload={"error": "monitor_state_unavailable"},
                include_body=include_body,
            )
            return
        self._send_json(status=200, payload=state, include_body=include_body)

    def _get_state(self) -> None:
        self._send_state(include_body=True)

    def _head_state(self) -> None:
        self._send_state(include_body=False)

    def _get_alerts(self) -> None:
        """GET /api/alerts：分页查询落盘告警。"""

        ctx = self.context
        if ctx.alert_store is None:
            self._send_json(
                status=200,
                payload={
                    "updated_at": time.time(),
                    **empty_alert_history_payload(),
                },
            )
            return
        try:
            criteria = _alert_query_from_url(self._query)
            alerts, has_more = ctx.alert_store.query_page(criteria)
            stats = ctx.alert_store.stats(since=criteria.since)
        except AlertStoreError as error:
            self._send_json(
                status=400,
                payload={
                    "error": "invalid_alert_query",
                    "message": str(error),
                },
            )
            return
        self._send_json(
            status=200,
            payload={
                "updated_at": time.time(),
                "available": True,
                "retention_days": ctx.alert_store.retention_days,
                "merge_window_seconds": ctx.alert_store.merge_window_seconds,
                "alerts": [item.to_dict() for item in alerts],
                "stats": stats,
                "has_more": has_more,
                "limit": criteria.limit,
                "offset": criteria.offset,
            },
        )


    def _get_alert_context(self) -> None:
        """GET /api/alerts/context：按告警关联当时的本地会话活动。"""

        ctx = self.context
        homes = ctx.accounts.provider_homes()
        if ctx.alert_store is None:
            self._send_json(
                status=200,
                payload={
                    "updated_at": time.time(),
                    "available": False,
                    "context": None,
                },
            )
            return
        raw_id = parse_qs(self._query).get("id", [""])[0]
        try:
            alert_id = int(raw_id)
        except ValueError:
            self._send_json(
                status=400,
                payload={"error": "invalid_alert_id", "message": "id 必须是整数"},
            )
            return
        try:
            alert = ctx.alert_store.get(alert_id)
        except AlertStoreError as error:
            self._send_json(
                status=500,
                payload={"error": "alert_store_error", "message": str(error)},
            )
            return
        if alert is None:
            self._send_json(
                status=404,
                payload={"error": "alert_not_found", "message": f"告警 #{alert_id} 不存在"},
            )
            return
        # 中文注释：多账号各自有独立 CODEX_HOME，会话分散在每个 home 的
        # sessions/ 下；从当前生效的账号元数据收集全部根目录，再补上
        # 默认 home 兜底（例如只跑了默认账号的独立部署）。
        _, metadata_map = ctx.accounts.snapshot()
        codex_roots = []
        for meta in metadata_map.values():
            home = (meta or {}).get("codex_home")
            if home:
                codex_roots.append(Path(str(home)).expanduser() / "sessions")
        # 中文注释：配置控制器显式禁用 Codex 时不能再扫描默认主目录。
        if ctx.scan_dirs is None and not codex_roots:
            codex_roots.append(default_session_root())
        context = load_alert_context(
            alert,
            configured_alert_context_roots(
                codex_sessions=tuple(codex_roots),
                homes=homes,
            ),
            include_content=ctx.alert_context_content,
        )
        self._send_json(
            status=200,
            payload={
                "updated_at": time.time(),
                "available": True,
                "context": context,
            },
        )


    def _get_scan_dirs(self) -> None:
        """GET /api/scan-dirs：返回各 provider 的扫描目录。"""

        ctx = self.context
        if ctx.scan_dirs is None:
            self._send_json(
                status=200,
                payload={
                    "updated_at": time.time(),
                    "available": False,
                    "providers": [],
                },
            )
            return
        self._send_json(
            status=200,
            payload={
                "updated_at": time.time(),
                "available": True,
                **ctx.scan_dirs.snapshot(),
            },
        )


    def _get_history(self) -> None:
        """GET /api/history：返回历史数据保留期与清理预览。"""

        ctx = self.context
        status, payload = _history_get_payload(
            ctx.history,
            ctx.retention,
            self._query,
        )
        self._send_json(status=status, payload=payload)


    def _get_usage(self) -> None:
        """GET /api/usage：返回用量快照。"""

        ctx = self.context
        current_registries, current_metadata = ctx.accounts.snapshot()
        try:
            usage = (
                ctx.usage_aggregator.snapshot(
                    current_registries,
                    account_metadata=current_metadata,
                )
                if ctx.usage_aggregator is not None
                else UsageAggregator.empty_snapshot()
            )
        except RegistryError:
            ctx.logger.exception("Dashboard 读取用量失败")
            self._send_json(
                status=503,
                payload={"error": "usage_state_unavailable"},
            )
            return
        self._send_json(
            status=200,
            payload={"updated_at": time.time(), "usage": usage},
        )


    def _get_usage_search(self) -> None:
        """GET /api/usage/search：按条件检索用量明细。"""

        ctx = self.context
        try:
            arguments = _usage_search_arguments(self._query)
            result = (
                ctx.usage_aggregator.search(**arguments)
                if ctx.usage_aggregator is not None
                else UsageAggregator.empty_search(
                    group=arguments["group"],
                    sort=arguments["sort"],
                    limit=arguments["limit"],
                    offset=arguments["offset"],
                )
            )
            facets = (
                ctx.usage_aggregator.usage_facets()
                if ctx.usage_aggregator is not None
                else {
                    "available": False,
                    "records": 0,
                    "sessions": 0,
                    "models": [],
                    "accounts": [],
                    "first_at": None,
                    "last_at": None,
                }
            )
        except ValueError as error:
            self._send_json(
                status=400,
                payload={
                    "error": "invalid_usage_search",
                    "message": str(error),
                },
            )
            return
        self._send_json(
            status=200,
            payload={
                "updated_at": time.time(),
                "search": result,
                "facets": facets,
            },
        )


    def _get_housekeeping(self) -> None:
        """GET /api/housekeeping：返回磁盘统计、清理预览和归档任务。"""

        ctx = self.context
        if ctx.housekeeping is None:
            self._send_json(
                status=200,
                payload={
                    "updated_at": time.time(),
                    "available": False,
                    "report": empty_housekeeping_payload(),
                    "preview": None,
                    "projects": [],
                    "project_sessions": None,
                    "archives": [],
                },
            )
            return
        try:
            arguments = _housekeeping_arguments(self._query)
            if arguments["task"]:
                self._send_json(
                    status=200,
                    payload={
                        "updated_at": time.time(),
                        "available": True,
                        "task": ctx.housekeeping.task(arguments["task"]),
                        "tasks": list(ctx.housekeeping.tasks()),
                    },
                )
                return
            criteria = CleanupCriteria(
                older_than_days=arguments["days"],
                min_bytes=arguments["min_bytes"],
                projects=(
                    (arguments["project"],) if arguments["project"] else ()
                ),
            )
            report = ctx.housekeeping.refresh(force=arguments["refresh"])
            preview = ctx.housekeeping.preview(criteria)
            # 中文注释：选中项目时返回该项目的全部会话文件及归档资格，
            # 供前端逐个管理；未选中项目时不携带，避免大清单拖慢轮询。
            project_sessions: list[dict[str, Any]] | None = None
            if arguments["project"]:
                rows = [
                    item
                    for item in ctx.housekeeping.sessions()
                    if item.project == arguments["project"]
                ]
                rows.sort(
                    key=lambda item: item.size
                    * max(0.0, time.time() - item.modified_at),
                    reverse=True,
                )
                states = ctx.housekeeping.session_archive_state(
                    [str(item.path) for item in rows[:200]]
                )
                project_sessions = [
                    {
                        **item.to_dict(),
                        "archive_state": states.get(str(item.path)),
                    }
                    for item in rows[:200]
                ]
        except (HousekeepingError, ValueError) as error:
            self._send_json(
                status=400,
                payload={
                    "error": "invalid_housekeeping_query",
                    "message": str(error),
                },
            )
            return
        self._send_json(
            status=200,
            payload={
                "updated_at": time.time(),
                "available": True,
                "report": report,
                "preview": preview,
                "projects": list(ctx.housekeeping.projects(criteria)),
                "project_sessions": project_sessions,
                "archives": list(ctx.housekeeping.restores()),
                "tasks": list(ctx.housekeeping.tasks()),
            },
        )


    def _get_insights(self) -> None:
        """GET /api/insights：返回使用习惯分析。"""

        ctx = self.context
        window_kind, window_days, window_since = _insights_window(
            self._query
        )
        current_registries, current_metadata = ctx.accounts.snapshot()
        try:
            insights = (
                ctx.usage_aggregator.insights(
                    current_registries,
                    account_metadata=current_metadata,
                    since_days=window_days,
                    since=window_since,
                    window_kind=window_kind,
                )
                if ctx.usage_aggregator is not None
                else UsageAggregator.empty_insights()
            )
        except RegistryError:
            ctx.logger.exception("Dashboard 读取习惯分析失败")
            self._send_json(
                status=503,
                payload={"error": "insights_state_unavailable"},
            )
            return
        self._send_json(
            status=200,
            payload={"updated_at": time.time(), "insights": insights},
        )


    def _head_alerts(self) -> None:
        """HEAD /api/alerts：只回响应头，不做实际查询。"""

        ctx = self.context
        # 中文注释：HEAD 只用于健康检查，不返回告警明细。
        self._send_json(
            status=200,
            payload={
                "updated_at": time.time(),
                "available": ctx.alert_store is not None,
                "stats": ctx.alert_stats(),
            },
            include_body=False,
        )


    def _head_scan_dirs(self) -> None:
        """HEAD /api/scan-dirs：只回响应头，不做实际查询。"""

        ctx = self.context
        # 中文注释：HEAD 只用于健康检查，不做目录校验。
        self._send_json(
            status=200,
            payload={
                "updated_at": time.time(),
                "available": ctx.scan_dirs is not None,
            },
            include_body=False,
        )


    def _head_history(self) -> None:
        """HEAD /api/history：只回响应头，不做实际查询。"""

        ctx = self.context
        # 中文注释：HEAD 只用于健康检查，不触发预览统计。
        self._send_json(
            status=200,
            payload={
                "updated_at": time.time(),
                "available": ctx.history is not None,
            },
            include_body=False,
        )


    def _head_usage(self) -> None:
        """HEAD /api/usage：只回响应头，不做实际查询。"""

        ctx = self.context
        usage = (
            ctx.usage_aggregator.cached_snapshot()
            if ctx.usage_aggregator is not None
            else UsageAggregator.empty_snapshot()
        )
        self._send_json(
            status=200,
            payload={"updated_at": time.time(), "usage": usage},
            include_body=False,
        )


    def _head_usage_search(self) -> None:
        """HEAD /api/usage/search：只回响应头，不做实际查询。"""

        ctx = self.context
        # 中文注释：HEAD 只用于健康检查，不触发一次完整用量检索。
        self._send_json(
            status=200,
            payload={
                "updated_at": time.time(),
                "search": UsageAggregator.empty_search(),
                "facets": {
                    "available": ctx.usage_aggregator is not None,
                    "records": 0,
                    "sessions": 0,
                    "models": [],
                    "accounts": [],
                    "first_at": None,
                    "last_at": None,
                },
            },
            include_body=False,
        )


    def _head_housekeeping(self) -> None:
        """HEAD /api/housekeeping：只回响应头，不做实际查询。"""

        ctx = self.context
        # 中文注释：HEAD 只用于健康检查，不触发一次目录扫描。
        self._send_json(
            status=200,
            payload={
                "updated_at": time.time(),
                "available": ctx.housekeeping is not None,
                "report": (
                    ctx.housekeeping.latest()
                    if ctx.housekeeping is not None
                    else empty_housekeeping_payload()
                ),
                "preview": None,
                "projects": [],
                "project_sessions": None,
                "archives": [],
            },
            include_body=False,
        )


    def _update_payload(self) -> dict[str, Any]:
        """版本更新状态；没有接入检查器时返回不可用。"""

        checker = self.context.updates
        if checker is None:
            return {"available": False, "update": None}
        # 中文注释：available 表示「这个运行模式能查更新」（有状态目录且没被关闭），
        # update 里始终带快照，页面据此区分「没有新版本」与「检查已关闭」。
        return {"available": checker.enabled, "update": checker.snapshot()}

    def _get_update(self) -> None:
        """GET /api/update：只读缓存，不触发网络检查。"""

        self._send_json(
            status=200,
            payload={"updated_at": time.time(), **self._update_payload()},
        )

    def _head_update(self) -> None:
        """HEAD /api/update：只回响应头。"""

        self._send_json(
            status=200,
            payload={"updated_at": time.time(), **self._update_payload()},
            include_body=False,
        )

    def _post_update(self) -> None:
        """POST /api/update：后台异步补一次检查，请求线程不等待网络。"""

        checker = self.context.updates
        if checker is None:
            self._send_json(
                status=503,
                payload={"error": "update_check_unavailable"},
            )
            return
        try:
            body = self._read_json_body()
        except AlertStoreError as error:
            self._send_json(
                status=400,
                payload={"error": "invalid_update_request", "message": str(error)},
            )
            return
        action = str(body.get("action") or "check").strip()
        if action != "check":
            self._send_json(status=400, payload={"error": "invalid_update_action"})
            return
        started = checker.refresh_async(force=True)
        self._send_json(
            status=200,
            payload={
                "ok": True,
                "checking": started,
                "available": True,
                "update": checker.snapshot(),
            },
        )

    def _head_insights(self) -> None:
        """HEAD /api/insights：只回响应头，不做实际查询。"""

        # 中文注释：HEAD 只用于健康检查，不触发一次完整习惯分析。
        self._send_json(
            status=200,
            payload={
                "updated_at": time.time(),
                "insights": UsageAggregator.empty_insights(),
            },
            include_body=False,
        )


    def _post_alerts(self) -> None:
        """POST /api/alerts：标记已读、删除或清空告警。"""

        ctx = self.context
        if ctx.alert_store is None:
            self._send_json(
                status=503,
                payload={"error": "alert_history_unavailable"},
            )
            return
        try:
            body = self._read_json_body()
            action = str(body.get("action") or "").strip()
            changed = _apply_alert_action(ctx.alert_store, action, body)
        except AlertStoreError as error:
            self._send_json(
                status=400,
                payload={"error": "invalid_alert_action", "message": str(error)},
            )
            return
        self._send_json(
            status=200,
            payload={
                "ok": True,
                "action": action,
                "changed": changed,
                "stats": ctx.alert_stats(),
            },
        )

    def _post_housekeeping(self) -> None:
        self._handle_housekeeping_post(self.context.housekeeping)

    def _post_scan_dirs(self) -> None:
        self._handle_scan_dirs_post(self.context.scan_dirs)

    def _post_history(self) -> None:
        self._handle_history_post(self.context.history, self.context.retention)

    def _handle_housekeeping_post(
        self,
        monitor: HousekeepingMonitor | None,
    ) -> None:
        """处理会话归档、清理和恢复请求；必须显式确认。"""

        if monitor is None:
            self._send_json(
                status=503,
                payload={"error": "housekeeping_unavailable"},
            )
            return
        try:
            body = self._read_json_body()
            action = str(body.get("action") or "").strip()
            days = _bounded_int(body.get("days"), maximum=3650) or 30
            min_bytes = int(
                float(body.get("min_size_mb") or 0) * 1024 * 1024
            )
            session_path = str(body.get("session") or "").strip()
            # 中文注释：项目筛选按字符串原样接收（strip、限长），
            # 具体会话优先于项目筛选。
            project = str(body.get("project") or "").strip()[:512] or None
            # 中文注释：项目卡片的「压缩归档」压缩整个项目的全部非活动会话，
            # 不看保留天数和体积下限；活动会话与过新文件仍由后端跳过。
            all_sessions = bool(body.get("all_sessions")) and bool(project)
            if session_path:
                # 中文注释：单个会话先确认它确实在自己的可归档目录里，
                # 不接受网页传来的任意路径。
                state = monitor.session_archive_state([session_path])
                entry = state.get(str(Path(session_path).expanduser()))
                if entry is None or not entry.get("eligible"):
                    reason = (entry or {}).get("reason") or "会话不存在"
                    raise HousekeepingError(f"该会话当前不能归档：{reason}")
                criteria = CleanupCriteria(paths=(session_path,))
            elif all_sessions:
                # 中文注释：all_sessions 只在给了 project 时才为真。
                criteria = CleanupCriteria(
                    projects=(project,) if project else (),
                    any_age=True,
                )
            else:
                criteria = CleanupCriteria(
                    older_than_days=days,
                    min_bytes=max(0, min_bytes),
                    projects=(project,) if project else (),
                )
            if action == "archive" and body.get("async") is True:
                if body.get("confirm") is not True:
                    raise HousekeepingError("归档需要确认")
                task = monitor.start_task("archive", criteria)
                self._send_json(
                    status=200,
                    payload={
                        "ok": True,
                        "action": action,
                        "task": task,
                        "report": monitor.latest(),
                        "preview": monitor.preview(criteria),
                        "archives": list(monitor.restores()),
                    },
                )
                return
            if action == "clean" and body.get("async") is True:
                if body.get("confirm") is not True:
                    raise HousekeepingError("清理需要确认")
                task = monitor.start_task("clean", criteria)
                self._send_json(
                    status=200,
                    payload={
                        "ok": True,
                        "action": action,
                        "task": task,
                        "report": monitor.latest(),
                        "preview": monitor.preview(criteria),
                        "archives": list(monitor.restores()),
                    },
                )
                return
            if action == "archive":
                if body.get("confirm") is not True:
                    raise HousekeepingError("归档需要确认")
                result = monitor.archive(criteria, confirm=True)
            elif action == "clean":
                if body.get("confirm") is not True:
                    raise HousekeepingError("清理需要确认")
                result = monitor.clean(criteria, confirm=True)
            elif action == "restore":
                archive = _archive_path_for(
                    monitor,
                    str(body.get("archive") or ""),
                )
                result = monitor.restore(archive)
            else:
                raise HousekeepingError(f"未知操作: {action or '(空)'}")
        except (HousekeepingError, ValueError) as error:
            self._send_json(
                status=400,
                payload={"error": "invalid_housekeeping_action", "message": str(error)},
            )
            return
        report = monitor.refresh(force=True)
        self._send_json(
            status=200,
            payload={
                "ok": True,
                "action": action,
                "result": result,
                "report": report,
                "preview": monitor.preview(criteria),
                "projects": list(monitor.projects(criteria)),
                "archives": list(monitor.restores()),
            },
        )

    def _handle_scan_dirs_post(
        self,
        controller: ScanDirsController | None,
    ) -> None:
        """处理扫描目录的添加、移除和恢复默认；移除与恢复需显式确认。"""

        if controller is None:
            self._send_json(
                status=503,
                payload={"error": "scan_dirs_unavailable"},
            )
            return
        try:
            body = self._read_json_body()
        except AlertStoreError as error:
            self._send_json(
                status=400,
                payload={
                    "error": "invalid_scan_dir_action",
                    "message": str(error),
                },
            )
            return
        action = str(body.get("action") or "").strip()
        if action not in {"add", "remove", "reset"}:
            self._send_json(
                status=400,
                payload={
                    "error": "invalid_scan_dir_action",
                    "message": f"未知操作: {action or '(空)'}",
                },
            )
            return
        provider = body.get("provider")
        if not isinstance(provider, str) or provider not in PROVIDER_SPECS:
            self._send_json(
                status=400,
                payload={
                    "error": "invalid_scan_dir_action",
                    "message": "未知或未提供的 provider",
                },
            )
            return
        if action in {"remove", "reset"} and body.get("confirm") is not True:
            self._send_json(
                status=400,
                payload={
                    "error": "invalid_scan_dir_action",
                    "message": "移除和恢复默认需要 confirm: true",
                },
            )
            return
        path: Path | None = None
        if action in {"add", "remove"}:
            raw_path = body.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                self._send_json(
                    status=400,
                    payload={
                        "error": "invalid_scan_dir_action",
                        "message": f"{action} 操作需要字符串形式的 path",
                    },
                )
                return
            path = Path(raw_path)
        try:
            snapshot = controller.apply(action, provider, path)
        except ScanDirsError as error:
            self._send_json(
                status=400,
                payload={"error": "invalid_scan_dir", "message": str(error)},
            )
            return
        self._send_json(
            status=200,
            payload={"ok": True, "action": action, **snapshot},
        )

    def _handle_history_post(
        self,
        manager: HistoryDataManager | None,
        controller: RetentionController | None,
    ) -> None:
        """处理历史数据清理和保留期配置；清理与恢复默认需显式确认。"""

        if manager is None:
            self._send_json(
                status=503,
                payload={"error": "history_unavailable"},
            )
            return
        try:
            body = self._read_json_body()
        except AlertStoreError as error:
            self._send_json(
                status=400,
                payload={
                    "error": "invalid_history_action",
                    "message": str(error),
                },
            )
            return
        action = str(body.get("action") or "").strip()
        if action == "cleanup":
            if body.get("confirm") is not True:
                self._send_json(
                    status=400,
                    payload={
                        "error": "invalid_history_action",
                        "message": "清理需要 confirm: true",
                    },
                )
                return
            try:
                result = manager.cleanup()
            except RetentionError as error:
                # 中文注释：部分库清理失败时仍回传已完成的部分结果。
                self._send_json(
                    status=500,
                    payload={
                        "error": "history_cleanup_failed",
                        "message": sanitize_error(error),
                        "result": manager.last_cleanup,
                    },
                )
                return
            self._send_json(
                status=200,
                payload={"ok": True, "action": action, "result": result},
            )
            return
        if action in {"set-retention", "reset-retention"}:
            if controller is None:
                self._send_json(
                    status=503,
                    payload={"error": "retention_unavailable"},
                )
                return
            if action == "reset-retention":
                if body.get("confirm") is not True:
                    self._send_json(
                        status=400,
                        payload={
                            "error": "invalid_history_action",
                            "message": "恢复默认需要 confirm: true",
                        },
                    )
                    return
                try:
                    snapshot = controller.apply("reset")
                except RetentionError as error:
                    self._send_json(
                        status=400,
                        payload={
                            "error": "invalid_retention",
                            "message": str(error),
                        },
                    )
                    return
                self._send_json(
                    status=200,
                    payload={"ok": True, "action": action, **snapshot},
                )
                return
            usage_days = body.get("usage_days")
            session_days = body.get("session_days")
            alert_days = body.get("alert_days")
            if (
                usage_days is None
                and session_days is None
                and alert_days is None
            ):
                self._send_json(
                    status=400,
                    payload={
                        "error": "invalid_retention",
                        "message": (
                            "至少提供 usage_days、session_days 或 alert_days 之一"
                        ),
                    },
                )
                return
            for name, value in (
                ("usage_days", usage_days),
                ("session_days", session_days),
                ("alert_days", alert_days),
            ):
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                ):
                    self._send_json(
                        status=400,
                        payload={
                            "error": "invalid_retention",
                            "message": f"{name} 必须是数值",
                        },
                    )
                    return
            try:
                snapshot = controller.apply(
                    "set",
                    usage_days=usage_days,
                    session_days=session_days,
                    alert_days=alert_days,
                )
            except RetentionError as error:
                self._send_json(
                    status=400,
                    payload={"error": "invalid_retention", "message": str(error)},
                )
                return
            self._send_json(
                status=200,
                payload={"ok": True, "action": action, **snapshot},
            )
            return
        self._send_json(
            status=400,
            payload={
                "error": "invalid_history_action",
                "message": f"未知操作: {action or '(空)'}",
            },
        )

    def _read_json_body(self) -> dict[str, Any]:
        """读取并校验 JSON 请求体，限制大小和内容类型。"""

        content_type = (
            (self.headers.get("Content-Type") or "")
            .split(";")[0]
            .strip()
            .lower()
        )
        if content_type != "application/json":
            raise AlertStoreError("请求体必须是 application/json")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as error:
            raise AlertStoreError("Content-Length 非法") from error
        if length <= 0 or length > _MAX_REQUEST_BYTES:
            raise AlertStoreError("请求体大小非法")
        # 中文注释：body 已由 do_POST 在分发前读完（见 _take_request_body），
        # 这里只做校验与解析。
        raw = self._request_body[:length]
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AlertStoreError(f"请求体不是合法 JSON: {error}") from error
        if not isinstance(payload, dict):
            raise AlertStoreError("请求体顶层必须是 JSON 对象")
        return payload

    def log_message(self, format: str, *args: object) -> None:
        """把 HTTP 访问日志交给监控器日志，不污染标准输出。"""

        self.context.logger.debug("Dashboard HTTP " + format, *args)

    def _request_language(self) -> str:
        """当前请求的语言。

        优先级：显式 ``?lang=`` > 顶栏开关写入的 cookie > ``Accept-Language`` > 中文。
        cookie 让「手动切换」对后续接口请求同样生效，不必给每个 fetch 加参数。
        """

        try:
            query = parse_qs(self._query)
        except ValueError:
            query = {}
        languages = query.get("lang")
        override = languages[0] if languages else None
        header = (
            self.headers.get("Accept-Language")
            if hasattr(self, "headers")
            else None
        )
        cookie = self._language_cookie() if override is None else None
        return resolve_language(
            accept_language=header,
            override=override or cookie,
        )

    def _language_cookie(self) -> str | None:
        """读取顶栏语言开关写入的 cookie。"""

        header = self.headers.get("Cookie") if hasattr(self, "headers") else None
        if not header:
            return None
        for item in header.split(";"):
            name, _, value = item.strip().partition("=")
            if name == _LANGUAGE_COOKIE_NAME:
                return value.strip() or None
        return None

    def _localized(self, payload: object) -> object:
        """按请求语言本地化负载里的字符串值（字典键与结构不动）。"""

        language = self._request_language()
        return localize_payload(payload, language)

    def _send_json(
        self,
        status: int,
        payload: object,
        include_body: bool = True,
    ) -> None:
        """发送 JSON 响应（英文请求会把负载里的文案换成英文）。"""

        body = json.dumps(
            self._localized(payload),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send_bytes(
            status=status,
            content_type="application/json; charset=utf-8",
            body=body,
            include_body=include_body,
        )

    def _send_bytes(
        self,
        status: int,
        content_type: str,
        body: bytes,
        include_body: bool = True,
        cache_seconds: int | None = None,
    ) -> None:
        """发送带有本地安全响应头的字节响应。

        ``cache_seconds`` 只给内容寻址的静态资源（例如带版本号的 favicon）用；
        监控状态与页面默认一律 ``no-store``，避免浏览器缓存出过期的监控数据。
        """

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache_seconds is None:
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header(
                "Cache-Control",
                f"public, max-age={int(cache_seconds)}",
            )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; connect-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        if include_body:
            self.wfile.write(body)


def _make_handler(
    accounts: _AccountSet,
    logger: logging.Logger,
    usage_aggregator: UsageAggregator | None = None,
    homes: ProviderHomes | None = None,
    budget_usd: float | None = None,
    alert_context_content: bool = False,
    traffic_monitor: TrafficMonitor | None = None,
    alert_store: TrafficAlertStore | None = None,
    housekeeping: HousekeepingMonitor | None = None,
    session_thresholds: SessionSwitchThresholds | None = None,
    scan_dirs: ScanDirsController | None = None,
    health: HealthTracker | None = None,
    history: HistoryDataManager | None = None,
    retention: RetentionController | None = None,
    updates: UpdateChecker | None = None,
) -> type[BaseHTTPRequestHandler]:
    """为一个 Dashboard 服务创建绑定了依赖的请求处理器类型。"""

    accounts.update_homes(
        {key: tuple((homes or {}).get(key, ())) for key in home_keys()}
    )
    context = _DashboardContext(
        accounts=accounts,
        logger=logger,
        usage_aggregator=usage_aggregator,
        budget_usd=budget_usd,
        alert_context_content=alert_context_content,
        traffic_monitor=traffic_monitor,
        alert_store=alert_store,
        housekeeping=housekeeping,
        updates=updates,
        thresholds_in_use=session_thresholds or SessionSwitchThresholds(),
        scan_dirs=scan_dirs,
        health=health,
        history=history,
        retention=retention,
    )
    return type(
        "DashboardRequestHandler",
        (DashboardRequestHandler,),
        {"context": context},
    )
