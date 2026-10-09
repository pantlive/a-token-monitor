"""HTTP 接口的参数解析与响应负载构建（不依赖请求处理器本身）。"""

from __future__ import annotations

import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs

from ..local_time import local_date_end, local_date_start
from ..alerts import (
    MAX_QUERY_LIMIT,
    AlertQuery,
    AlertStoreError,
    TrafficAlertStore,
)
from ..housekeeping import (
    HousekeepingError,
    HousekeepingMonitor,
    empty_housekeeping_report,
)
from ..health import HealthTracker, sanitize_error
from ..retention import HistoryDataManager, RetentionController
from ..usage import (
    DEFAULT_SEARCH_DAYS,
    calendar_day_start,
    search_since_days,
)


# 中文注释：告警历史的写接口只接受小请求体，避免 Dashboard 被当成通用上传入口。
_MAX_REQUEST_BYTES = 64 * 1024


def _insights_window(query: str) -> tuple[str, int | None, float | None]:
    """解析习惯分析的统计窗口，返回 ``(类型, 天数, 起始时间戳)``。

    ``days=today`` 表示按本地日历日的「今天」（与用量区的今天同口径）；
    ``days=N`` 表示最近 N 天；缺失、``0`` 或非法值都按全部历史处理。
    """

    raw = parse_qs(query).get("days", [None])[0]
    if raw is None:
        return ("all", None, None)
    if raw.strip().lower() == "today":
        return ("today", None, calendar_day_start())
    try:
        days = int(raw)
    except ValueError:
        return ("all", None, None)
    if 0 < days <= 3660:
        return ("days", days, None)
    return ("all", None, None)


def _usage_search_arguments(raw_query: str) -> dict[str, Any]:
    """把用量检索查询串解析成 UsageAggregator.search 参数。"""

    params = parse_qs(raw_query)

    def single(name: str) -> str | None:
        for value in params.get(name) or ():
            text = value.strip()
            if text:
                return text
        return None

    # 中文注释：days=0 表示不限制时间范围，非法值退回默认天数。
    days = DEFAULT_SEARCH_DAYS
    raw_days = single("days")
    if raw_days is not None:
        try:
            parsed_days = int(raw_days)
        except ValueError:
            parsed_days = DEFAULT_SEARCH_DAYS
        days = parsed_days if 0 <= parsed_days <= 3660 else DEFAULT_SEARCH_DAYS
    since: float | None = None
    until: float | None = None
    explicit_from = _day_start(single("from"))
    explicit_to = _day_end(single("to"))
    if explicit_from is not None or explicit_to is not None:
        since, until = explicit_from, explicit_to
    elif days > 0:
        since = search_since_days(days)
    group = (single("group") or "session").lower()
    sort = (single("sort") or "recent").lower()
    return {
        "since": since,
        "until": until,
        "models": tuple(_split_values(single("model"))),
        "session": single("session"),
        "project": single("project"),
        "account": single("account"),
        "keyword": single("q"),
        "group": group,
        "sort": sort,
        "limit": _bounded_int(single("limit"), maximum=500) or 50,
        "offset": _bounded_int(single("offset"), maximum=1_000_000) or 0,
    }


def _day_start(value: str | None) -> float | None:
    """把 YYYY-MM-DD 解析为本地当天零点时间戳。"""

    parsed = _parse_day(value)
    return local_date_start(parsed) if parsed is not None else None


def _day_end(value: str | None) -> float | None:
    """把 YYYY-MM-DD 解析为本地当天最后一刻的时间戳。"""

    parsed = _parse_day(value)
    if parsed is None:
        return None
    return local_date_end(parsed)


def _parse_day(value: str | None) -> date | None:
    """解析日期参数，非法值按未提供处理。"""

    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def empty_housekeeping_payload() -> dict[str, Any]:
    """返回未接入磁盘统计时的空报告。"""

    return empty_housekeeping_report()


def _healthz_payload(health: HealthTracker | None) -> tuple[int, dict[str, Any]]:
    """组装 /healthz 响应；主循环停滞（含 degraded 过期）一律视为卡死。"""

    if health is None:
        return 200, {"status": "ok", "updated_at": time.time()}
    main_loop = health.component_status("main-loop")
    if main_loop == "ok":
        return 200, {
            "status": "ok",
            "uptime_seconds": max(0.0, time.time() - health.started_at),
            "updated_at": time.time(),
        }
    return 503, {
        "status": "stuck",
        "main_loop": main_loop,
        "updated_at": time.time(),
    }


def _readyz_payload(health: HealthTracker | None) -> tuple[int, dict[str, Any]]:
    """组装 /readyz 响应；就绪判定只看关键组件，组件明细原样来自快照。"""

    if health is None:
        return 200, {"status": "ok", "updated_at": time.time()}
    snapshot = health.snapshot()
    ready = health.ready()
    return (200 if ready else 503), {
        "status": "ready" if ready else "not_ready",
        "overall": snapshot["overall"],
        "components": snapshot["components"],
        "updated_at": time.time(),
    }


