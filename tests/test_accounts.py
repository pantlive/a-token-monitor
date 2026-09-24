"""多个 Codex 登录目录的配置和监控隔离测试。"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from _platform_support import requires_proc
from token_monitor.accounts import (
    _path_suffix,
    build_account_specs,
    build_additional_account_spec,
)
from token_monitor.alerts import AlertStoreError
from token_monitor.multi_models import (
    DetectionConfidence,
    SessionStatus,
    TrackedSession,
)
from token_monitor.monitor import MonitorConfig
from token_monitor.multi_account import MultiAccountMonitor
from token_monitor.traffic import TrafficAlert
from token_monitor.quota import QuotaSnapshot, QuotaWindow


class _FakeAppServer:
    """返回固定额度的账号级 App Server。"""

    configs: list[object] = []

    def __init__(self, config: object, **kwargs: object) -> None:
        self.config = config
        self.__class__.configs.append(config)

    def start(self) -> None:
        """模拟启动 App Server。"""

    def close(self) -> None:
        """模拟关闭 App Server。"""

    def drain_notifications(self) -> int:
        """模拟没有待处理通知。"""

        return 0

    def read_rate_limits(self, now: float | None = None) -> QuotaSnapshot:
        """返回未耗尽的窗口。"""

        return QuotaSnapshot(
            observed_at=now or 100,
            plan_type="plus",
            windows=(
                QuotaWindow(
                    limit_id="codex",
                    name="primary",
                    used_percent=10,
                    window_minutes=300,
                    resets_at=200,
                ),
            ),
        )

    def list_threads(self) -> list[dict[str, object]]:
        """模拟没有 App Server 会话。"""

        return []


class AccountTests(unittest.TestCase):
    """验证账号目录、状态目录和 App Server 上下文彼此隔离。"""

    def test_builds_independent_specs_and_preserves_default_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            personal = root / ".codex"
            company = root / ".codex-work"
            state_dir = root / "monitor-state"

            accounts = build_account_specs(
                homes=(personal, company),
                state_dir=state_dir,
            )

        self.assertEqual(
            [account.name for account in accounts],
            ["codex", "codex-work"],
        )
        self.assertEqual(accounts[0].home, personal.resolve())
        self.assertEqual(accounts[0].session_root, personal.resolve() / "sessions")
        self.assertEqual(accounts[0].state_dir, state_dir)
        self.assertEqual(accounts[1].session_root, company.resolve() / "sessions")
        self.assertEqual(
            accounts[1].state_dir,
            state_dir / "accounts" / "codex-work",
        )

    def test_multi_account_monitor_sets_codex_home_per_app_server(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            accounts = build_account_specs(
                homes=(root / ".codex", root / ".codex-work"),
                state_dir=root / "state",
            )
            _FakeAppServer.configs = []
            with patch(
                "token_monitor.monitor.AppServerClient",
                _FakeAppServer,
            ):
                monitor = MultiAccountMonitor(
                    accounts=accounts,
                    state_dir=root / "state",
                    config=MonitorConfig(auto_resume=False),
                )
                monitor.start()
                monitor.close()

        homes = [getattr(config, "codex_home") for config in _FakeAppServer.configs]
        self.assertEqual(homes, [accounts[0].home, accounts[1].home])
        self.assertEqual(set(monitor.registries), {"codex", "codex-work"})

    def test_reads_account_id_without_exposing_auth_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            personal = root / ".codex"
            personal.mkdir()
            (personal / "auth.json").write_text(
                json.dumps(
                    {
                        "tokens": {
                            "account_id": "account-personal",
                            "access_token": "must-not-be-returned",
                        }
                    }
                ),
                encoding="utf-8",
            )

            accounts = build_account_specs(
                homes=(personal,),
                state_dir=root / "state",
            )

        self.assertEqual(accounts[0].account_id, "account-personal")
        self.assertEqual(accounts[0].identity_key, "account-personal")

    def test_traffic_alerts_are_persisted_to_state_directory(self) -> None:
        """新告警应落盘到状态目录，供 Dashboard 和 alerts 命令查询。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            accounts = build_account_specs(
                homes=(root / ".codex",),
                state_dir=root / "state",
            )
            _FakeAppServer.configs = []
            with patch(
                "token_monitor.monitor.AppServerClient",
                _FakeAppServer,
            ):
                monitor = MultiAccountMonitor(
                    accounts=accounts,
                    state_dir=root / "state",
                    config=MonitorConfig(auto_resume=False),
                )
                with self.assertLogs("token_monitor.multi_account", level="ERROR"):
                    monitor._record_alerts([_sample_alert()])
                stored = monitor.alert_store.query()
                database_file = monitor.alert_store.database_file
                database_exists = database_file.exists()
                monitor.close()

        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].process_key, "codex:9:99")
        self.assertEqual(stored[0].level, "danger")
        self.assertTrue(database_exists)
        self.assertEqual(
            database_file,
            Path(temporary_directory) / "state" / "traffic-alerts.sqlite3",
        )

    def test_alert_store_failure_does_not_break_recording(self) -> None:
        """落盘失败时只记录错误日志，不影响监控主循环。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            accounts = build_account_specs(
                homes=(root / ".codex",),
                state_dir=root / "state",
            )
            _FakeAppServer.configs = []
            with patch(
                "token_monitor.monitor.AppServerClient",
                _FakeAppServer,
            ):
                monitor = MultiAccountMonitor(
                    accounts=accounts,
                    state_dir=root / "state",
                    config=MonitorConfig(auto_resume=False),
                )
                with (
                    patch.object(
                        monitor.alert_store,
                        "record",
                        side_effect=AlertStoreError("磁盘不可写"),
                    ),
                    self.assertLogs(
                        "token_monitor.multi_account",
                        level="ERROR",
                    ) as captured,
                ):
                    monitor._record_alerts([_sample_alert()])
                monitor.close()

        self.assertTrue(
            any("落盘失败" in message for message in captured.output)
        )


    def test_housekeeping_targets_and_advice_logging(self) -> None:
        """daemon 应统计 codex 目录占用，并按冷却时间记录提醒。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            session_dir = home / "sessions" / "2026" / "06" / "01"
            session_dir.mkdir(parents=True)
            stale = (
                session_dir
                / "rollout-2026-06-01T01-00-00-88888888-8888-4888-8888-888888888888.jsonl"
            )
            stale.write_bytes(b"x" * 4096)
            old_stamp = time.time() - 100 * 86_400
            os.utime(stale, (old_stamp, old_stamp))
            accounts = build_account_specs(
                homes=(home,),
                state_dir=root / "state",
            )
            _FakeAppServer.configs = []
            with patch(
                "token_monitor.monitor.AppServerClient",
                _FakeAppServer,
            ):
                monitor = MultiAccountMonitor(
                    accounts=accounts,
                    state_dir=root / "state",
                    config=MonitorConfig(
                        auto_resume=False,
                        disk_warn_gb=0.000001,
                        disk_total_warn_gb=0.000001,
                    ),
                    grok_homes=(),
                    kimi_homes=(),
                    dsh_homes=(),
                    commandcode_homes=(),
                    claude_homes=(),
                )
                labels = [target.label for target in monitor.housekeeping.targets]
                with self.assertLogs(
                    "token_monitor.multi_account",
                    level="WARNING",
                ) as captured:
                    monitor._check_advice(1_000.0)
                    after_first = len(captured.output)
                    monitor._check_advice(1_030.0)
                    after_throttled = len(captured.output)
                    monitor._check_advice(1_000.0 + 3_600.0)
                    after_cooldown = len(captured.output)
                monitor.close()

        self.assertIn("Codex (codex)", labels)
        self.assertIn("监控状态目录", labels)
        self.assertTrue(
            any("磁盘占用提醒" in line for line in captured.output)
        )
        self.assertTrue(any("Codex (codex)" in line for line in captured.output))
        self.assertEqual(after_throttled, after_first)
        self.assertGreater(after_cooldown, after_throttled)

    def test_active_session_paths_protect_running_sessions(self) -> None:
        """有进程的活动会话路径应被收集，供归档和清理跳过。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            accounts = build_account_specs(
                homes=(root / ".codex",),
                state_dir=root / "state",
            )
            _FakeAppServer.configs = []
            with patch(
                "token_monitor.monitor.AppServerClient",
                _FakeAppServer,
            ):
                monitor = MultiAccountMonitor(
                    accounts=accounts,
                    state_dir=root / "state",
                    config=MonitorConfig(auto_resume=False),
                )
                monitor.account_monitors[0].registry.upsert_session(
                    TrackedSession(
                        thread_id="thread-active",
                        session_id="session-active",
                        jsonl_path=str(root / "sessions" / "active.jsonl"),
                        cwd=str(root),
                        source="cli",
                        status=SessionStatus.RUNNING,
                        confidence=DetectionConfidence.OPEN_FILE,
                        first_seen_at=1,
                        last_seen_at=2,
                        pids=(1234,),
                    )
                )
                paths = monitor._active_session_paths()
                monitor.close()

        self.assertEqual(paths, {str(root / "sessions" / "active.jsonl")})


    def test_build_account_specs_without_codex_home_returns_empty(self) -> None:
        """没有默认 CODEX_HOME 时返回零账号，显式目录仍按配置生效。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            missing = root / "missing-codex-home"
            with patch.dict(os.environ, {"CODEX_HOME": str(missing)}):
                default_accounts = build_account_specs(
                    None,
                    state_dir=root / "state",
                )
            explicit = build_account_specs(
                (root / ".codex-new",),
                state_dir=root / "state",
            )
            empty = build_account_specs((), state_dir=root / "state")

        self.assertEqual(default_accounts, ())
        self.assertEqual(empty, ())
        self.assertEqual(len(explicit), 1)

    @requires_proc
    def test_monitor_without_codex_accounts_still_runs(self) -> None:
        """零 Codex 账号时 daemon 仍能跑一轮（流量与磁盘提醒）。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            provider_root = root / "missing"
            _FakeAppServer.configs = []
            with patch(
                "token_monitor.monitor.AppServerClient",
                _FakeAppServer,
            ):
                monitor = MultiAccountMonitor(
                    accounts=(),
                    state_dir=root / "state",
                    config=MonitorConfig(auto_resume=False),
                    grok_homes=(provider_root / "grok",),
                    kimi_homes=(provider_root / "kimi",),
                    dsh_homes=(provider_root / "dsh",),
                    commandcode_homes=(provider_root / "commandcode",),
                    claude_homes=(provider_root / "claude",),
                )
                registries = dict(monitor.registries)
                metadata = dict(monitor.dashboard_account_metadata)
                monitor.run_once(now=1_000.0)
                labels = {
                    target.label
                    for target in monitor.housekeeping.targets
                }
                monitor.close()

        self.assertEqual(registries, {})
        self.assertEqual(metadata, {})
        # 缺失的 provider 目录不进入磁盘审计目标
        self.assertLessEqual(labels, {"监控状态目录"})

    def test_monitor_requires_state_dir_without_accounts(self) -> None:
        """零账号又没有 state_dir 时给出明确错误。"""

        with self.assertRaises(ValueError):
            MultiAccountMonitor(accounts=(), state_dir=None)


class AdditionalAccountSpecTests(unittest.TestCase):
    """验证热新增账号的命名和状态目录分配约定。"""

    def test_allocates_state_dir_under_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            state_dir = root / "state"
            existing = build_account_specs((root / ".codex",), state_dir=state_dir)

            added = build_additional_account_spec(
                root / ".codex-work",
                state_dir,
                existing,
            )

        self.assertEqual(added.name, "codex-work")
        self.assertEqual(added.home, root / ".codex-work")
        self.assertEqual(added.session_root, root / ".codex-work" / "sessions")
        self.assertEqual(added.state_dir, state_dir / "accounts" / "codex-work")

    def test_readding_default_home_reuses_legacy_state_dir(self) -> None:
        """默认目录被重新加回空账号集时复用旧 state_dir，保住历史检查点。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            state_dir = root / "state"
            default_home = root / "default-codex"
            with patch.dict(os.environ, {"CODEX_HOME": str(default_home)}):
                added = build_additional_account_spec(default_home, state_dir, [])

        self.assertEqual(added.state_dir, state_dir)
        self.assertEqual(added.home, default_home)

    def test_default_home_with_existing_accounts_uses_accounts_dir(self) -> None:
        """已有账号占用旧 state_dir 时，默认目录也只能分配到 accounts/ 下。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            state_dir = root / "state"
            default_home = root / "default-codex"
            existing = build_account_specs((root / ".codex-other",), state_dir)
            self.assertEqual(existing[0].state_dir, state_dir)
            with patch.dict(os.environ, {"CODEX_HOME": str(default_home)}):
                added = build_additional_account_spec(
                    default_home,
                    state_dir,
                    existing,
                )

        self.assertNotEqual(added.state_dir, state_dir)
        self.assertEqual(added.state_dir.parent, state_dir / "accounts")

    def test_same_dirname_gets_unique_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            state_dir = root / "state"
            existing = build_account_specs((root / ".codex",), state_dir=state_dir)

            added = build_additional_account_spec(
                root / "work" / ".codex",
                state_dir,
                existing,
            )

        self.assertEqual(existing[0].name, "codex")
        self.assertTrue(added.name.startswith("codex-"))
        self.assertNotEqual(added.name, existing[0].name)

    def test_state_dir_collision_gets_path_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            state_dir = root / "state"
            existing = build_account_specs(
                (root / ".codex", root / ".codex-work"),
                state_dir=state_dir,
            )
            colliding_home = root / "other" / ".codex-work"

            added = build_additional_account_spec(colliding_home, state_dir, existing)

        suffix = _path_suffix(colliding_home)
        self.assertEqual(added.name, f"codex-work-{suffix}")
        self.assertEqual(
            added.state_dir,
            state_dir / "accounts" / f"codex-work-{suffix}",
        )
        self.assertNotIn(
            added.state_dir,
            {account.state_dir for account in existing},
        )

    def test_reads_account_id_from_auth_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            state_dir = root / "state"
            home = root / ".codex-work"
            home.mkdir()
            (home / "auth.json").write_text(
                json.dumps({"tokens": {"account_id": "account-hot-added"}}),
                encoding="utf-8",
            )
            existing = build_account_specs((root / ".codex",), state_dir=state_dir)

            added = build_additional_account_spec(home, state_dir, existing)

        self.assertEqual(added.account_id, "account-hot-added")


def _sample_alert() -> TrafficAlert:
    """构造一条用于编排层测试的告警。"""

    return TrafficAlert(
        level="danger",
        product="codex",
        pid=9,
        kind="burst",
        bytes=40 * 1024 * 1024,
        window_seconds=15.0,
        message="codex pid 9 在 15 秒内向外发送 40.0 MiB",
        observed_at=1000.0,
        remote="203.0.113.10:443",
        process_key="codex:9:99",
        command="codex",
        cwd="/home/dev/project",
    )


if __name__ == "__main__":
    unittest.main()
