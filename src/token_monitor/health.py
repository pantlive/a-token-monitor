"""daemon 各组件的健康状态追踪。

为 ``/healthz``、``/readyz`` 和 Dashboard 降级提示提供统一的组件级状态:
最后成功时间、最近错误(脱敏)和数据是否过期。全部状态只在内存中维护,
不持久化;daemon 重启后组件从 ``starting`` 开始,首轮成功后转为 ``ok``。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


_ERROR_LIMIT = 200

_STATUS_ORDER = {"ok": 0, "starting": 1, "degraded": 2, "failed": 3}


def sanitize_error(error: BaseException | str) -> str:
    """把异常转换为可对外展示的错误串:主目录脱敏为 ``~`` 并截断。"""

    message = str(error)
    try:
        home = str(Path.home())
    except (RuntimeError, OSError):
        home = ""
    if home and home != "/":
        message = message.replace(home, "~")
    if len(message) > _ERROR_LIMIT:
        message = f"{message[: _ERROR_LIMIT - 1]}…"
    return message


@dataclass
class ComponentHealth:
    """单个组件的健康状态记录。"""

    key: str
    label: str
    critical: bool = False
    # 中文注释:超过该秒数没有成功记录即视为数据过期(degraded)。
    stale_after: float | None = None
    last_success_at: float | None = None
    last_error: str | None = None
    last_error_at: float | None = None
    details: dict[str, object] = field(default_factory=dict)

    def status(self, now: float) -> str:
        """根据成功/失败记录计算当前状态。"""

        if self.last_success_at is None:
            if self.last_error_at is not None:
                return "failed"
            return "starting"
        if self.last_error_at is not None and self.last_error_at > self.last_success_at:
            return "failed"
        if self.stale_after is not None and now - self.last_success_at > self.stale_after:
            return "degraded"
        return "ok"


class HealthTracker:
    """线程安全的组件健康登记表。"""

    def __init__(self) -> None:
        self._components: dict[str, ComponentHealth] = {}
        self._lock = threading.Lock()
        self.started_at = time.time()

    def register(
        self,
        key: str,
        label: str,
        *,
        critical: bool = False,
        stale_after: float | None = None,
    ) -> None:
        """登记一个组件;重复登记保留已有记录,只更新元数据。"""

        with self._lock:
            existing = self._components.get(key)
            if existing is not None:
                existing.label = label
                existing.critical = critical
                existing.stale_after = stale_after
                return
            self._components[key] = ComponentHealth(
                key=key,
                label=label,
                critical=critical,
                stale_after=stale_after,
            )

    def unregister(self, key: str) -> None:
        """移除一个组件(例如热重载移除账号时)。"""

        with self._lock:
            self._components.pop(key, None)

    def record_success(
        self,
        key: str,
        now: float | None = None,
        **details: object,
    ) -> None:
        """记录一次成功;未注册的组件按非关键组件自动登记。"""

        timestamp = time.time() if now is None else float(now)
        with self._lock:
            component = self._ensure(key)
            component.last_success_at = timestamp
            if details:
                component.details = dict(details)

    def record_failure(
        self,
        key: str,
        error: BaseException | str,
        now: float | None = None,
        **details: object,
    ) -> None:
        """记录一次失败;错误串脱敏并截断。"""

        timestamp = time.time() if now is None else float(now)
        with self._lock:
            component = self._ensure(key)
            component.last_error = sanitize_error(error)
            component.last_error_at = timestamp
            if details:
                component.details = dict(details)

    def snapshot(self, now: float | None = None) -> dict[str, object]:
        """返回全部组件的状态摘要和整体结论。"""

        timestamp = time.time() if now is None else float(now)
        with self._lock:
            components = [
                self._component_payload(component, timestamp)
                for component in self._components.values()
            ]
        overall = "ok"
        for component in components:
            status = component["status"]
            if _STATUS_ORDER[status] > _STATUS_ORDER[overall]:
                overall = status
        return {
            "overall": overall,
            "uptime_seconds": max(0.0, timestamp - self.started_at),
            "components": components,
        }

    def component_status(self, key: str, now: float | None = None) -> str:
        """返回单个组件的状态;未登记时返回 ``unknown``。"""

        timestamp = time.time() if now is None else float(now)
        with self._lock:
            component = self._components.get(key)
            if component is None:
                return "unknown"
            return component.status(timestamp)

    def ready(self, now: float | None = None) -> bool:
        """就绪判定:所有关键组件都不能处于 failed 或 starting。"""

        timestamp = time.time() if now is None else float(now)
        with self._lock:
            components = list(self._components.values())
        for component in components:
            if component.critical and component.status(timestamp) in (
                "failed",
                "starting",
            ):
                return False
        return True

    def _ensure(self, key: str) -> ComponentHealth:
        component = self._components.get(key)
        if component is None:
            component = ComponentHealth(key=key, label=key)
            self._components[key] = component
        return component

    @staticmethod
    def _component_payload(
        component: ComponentHealth,
        now: float,
    ) -> dict[str, object]:
        status = component.status(now)
        return {
            "key": component.key,
            "label": component.label,
            "critical": component.critical,
            "status": status,
            "stale": status == "degraded",
            "last_success_at": component.last_success_at,
            "last_error": component.last_error,
            "last_error_at": component.last_error_at,
            "details": dict(component.details),
        }
