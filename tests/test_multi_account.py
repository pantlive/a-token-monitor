"""多账号编排的扫描目录热重载测试，不触碰真实的用户主目录。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from token_monitor.accounts import build_account_specs
from token_monitor.grok import resolve_grok_homes
from token_monitor.monitor import MonitorConfig
from token_monitor.multi_account import AccountMonitor, MultiAccountMonitor
from token_monitor.registry import MultiSessionRegistry
from token_monitor.retention import RetentionError


class _FakeMonitor:
    """记录 start/close 调用的假单账号监控器。"""

    def __init__(self) -> None:
        self.start_calls: list[bool] = []
        self.close_calls = 0
        self.run_once_calls: list[float | None] = []
        self.run_once_error: Exception | None = None
        self.quota_health_value: dict[str, object] = {
            "last_success_at": 1.0,
            "last_error": None,
            "is_current": True,
            "app_server_available": True,
        }

    def start(self, allow_app_server_failure: bool = False) -> None:
        """模拟启动并记录参数。"""

        self.start_calls.append(allow_app_server_failure)

    def run_once(self, now: float | None = None) -> None:
        """记录一轮调用，可按需抛出预设异常。"""

        self.run_once_calls.append(now)
        if self.run_once_error is not None:
            raise self.run_once_error

    def quota_health(self) -> dict[str, object]:
        """返回预设的额度链路健康摘要。"""

        return dict(self.quota_health_value)

    def close(self) -> None:
        """模拟关闭。"""

        self.close_calls += 1


def _fake_create_account_monitor(self, account):  # noqa: ANN001, ANN202
    """用真注册表（sqlite 落在临时目录）和假监控器组装 AccountMonitor。"""

    return AccountMonitor(
        account=account,
        registry=MultiSessionRegistry(account.state_dir),
        monitor=_FakeMonitor(),
    )


class _FakeAggregator:
    """记录 update_homes 调用的假用量聚合器。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def update_homes(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


class _FakeDashboard:
    """记录 update_accounts 调用的假 Dashboard。"""

    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []

    def update_accounts(self, registries, account_metadata) -> None:  # noqa: ANN001, ANN202
        self.calls.append((registries, account_metadata))


def _build_monitor(root: Path, homes: tuple[Path, ...], **kwargs: object):
    """在临时目录下构造使用假监控器的 MultiAccountMonitor。"""

    accounts = build_account_specs(homes, state_dir=root / "state")
    options = {
        "accounts": accounts,
        "state_dir": root / "state",
        "config": MonitorConfig(auto_resume=False),
        "grok_homes": (),
        "kimi_homes": (),
        "dsh_homes": (),
        "commandcode_homes": (),
        "claude_homes": (),
    }
    options.update(kwargs)
    return MultiAccountMonitor(**options)


class ApplyScanDirsTests(unittest.TestCase):
    """验证 apply_scan_dirs 的账号 diff、provider 目录替换和联动刷新。"""

    def test_add_and_remove_codex_homes_preserve_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            home_a = root / ".codex-a"
            home_b = root / ".codex-b"
            home_c = root / ".codex-c"
            for home in (home_a, home_b, home_c):
                home.mkdir()
            with patch.object(
                MultiAccountMonitor,
                "_create_account_monitor",
                _fake_create_account_monitor,
            ):
                monitor = _build_monitor(root, (home_a, home_b))
                monitor.start()
                before = {
                    item.account.home: item for item in monitor.account_monitors
                }
                removed_state_dir = before[home_a].account.state_dir
                self.assertTrue(removed_state_dir.exists())

                monitor.apply_scan_dirs(
                    {
                        "codex": (home_b, home_c),
                        "grok": (),
                        "kimi": (),
                        "dsh": (),
                        "commandcode": (),
                        "claude": (),
                    }
                )

                after = {
                    item.account.home: item for item in monitor.account_monitors
                }
                monitor.close()
                removed_state_kept = removed_state_dir.exists()

        # 未变化的账号复用同一个 AccountMonitor（注册表和检查点不重建）。
        self.assertIs(after[home_b], before[home_b])
        self.assertIs(after[home_b].registry, before[home_b].registry)
        # 移除的账号只关闭监控器，状态目录和 sqlite 文件保留在磁盘上。
        self.assertNotIn(home_a, after)
        self.assertEqual(before[home_a].monitor.close_calls, 1)
        self.assertTrue(removed_state_kept)
        # 新增账号已启动，状态目录分配在 accounts/ 下，顺序排在最后。
        self.assertEqual(after[home_c].monitor.start_calls, [True])
        self.assertEqual(
            after[home_c].account.state_dir.parent,
            (root / "state") / "accounts",
        )
        self.assertEqual(
            [item.account.home for item in monitor.account_monitors],
            [home_b, home_c],
        )
        self.assertEqual(
            [account.home for account in monitor.accounts],
            [home_b, home_c],
        )

    def test_new_account_is_not_started_before_monitor_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            home_a = root / ".codex-a"
            home_b = root / ".codex-b"
            home_a.mkdir()
            home_b.mkdir()
            with patch.object(
                MultiAccountMonitor,
                "_create_account_monitor",
                _fake_create_account_monitor,
            ):
                monitor = _build_monitor(root, (home_a,))
                monitor.apply_scan_dirs(
                    {
                        "codex": (home_a, home_b),
                        "grok": (),
                        "kimi": (),
                        "dsh": (),
                        "commandcode": (),
                        "claude": (),
                    }
                )
                added = monitor.account_monitors[-1]
                start_calls_before = list(added.monitor.start_calls)
                monitor.start()
                monitor.close()

        self.assertEqual(start_calls_before, [])
        self.assertEqual(added.monitor.start_calls, [True])

    def test_grok_homes_swap_and_empty_tuple_disables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            grok_a = root / "grok-a"
            grok_b = root / "grok-b"
            grok_a.mkdir()
            grok_b.mkdir()
            with patch.object(
                MultiAccountMonitor,
                "_create_account_monitor",
                _fake_create_account_monitor,
            ):
                monitor = _build_monitor(root, (root / ".codex",))
                (root / ".codex").mkdir(exist_ok=True)
                monitor.apply_scan_dirs(
                    {
                        "codex": (root / ".codex",),
                        "grok": (grok_a,),
                        "kimi": (),
                        "dsh": (),
                        "commandcode": (),
                        "claude": (),
                    }
                )
                swapped = monitor.grok_homes
                labels_after_add = {
                    target.label for target in monitor.housekeeping.targets
                }
                monitor.apply_scan_dirs(
                    {
                        "codex": (root / ".codex",),
                        "grok": (),
                        "kimi": (),
                        "dsh": (),
                        "commandcode": (),
                        "claude": (),
                    }
                )
                disabled = monitor.grok_homes
                labels_after_disable = {
                    target.label for target in monitor.housekeeping.targets
                }
                monitor.close()

        self.assertEqual(swapped, (grok_a,))
        self.assertEqual(disabled, ())
        self.assertIn("Grok", labels_after_add)
        self.assertNotIn("Grok", labels_after_disable)

    def test_housekeeping_targets_follow_codex_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            home_a = root / ".codex-a"
            home_b = root / ".codex-b"
            home_a.mkdir()
            home_b.mkdir()
            with patch.object(
                MultiAccountMonitor,
                "_create_account_monitor",
                _fake_create_account_monitor,
            ):
                monitor = _build_monitor(root, (home_a, home_b))
                name_a = monitor.account_monitors[0].account.name
                monitor.apply_scan_dirs(
                    {
                        "codex": (home_b,),
                        "grok": (),
                        "kimi": (),
                        "dsh": (),
                        "commandcode": (),
                        "claude": (),
                    }
                )
                labels = {
                    target.label for target in monitor.housekeeping.targets
                }
                monitor.close()

        self.assertNotIn(f"Codex ({name_a})", labels)
        self.assertEqual(sum(label.startswith("Codex (") for label in labels), 1)

    def test_updates_aggregators_and_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            (root / ".codex").mkdir()
            grok = root / "grok"
            grok.mkdir()
            with patch.object(
                MultiAccountMonitor,
                "_create_account_monitor",
                _fake_create_account_monitor,
            ):
                monitor = _build_monitor(root, (root / ".codex",))
                dashboard_aggregator = _FakeAggregator()
                advice_aggregator = _FakeAggregator()
                dashboard = _FakeDashboard()
                monitor._dashboard_aggregator = dashboard_aggregator
                monitor._advice_aggregator = advice_aggregator
                monitor._dashboard = dashboard
                monitor.apply_scan_dirs(
                    {
                        "codex": (root / ".codex",),
                        "grok": (grok,),
                        "kimi": (),
                        "dsh": (),
                        "commandcode": (),
                        "claude": (),
                    }
                )

        self.assertEqual(len(dashboard_aggregator.calls), 1)
        self.assertEqual(
            dashboard_aggregator.calls[0]["grok_homes"],
            (grok,),
        )
        self.assertEqual(advice_aggregator.calls, dashboard_aggregator.calls)
        self.assertEqual(len(dashboard.calls), 1)
        registries, metadata = dashboard.calls[0]
        self.assertEqual(set(registries), set(monitor.registries))
        self.assertEqual(set(metadata), set(monitor.dashboard_account_metadata))

    def test_close_clears_dashboard_aggregator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            with patch.object(
                MultiAccountMonitor,
                "_create_account_monitor",
                _fake_create_account_monitor,
            ):
                monitor = _build_monitor(root, ())
                monitor._dashboard_aggregator = _FakeAggregator()
                monitor.close()

        self.assertIsNone(monitor._dashboard_aggregator)


class ConstructorSemanticsTests(unittest.TestCase):
    """验证 provider 目录参数的 None / 空元组语义。"""

    def test_empty_tuple_disables_provider_without_autodetect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            with patch(
                "token_monitor.multi_account.resolve_grok_homes",
                wraps=resolve_grok_homes,
            ) as mocked:
                monitor = MultiAccountMonitor(
                    accounts=(),
                    state_dir=root / "state",
                    grok_homes=(),
                    kimi_homes=(),
                    dsh_homes=(),
                    commandcode_homes=(),
                    claude_homes=(),
                )

        # 显式空元组必须原样传给解析器，禁止回退成自动探测。
        self.assertEqual(mocked.call_args.args[0], ())
        self.assertEqual(monitor.grok_homes, ())

    def test_none_still_autodetects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            with patch(
                "token_monitor.multi_account.resolve_grok_homes",
                wraps=resolve_grok_homes,
            ) as mocked:
                monitor = MultiAccountMonitor(
                    accounts=(),
                    state_dir=root / "state",
                    grok_homes=None,
                    kimi_homes=(),
                    dsh_homes=(),
                    commandcode_homes=(),
                    claude_homes=(),
                )

        self.assertIsNone(mocked.call_args.args[0])
        self.assertEqual(monitor.grok_homes, resolve_grok_homes(None))

    def test_scan_dirs_controller_is_stored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            controller = object()
            monitor = MultiAccountMonitor(
                accounts=(),
                state_dir=root / "state",
                grok_homes=(),
                kimi_homes=(),
                dsh_homes=(),
                commandcode_homes=(),
                claude_homes=(),
                scan_dirs_controller=controller,
            )

        self.assertIs(monitor.scan_dirs_controller, controller)


class HealthInstrumentationTests(unittest.TestCase):
    """验证 daemon 健康埋点:整轮状态、单账号失败隔离和热重载登记。"""

    def test_run_once_marks_main_loop_and_accounts_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            (root / ".codex").mkdir()
            with patch.object(
                MultiAccountMonitor,
                "_create_account_monitor",
                _fake_create_account_monitor,
            ):
                monitor = _build_monitor(root, (root / ".codex",))
                monitor.run_once()
                main_loop = monitor.health.component_status("main-loop")
                account = monitor.health.component_status(
                    f"account:{monitor.account_monitors[0].account.name}"
                )
                monitor.close()

        self.assertEqual(main_loop, "ok")
        self.assertEqual(account, "ok")

    def test_single_account_failure_does_not_stop_others(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            home_a = root / ".codex-a"
            home_b = root / ".codex-b"
            home_a.mkdir()
            home_b.mkdir()
            with patch.object(
                MultiAccountMonitor,
                "_create_account_monitor",
                _fake_create_account_monitor,
            ):
                monitor = _build_monitor(root, (home_a, home_b))
                failing, healthy = monitor.account_monitors
                name_a = failing.account.name
                name_b = healthy.account.name
                failing.monitor.run_once_error = RuntimeError("额度接口超时")

                monitor.run_once()
                failing_status = monitor.health.component_status(f"account:{name_a}")
                healthy_status = monitor.health.component_status(f"account:{name_b}")
                main_loop = monitor.health.component_status("main-loop")

                # 故障恢复后下一轮重新记为成功。
                failing.monitor.run_once_error = None
                monitor.run_once()
                recovered = monitor.health.component_status(f"account:{name_a}")
                monitor.close()

        # 账号 A 抛异常不影响账号 B 的 run_once 执行。
        self.assertEqual(len(healthy.monitor.run_once_calls), 2)
        self.assertEqual(failing_status, "failed")
        self.assertEqual(healthy_status, "ok")
        self.assertEqual(main_loop, "ok")
        self.assertEqual(recovered, "ok")

    def test_apply_scan_dirs_registers_and_unregisters_health(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            home_a = root / ".codex-a"
            home_b = root / ".codex-b"
            home_c = root / ".codex-c"
            for home in (home_a, home_b, home_c):
                home.mkdir()
            with patch.object(
                MultiAccountMonitor,
                "_create_account_monitor",
                _fake_create_account_monitor,
            ):
                monitor = _build_monitor(root, (home_a, home_b))
                name_a = monitor.account_monitors[0].account.name
                monitor.apply_scan_dirs(
                    {
                        "codex": (home_b, home_c),
                        "grok": (),
                        "kimi": (),
                        "dsh": (),
                        "commandcode": (),
                        "claude": (),
                    }
                )
                name_c = monitor.account_monitors[-1].account.name
                removed = monitor.health.component_status(f"account:{name_a}")
                added = monitor.health.component_status(f"account:{name_c}")
                monitor.close()

        self.assertEqual(removed, "unknown")
        # 新增账号已登记但尚未跑过一轮,处于 starting。
        self.assertEqual(added, "starting")


class RetentionWiringTests(unittest.TestCase):
    """验证保留期控制器、历史数据管理器与 daemon 的接线。"""

    def test_controller_effective_values_reach_history_manager(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            with patch.object(
                MultiAccountMonitor,
                "_create_account_monitor",
                _fake_create_account_monitor,
            ):
                monitor = _build_monitor(
                    root,
                    (),
                    config=MonitorConfig(
                        auto_resume=False,
                        usage_retention_days=45.0,
                        session_retention_days=7.0,
                    ),
                )
                initial = monitor._history_manager.retention_days
                # Web 侧修改通过 reload 回调热生效。
                monitor.retention_controller.apply("set", usage_days=10.0)
                updated = monitor._history_manager.retention_days
                persisted = monitor.retention_controller.effective()

        self.assertEqual(initial["usage_days"], 45.0)
        self.assertEqual(initial["session_days"], 7.0)
        self.assertEqual(updated["usage_days"], 10.0)
        self.assertEqual(updated["session_days"], 7.0)
        self.assertEqual(persisted, {"usage_days": 10.0, "session_days": 7.0})

    def test_cleanup_failure_marks_component_failed_then_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            with patch.object(
                MultiAccountMonitor,
                "_create_account_monitor",
                _fake_create_account_monitor,
            ):
                monitor = _build_monitor(root, ())
                with patch.object(
                    monitor._history_manager,
                    "cleanup",
                    side_effect=RetentionError("模拟磁盘只读"),
                ):
                    monitor._check_advice(1000.0)
                    failed = monitor.health.component_status("history-cleanup")
                with patch.object(
                    monitor._history_manager,
                    "cleanup",
                    return_value={"deleted": {"usage": 0}, "freed_bytes": 0},
                ):
                    # 失败后按天节流,下一天重试成功后恢复 ok。
                    monitor._check_advice(1000.0 + 86400 + 61)
                    recovered = monitor.health.component_status("history-cleanup")

        self.assertEqual(failed, "failed")
        self.assertEqual(recovered, "ok")

    def test_cleanup_runs_once_per_day(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            with patch.object(
                MultiAccountMonitor,
                "_create_account_monitor",
                _fake_create_account_monitor,
            ):
                monitor = _build_monitor(root, ())
                with patch.object(
                    monitor._history_manager,
                    "cleanup",
                    return_value={"deleted": {}, "freed_bytes": 0},
                ) as cleanup:
                    # 首次巡检立即清理一次;同一天的后续巡检不再重复。
                    monitor._check_advice(1000.0)
                    monitor._check_advice(1000.0 + 61)
                    monitor._check_advice(1000.0 + 3600)
                    within_day = cleanup.call_count
                    monitor._check_advice(1000.0 + 86400 + 1)
                    next_day = cleanup.call_count

        self.assertEqual(within_day, 1)
        self.assertEqual(next_day, 2)


if __name__ == "__main__":
    unittest.main()