def _history_get_payload(
    history: HistoryDataManager | None,
    retention: RetentionController | None,
    raw_query: str,
) -> tuple[int, dict[str, Any]]:
    """组装 GET /api/history 响应；?preview=1 时附带清理预览。"""

    if history is None:
        return 200, {"updated_at": time.time(), "available": False}
    try:
        dbs = history.db_sizes()
    except OSError:
        # 中文注释：占用统计失败不阻断整个端点，降级为空列表。
        dbs = []
    payload: dict[str, Any] = {
        "updated_at": time.time(),
        "available": True,
        "retention_days": history.retention_days,
        "retention": (
            retention.snapshot()["retention"] if retention is not None else None
        ),
        "dbs": dbs,
        "last_cleanup": history.last_cleanup,
    }
    if parse_qs(raw_query).get("preview") == ["1"]:
        try:
            payload["preview"] = history.preview()
        except OSError as error:
            return 500, {
                "error": "history_preview_failed",
                "message": sanitize_error(error),
                "updated_at": time.time(),
            }
    return 200, payload


def _housekeeping_arguments(raw_query: str) -> dict[str, Any]:
    """解析磁盘/会话管理查询串。"""

    params = parse_qs(raw_query)

    def single(name: str) -> str | None:
        for value in params.get(name) or ():
            text = value.strip()
            if text:
                return text
        return None

    days = _bounded_int(single("days"), maximum=3650) or 30
    min_size_mb = _bounded_float(single("min_size_mb"), maximum=1_000_000) or 0.0
    task = single("task")
    project = single("project")
    if project is not None and len(project) > 512:
        # 中文注释：项目来自查询串，限制长度避免构造异常大的筛选条件。
        project = project[:512]
    return {
        "days": days,
        "min_bytes": int(min_size_mb * 1024 * 1024),
        "refresh": single("refresh") not in {None, "0", "false"},
        "task": task if task and task.isalnum() else None,
        "project": project or None,
    }


def _archive_path_for(monitor: HousekeepingMonitor, name: str) -> Path:
    """把请求里的归档名限制在归档目录内，避免任意路径读取。"""

    candidate = Path(name).name
    if not candidate or candidate != name:
        raise HousekeepingError("归档名非法")
    archive_dir = monitor.archive_dir
    if archive_dir is None:
        raise HousekeepingError("没有配置归档目录")
    archive = archive_dir / candidate
    if not archive.is_file():
        raise HousekeepingError(f"归档不存在: {candidate}")
    return archive


def empty_alert_history_payload() -> dict[str, Any]:
    """返回未接入告警落盘时的空历史结构。"""

    return {
        "available": False,
        "retention_days": None,
        "merge_window_seconds": None,
        "alerts": [],
        "has_more": False,
        "stats": {
            "total": 0,
            "unread": 0,
            "danger": 0,
            "warn": 0,
            "last_alert_at": None,
        },
    }


def _alert_query_from_url(raw_query: str) -> AlertQuery:
    """把 Dashboard 查询串解析为告警筛选条件。"""

    params = parse_qs(raw_query)

    def single(name: str) -> str | None:
        for value in params.get(name) or ():
            text = value.strip()
            if text:
                return text
        return None

    days = _bounded_int(single("days"), maximum=3660)
    since = time.time() - days * 86400 if days else None
    explicit_since = _bounded_float(single("since"))
    if explicit_since is not None:
        since = explicit_since
    acknowledged: bool | None = None
    ack_value = (single("ack") or "").lower()
    if ack_value == "unread":
        acknowledged = False
    elif ack_value == "read":
        acknowledged = True
    return AlertQuery(
        since=since,
        until=_bounded_float(single("until")),
        levels=tuple(_split_values(single("level"))),
        kinds=tuple(_split_values(single("kind"))),
        products=tuple(_split_values(single("product"))),
        acknowledged=acknowledged,
        keyword=single("q"),
        limit=_bounded_int(single("limit"), maximum=MAX_QUERY_LIMIT) or 50,
        offset=_bounded_int(single("offset"), maximum=1_000_000) or 0,
    )


def _apply_alert_action(
    store: TrafficAlertStore,
    action: str,
    body: Mapping[str, Any],
) -> int:
    """执行一次告警历史修改，返回改动条数。"""

    if action == "ack":
        if body.get("all") is True:
            return store.acknowledge(all_alerts=True)
        return store.acknowledge(_alert_ids(body.get("ids")))
    if action == "unack":
        return store.unacknowledge(_alert_ids(body.get("ids")))
    if action == "clear":
        if body.get("all") is True:
            return store.clear_all()
        before = _bounded_float(body.get("before"))
        if before is not None:
            return store.clear_before(before)
        return store.clear(_alert_ids(body.get("ids")))
    raise AlertStoreError(f"未知告警操作: {action or '(空)'}")


def _alert_ids(value: object) -> tuple[int, ...]:
    """校验告警 ID 列表，拒绝非法值和非正整数。"""

    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > MAX_QUERY_LIMIT:
        raise AlertStoreError("ids 必须是告警 ID 数组")
    ids: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise AlertStoreError("ids 只能包含告警 ID 数字")
        identifier = int(item)
        if identifier <= 0:
            raise AlertStoreError("告警 ID 必须是正整数")
        ids.append(identifier)
    return tuple(ids)


def _bounded_int(value: object, *, maximum: int) -> int | None:
    """解析 1 到 maximum 之间的整数，非法值返回 None。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if 1 <= parsed <= maximum else None


def _bounded_float(value: object, *, maximum: float = 1e12) -> float | None:
    """解析正浮点数，非法值返回 None。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or parsed > maximum:
        return None
    return parsed


def _split_values(value: str | None) -> list[str]:
    """把逗号分隔的筛选值拆成去重后的列表。"""

    if not value:
        return []
    items = [item.strip() for item in value.split(",")]
    return [item for index, item in enumerate(items) if item and item not in items[:index]]
