"""组件健康追踪的状态机、脱敏和聚合测试。"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path

from a_token_monitor.health import ComponentHealth, HealthTracker, sanitize_error


class SanitizeErrorTests(unittest.TestCase):
    """错误串对外展示前的脱敏与截断。"""

    def test_home_directory_is_masked(self) -> None:
        home = str(Path.home())
        message = sanitize_error(ValueError(f"读取 {home}/.codex/sessions 失败"))
        self.assertNotIn(home, message)
        self.assertIn("~/.codex/sessions", message)

    def test_long_message_is_truncated(self) -> None:
        message = sanitize_error("x" * 500)
        self.assertEqual(len(message), 200)
        self.assertTrue(message.endswith("…"))

    def test_plain_string_passthrough(self) -> None:
        self.assertEqual(sanitize_error("boom"), "boom")


class ComponentHealthTests(unittest.TestCase):
    """单个组件的状态推导。"""

    def test_starting_without_any_record(self) -> None:
        component = ComponentHealth(key="k", label="K")
        self.assertEqual(component.status(now=100.0), "starting")

    def test_failed_when_error_after_success(self) -> None:
        component = ComponentHealth(
            key="k", label="K", last_success_at=10.0, last_error_at=20.0,
            last_error="boom",
        )
        self.assertEqual(component.status(now=30.0), "failed")

    def test_failed_when_never_succeeded_but_errored(self) -> None:
        component = ComponentHealth(key="k", label="K", last_error_at=5.0,
                                    last_error="boom")
        self.assertEqual(component.status(now=10.0), "failed")

    def test_degraded_when_stale(self) -> None:
        component = ComponentHealth(
            key="k", label="K", stale_after=60.0, last_success_at=100.0
        )
        self.assertEqual(component.status(now=150.0), "ok")
        self.assertEqual(component.status(now=200.0), "degraded")

    def test_ok_without_stale_threshold(self) -> None:
        component = ComponentHealth(key="k", label="K", last_success_at=1.0)
        self.assertEqual(component.status(now=99999.0), "ok")


class HealthTrackerTests(unittest.TestCase):
    """登记、记录、聚合与线程安全。"""

    def test_register_and_record_success(self) -> None:
        tracker = HealthTracker()
        tracker.register("main-loop", "主循环", critical=True, stale_after=120.0)
        tracker.record_success("main-loop", now=10.0)

        snapshot = tracker.snapshot(now=20.0)

        self.assertEqual(snapshot["overall"], "ok")
        component = snapshot["components"][0]
        self.assertEqual(component["status"], "ok")
        self.assertEqual(component["last_success_at"], 10.0)
        self.assertFalse(component["critical"] is False)

    def test_reregister_preserves_records(self) -> None:
        tracker = HealthTracker()
        tracker.register("traffic", "流量", stale_after=60.0)
        tracker.record_success("traffic", now=10.0)
        tracker.register("traffic", "流量采集", stale_after=30.0)

        snapshot = tracker.snapshot(now=20.0)

        component = snapshot["components"][0]
        self.assertEqual(component["label"], "流量采集")
        self.assertEqual(component["last_success_at"], 10.0)

    def test_unregister_removes_component(self) -> None:
        tracker = HealthTracker()
        tracker.register("account:a", "账号 a")
        tracker.unregister("account:a")
        self.assertEqual(tracker.snapshot()["components"], [])
        self.assertEqual(tracker.component_status("account:a"), "unknown")

    def test_unregistered_record_creates_noncritical_component(self) -> None:
        tracker = HealthTracker()
        tracker.record_failure("ad-hoc", "boom", now=5.0)
        snapshot = tracker.snapshot(now=6.0)
        component = snapshot["components"][0]
        self.assertEqual(component["key"], "ad-hoc")
        self.assertFalse(component["critical"])
        self.assertEqual(component["status"], "failed")

    def test_overall_prefers_worst_status(self) -> None:
        tracker = HealthTracker()
        tracker.register("a", "A")
        tracker.register("b", "B")
        tracker.record_success("a", now=10.0)
        tracker.record_failure("b", "boom", now=10.0)
        self.assertEqual(tracker.snapshot(now=11.0)["overall"], "failed")

    def test_ready_ignores_noncritical_failures(self) -> None:
        tracker = HealthTracker()
        tracker.register("main-loop", "主循环", critical=True)
        tracker.register("provider:grok", "Grok")
        tracker.record_success("main-loop", now=10.0)
        tracker.record_failure("provider:grok", "boom", now=10.0)
        self.assertTrue(tracker.ready(now=11.0))

    def test_not_ready_when_critical_starting_or_failed(self) -> None:
        tracker = HealthTracker()
        tracker.register("main-loop", "主循环", critical=True)
        self.assertFalse(tracker.ready(now=1.0))
        tracker.record_failure("main-loop", "boom", now=2.0)
        self.assertFalse(tracker.ready(now=3.0))
        tracker.record_success("main-loop", now=4.0)
        self.assertTrue(tracker.ready(now=5.0))

    def test_uptime_is_monotonic(self) -> None:
        tracker = HealthTracker()
        snapshot = tracker.snapshot(now=tracker.started_at + 5.0)
        self.assertEqual(snapshot["uptime_seconds"], 5.0)

    def test_concurrent_recording(self) -> None:
        tracker = HealthTracker()

        def worker(index: int) -> None:
            for iteration in range(100):
                tracker.record_success(f"c{index}", now=float(iteration))
                tracker.record_failure(f"c{index}", "boom", now=float(iteration))

        threads = [
            threading.Thread(target=worker, args=(index,)) for index in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(tracker.snapshot()["components"]), 8)


if __name__ == "__main__":
    unittest.main()
