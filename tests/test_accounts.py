"""多个 Codex 登录目录的配置和监控隔离测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_reset_monitor.accounts import build_account_specs
from codex_reset_monitor.monitor import MonitorConfig
from codex_reset_monitor.multi_account import MultiAccountMonitor
from codex_reset_monitor.quota import QuotaSnapshot, QuotaWindow


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
                "codex_reset_monitor.monitor.AppServerClient",
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


if __name__ == "__main__":
    unittest.main()
