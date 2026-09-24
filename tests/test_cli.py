"""命令行多账号参数测试。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _platform_support import requires_proc
from a_token_monitor.accounts import build_account_specs
from a_token_monitor.alerts import TrafficAlertStore
from a_token_monitor.cli import (
    _monitor,
    _service_config,
    _session_usage_note,
    build_parser,
    default_state_dir,
    main,
)
from a_token_monitor.retention import (
    DEFAULT_SESSION_RETENTION_DAYS,
    DEFAULT_USAGE_RETENTION_DAYS,
)
from a_token_monitor.scan_dirs import ScanDirsConfig, ScanDirsController
from a_token_monitor.service import ServiceConfig
from a_token_monitor.housekeeping import DEFAULT_TOTAL_WARN_GIB
from a_token_monitor.usage import (
    DEFAULT_SESSION_TURN_WARN,
)
from a_token_monitor.quota import QuotaSnapshot, QuotaWindow
from a_token_monitor.registry import MultiSessionRegistry
from a_token_monitor.traffic import TrafficAlert
from a_token_monitor.usage import UsageAggregator


def _sample_alert(observed_at: float | None = None) -> TrafficAlert:
    """构造一条用于命令行测试的历史告警。"""

    return TrafficAlert(
        level="danger",
        product="codex",
        pid=9,
        kind="burst",
        bytes=40 * 1024 * 1024,
        window_seconds=15.0,
        message="codex pid 9 在 15 秒内向外发送 40.0 MiB",
        observed_at=time.time() if observed_at is None else observed_at,
        remote="203.0.113.10:443",
        process_key="codex:9:99",
        command="codex",
        cwd="/home/dev/project",
    )


def _missing_provider_env(root: Path) -> dict[str, str]:
    """把所有 provider 的默认目录指向不存在的路径，隔离自动探测。"""

    return {
        "CODEX_HOME": str(root / "missing-codex"),
        "GROK_HOME": str(root / "missing-grok"),
        "KIMI_CODE_HOME": str(root / "missing-kimi"),
        "DSH_HOME": str(root / "missing-dsh"),
        "COMMANDCODE_HOME": str(root / "missing-commandcode"),
        "CLAUDE_CONFIG_DIR": str(root / "missing-claude"),
    }


class CliTests(unittest.TestCase):
    """验证多个账号参数能被重复传入并保持顺序。"""

    def test_parser_accepts_repeated_grok_home(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--grok-home",
                "~/.grok",
                "--grok-home",
                "~/.grok-work",
                "daemon",
            ]
        )

        self.assertEqual(
            args.grok_homes,
            [Path("~/.grok"), Path("~/.grok-work")],
        )

    def test_parser_accepts_repeated_kimi_home(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--kimi-home",
                "~/.kimi-code",
                "--kimi-home",
                "~/.kimi-code-work",
                "daemon",
            ]
        )

        self.assertEqual(
            args.kimi_homes,
            [Path("~/.kimi-code"), Path("~/.kimi-code-work")],
        )

    def test_parser_accepts_repeated_dsh_home(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--dsh-home",
                "~/.dsh",
                "--dsh-home",
                "~/.dsh-work",
                "daemon",
            ]
        )

        self.assertEqual(
            args.dsh_homes,
            [Path("~/.dsh"), Path("~/.dsh-work")],
        )

    def test_parser_accepts_repeated_codex_home(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--codex-home",
                "~/.codex",
                "--codex-home",
                "~/.codex-work",
                "daemon",
            ]
        )

        self.assertEqual(
            args.codex_homes,
            [Path("~/.codex"), Path("~/.codex-work")],
        )

    def test_session_root_cannot_be_shared_by_multiple_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaisesRegex(ValueError, "session-root"):
                build_account_specs(
                    homes=(root / ".codex", root / ".codex-work"),
                    state_dir=root / "state",
                    session_root=root / "one-session-root",
                )

    def test_service_install_accepts_daemon_options(self) -> None:
        """后台安装命令应复用 daemon 的多账号和 Dashboard 参数。"""

        parser = build_parser()
        args = parser.parse_args(
            [
                "--state-dir",
                "~/.a-token-monitor",
                "--codex-home",
                "~/.codex",
                "--codex-home",
                "~/.codex-work",
                "service",
                "install",
                "--dashboard",
                "--dashboard-host",
                "0.0.0.0",
            ]
        )

        self.assertEqual(args.command, "service")
        self.assertEqual(args.service_action, "install")
        self.assertEqual(
            args.codex_homes,
            [Path("~/.codex"), Path("~/.codex-work")],
        )
        self.assertTrue(args.dashboard)
        self.assertEqual(args.dashboard_host, "0.0.0.0")

    def test_recovery_commands_and_options_are_removed(self) -> None:
        """命令行不再提供额度中断后的恢复入口。"""

        parser = build_parser()
        for command in ("run", "watch", "resume"):
            with self.assertRaises(SystemExit):
                parser.parse_args([command])
        with self.assertRaises(SystemExit):
            parser.parse_args(["daemon", "--no-auto-resume"])

        daemon_args = parser.parse_args(["daemon"])
        self.assertFalse(hasattr(daemon_args, "auto_resume"))

    def test_parser_accepts_traffic_command_and_upload_thresholds(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "traffic",
                "--json",
                "--upload-warn-mb",
                "4",
                "--upload-alert-mb",
                "16",
            ]
        )

        self.assertEqual(args.command, "traffic")
        self.assertTrue(args.json)
        self.assertEqual(args.upload_warn_mb, 4.0)
        self.assertEqual(args.upload_alert_mb, 16.0)

        daemon_args = parser.parse_args(
            ["daemon", "--upload-window-warn-mb", "40"]
        )
        self.assertEqual(daemon_args.upload_window_warn_mb, 40.0)

    def test_daemon_and_service_install_accept_budget_usd(self) -> None:
        """daemon 和 service install 都应接受可选的月度预算参数。"""

        parser = build_parser()
        args = parser.parse_args(["daemon", "--budget-usd", "25.5"])
        self.assertEqual(args.budget_usd, 25.5)

        install_args = parser.parse_args(["service", "install"])
        self.assertIsNone(install_args.budget_usd)

        for invalid in ("0", "-3"):
            with self.assertRaises(SystemExit):
                parser.parse_args(["daemon", "--budget-usd", invalid])

    def test_parser_accepts_retention_days_options(self) -> None:
        """daemon 和 service install 应接受用量与会话历史保留天数参数。"""

        parser = build_parser()
        daemon_args = parser.parse_args(["daemon"])
        self.assertEqual(
            daemon_args.usage_retention_days,
            DEFAULT_USAGE_RETENTION_DAYS,
        )
        self.assertEqual(
            daemon_args.session_retention_days,
            DEFAULT_SESSION_RETENTION_DAYS,
        )

        explicit = parser.parse_args(
            [
                "daemon",
                "--usage-retention-days",
                "45",
                "--session-retention-days",
                "7.5",
            ]
        )
        self.assertEqual(explicit.usage_retention_days, 45.0)
        self.assertEqual(explicit.session_retention_days, 7.5)

        install_args = parser.parse_args(
            ["service", "install", "--usage-retention-days", "120"]
        )
        self.assertEqual(install_args.usage_retention_days, 120.0)
        self.assertEqual(
            install_args.session_retention_days,
            DEFAULT_SESSION_RETENTION_DAYS,
        )

        for invalid in ("0", "-3"):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    ["daemon", "--usage-retention-days", invalid]
                )
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    ["daemon", "--session-retention-days", invalid]
                )

    def test_service_config_passes_retention_days(self) -> None:
        """service install 参数中的保留天数应透传到持久化配置。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = build_parser().parse_args(
                [
                    "--state-dir",
                    str(root / "state"),
                    "service",
                    "install",
                    "--usage-retention-days",
                    "45",
                    "--session-retention-days",
                    "7.5",
                ]
            )
            with mock.patch.dict(os.environ, _missing_provider_env(root)):
                config = _service_config(args)

        self.assertEqual(config.usage_retention_days, 45.0)
        self.assertEqual(config.session_retention_days, 7.5)

    def test_default_state_dir_reuses_legacy_directory(self) -> None:
        """改名后旧状态目录存在时应继续使用，避免升级丢失历史。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            legacy = home / ".codex-reset-monitor"
            legacy.mkdir()
            with mock.patch("a_token_monitor.cli.Path.home", return_value=home):
                reused = default_state_dir()

            self.assertEqual(reused, legacy)

            # 新建目录后自动切换到新位置。
            (home / ".a-token-monitor").mkdir()
            with mock.patch("a_token_monitor.cli.Path.home", return_value=home):
                switched = default_state_dir()

            self.assertEqual(switched, home / ".a-token-monitor")

    def test_default_state_dir_reuses_intermediate_directory(self) -> None:
        """上一代 ~/.token-monitor 同样回退，且优先于更早的 codex-reset-monitor。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            intermediate = home / ".token-monitor"
            intermediate.mkdir()
            with mock.patch("a_token_monitor.cli.Path.home", return_value=home):
                reused = default_state_dir()

            self.assertEqual(reused, intermediate)

            (home / ".codex-reset-monitor").mkdir()
            with mock.patch("a_token_monitor.cli.Path.home", return_value=home):
                still_intermediate = default_state_dir()

            self.assertEqual(still_intermediate, intermediate)

    def test_default_state_dir_uses_new_name_by_default(self) -> None:
        """没有任何历史目录时使用新的 ~/.a-token-monitor。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            with mock.patch("a_token_monitor.cli.Path.home", return_value=home):
                resolved = default_state_dir()

            self.assertEqual(resolved, home / ".a-token-monitor")

    def test_quota_command_prints_kimi_windows(self) -> None:
        """quota 子命令应展示 Kimi /usages 返回的窗口。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            kimi_home = root / ".kimi-code"
            credentials = kimi_home / "credentials"
            credentials.mkdir(parents=True)
            (credentials / "kimi-code.json").write_text(
                json.dumps({"access_token": "SECRET-TOKEN"}),
                encoding="utf-8",
            )
            snapshot = QuotaSnapshot(
                observed_at=1_789_700_000.0,
                windows=(
                    QuotaWindow(
                        limit_id="kimi",
                        name="limit_5h",
                        used_percent=25.0,
                        window_minutes=300.0,
                        resets_at=1_789_730_000.0,
                    ),
                ),
                source="kimi-api",
                raw_limit_ids=("kimi",),
            )
            with (
                mock.patch.dict(os.environ, _missing_provider_env(root)),
                mock.patch("a_token_monitor.cli._accounts", return_value=()),
                mock.patch(
                    "a_token_monitor.cli.read_kimi_quota",
                    return_value=snapshot,
                ),
            ):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    exit_code = main(
                        [
                            "--state-dir",
                            str(root / "state"),
                            "--kimi-home",
                            str(kimi_home),
                            "quota",
                        ]
                    )

            self.assertEqual(exit_code, 0)
            output = buffer.getvalue()
            self.assertIn("kimi/limit_5h", output)
            self.assertIn("25%", output)
            self.assertIn("已登录", output)
            self.assertNotIn("SECRET-TOKEN", output)

    def test_quota_command_notes_kimi_quota_failure(self) -> None:
        """Kimi 配额读取失败时应提示但不影响退出码。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            kimi_home = root / ".kimi-code"
            credentials = kimi_home / "credentials"
            credentials.mkdir(parents=True)
            (credentials / "kimi-code.json").write_text(
                json.dumps({"access_token": "SECRET-TOKEN"}),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(os.environ, _missing_provider_env(root)),
                mock.patch("a_token_monitor.cli._accounts", return_value=()),
                mock.patch(
                    "a_token_monitor.cli.read_kimi_quota",
                    return_value=None,
                ),
            ):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    exit_code = main(
                        [
                            "--state-dir",
                            str(root / "state"),
                            "--kimi-home",
                            str(kimi_home),
                            "quota",
                        ]
                    )

            self.assertEqual(exit_code, 0)
            self.assertIn("配额暂不可读", buffer.getvalue())
            self.assertNotIn("SECRET-TOKEN", buffer.getvalue())

    def test_parser_accepts_repeated_commandcode_home(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--commandcode-home",
                "~/.commandcode",
                "--commandcode-home",
                "~/.commandcode-work",
                "daemon",
            ]
        )

        self.assertEqual(
            args.commandcode_homes,
            [Path("~/.commandcode"), Path("~/.commandcode-work")],
        )

    def test_quota_command_prints_commandcode_windows(self) -> None:
        """quota 子命令应展示 Command Code 订阅额度窗口和本月扣费。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".commandcode"
            home.mkdir(parents=True)
            (home / "auth.json").write_text(
                json.dumps(
                    {
                        "apiKey": "SECRET-API-KEY",
                        "userId": "a8f7ddce-358a-4441-9d10-de053e64c79f",
                        "userName": "tester",
                    }
                ),
                encoding="utf-8",
            )
            snapshot = QuotaSnapshot(
                observed_at=1_789_490_000.0,
                windows=(
                    QuotaWindow(
                        limit_id="command-code",
                        name="5-hour",
                        used_percent=3.85,
                        window_minutes=300.0,
                        resets_at=1_789_493_000.0,
                    ),
                ),
                plan_type="GOAT",
                source="command-code-api",
                raw_limit_ids=("command-code",),
                metadata={
                    "period_credits_spent": "28.69",
                    "monthly_credits_remaining": "41.63",
                    "days_left": "17",
                    "period_requests": "5095",
                },
            )
            with (
                mock.patch.dict(os.environ, _missing_provider_env(root)),
                mock.patch("a_token_monitor.cli._accounts", return_value=()),
                mock.patch(
                    "a_token_monitor.cli.read_commandcode_quota",
                    return_value=snapshot,
                ),
            ):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    exit_code = main(
                        [
                            "--state-dir",
                            str(root / "state"),
                            "--commandcode-home",
                            str(home),
                            "quota",
                        ]
                    )

            self.assertEqual(exit_code, 0)
            output = buffer.getvalue()
            self.assertIn("command-code/5-hour", output)
            self.assertIn("3.85%", output)
            self.assertIn("套餐: GOAT", output)
            self.assertIn("已登录", output)
            self.assertIn("28.69", output)
            self.assertNotIn("SECRET-API-KEY", output)

    def test_quota_command_notes_commandcode_quota_failure(self) -> None:
        """Command Code 配额读取失败时应提示但不影响退出码。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".commandcode"
            home.mkdir(parents=True)
            (home / "auth.json").write_text(
                json.dumps({"apiKey": "SECRET-API-KEY", "userName": "tester"}),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(os.environ, _missing_provider_env(root)),
                mock.patch("a_token_monitor.cli._accounts", return_value=()),
                mock.patch(
                    "a_token_monitor.cli.read_commandcode_quota",
                    return_value=None,
                ),
            ):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    exit_code = main(
                        [
                            "--state-dir",
                            str(root / "state"),
                            "--commandcode-home",
                            str(home),
                            "quota",
                        ]
                    )

            output = buffer.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("配额暂不可读", output)
            self.assertNotIn("SECRET-API-KEY", output)

    def test_parser_accepts_alerts_command_filters(self) -> None:
        """alerts 子命令应接受历史和已读相关筛选参数。"""

        parser = build_parser()
        args = parser.parse_args(
            [
                "alerts",
                "--days",
                "7",
                "--level",
                "danger",
                "--kind",
                "burst",
                "--product",
                "codex",
                "--unread",
                "--query",
                "上传",
                "--limit",
                "20",
                "--json",
            ]
        )

        self.assertEqual(args.command, "alerts")
        self.assertEqual(args.days, 7.0)
        self.assertEqual(args.level, "danger")
        self.assertEqual(args.kind, "burst")
        self.assertEqual(args.product, "codex")
        self.assertTrue(args.unread)
        self.assertEqual(args.query, "上传")
        self.assertEqual(args.limit, 20)
        self.assertTrue(args.json)

        daemon_args = parser.parse_args(
            ["daemon", "--alert-retention-days", "14"]
        )
        self.assertEqual(daemon_args.alert_retention_days, 14.0)
        install_args = parser.parse_args(["service", "install"])
        self.assertEqual(install_args.alert_retention_days, 30.0)

    def test_alerts_command_reads_persisted_history(self) -> None:
        """alerts 命令应读取落盘告警，并支持标记已读和清理预览。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            store = TrafficAlertStore(state_dir)
            store.record([_sample_alert(time.time() - 3 * 86400)], now=time.time())

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                listed = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "alerts",
                        "--json",
                    ]
                )
            payload = json.loads(buffer.getvalue())

            self.assertEqual(listed, 1)
            self.assertEqual(payload["stats"]["total"], 1)
            self.assertEqual(payload["stats"]["unread"], 1)
            self.assertEqual(payload["alerts"][0]["process_key"], "codex:9:99")
            self.assertNotIn("SECRET", buffer.getvalue())

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                acked = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "alerts",
                        "--ack-all",
                    ]
                )
            self.assertEqual(acked, 0)
            self.assertIn("标记为已读", buffer.getvalue())
            self.assertEqual(store.stats()["unread"], 0)

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                quiet = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "alerts",
                        "--unread",
                        "--quiet",
                    ]
                )
            self.assertEqual(quiet, 0)
            self.assertEqual(buffer.getvalue(), "")

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                preview = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "alerts",
                        "--clear-before",
                        "1",
                        "--dry-run",
                    ]
                )
            self.assertEqual(preview, 0)
            self.assertIn("预览", buffer.getvalue())
            self.assertEqual(store.stats()["total"], 1)

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cleared = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "alerts",
                        "--clear-before",
                        "1",
                    ]
                )
            self.assertEqual(cleared, 0)
            self.assertEqual(store.stats()["total"], 0)

    def test_alerts_command_requires_confirmation_for_clear_all(self) -> None:
        """--clear-all 必须显式带上 --yes。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            store = TrafficAlertStore(state_dir)
            store.record([_sample_alert()], now=time.time())

            failed = main(["--state-dir", str(state_dir), "alerts", "--clear-all"])
            self.assertEqual(failed, 2)
            self.assertEqual(store.stats()["total"], 1)

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                confirmed = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "alerts",
                        "--clear-all",
                        "--yes",
                    ]
                )
            self.assertEqual(confirmed, 0)
            self.assertEqual(store.stats()["total"], 0)


    def test_usage_command_searches_token_history(self) -> None:
        """usage 子命令应按日期、模型和会话检索用量索引。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_dir = root / "state"
            _write_usage_index(root)
            aggregator = UsageAggregator(
                discovery_interval=0.01,
                refresh_interval=0.01,
                cache_path=state_dir / "usage-index.sqlite3",
            )
            aggregator.snapshot(
                {"codex": MultiSessionRegistry(state_dir)},
                account_metadata={
                    "codex": {
                        "account_id": "account-personal",
                        "profile_name": "codex",
                        "codex_home": str(root / ".codex"),
                    }
                },
                now=time.time(),
            )

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                listed = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "usage",
                        "--days",
                        "0",
                        "--json",
                    ]
                )
            payload = json.loads(buffer.getvalue())

            self.assertEqual(listed, 0)
            self.assertTrue(payload["search"]["available"])
            self.assertEqual(payload["search"]["totals"]["records"], 2)
            self.assertEqual(payload["search"]["totals"]["total_tokens"], 9_000)
            self.assertEqual(payload["facets"]["models"], ["gpt-5.6-luna"])

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                filtered = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "usage",
                        "--days",
                        "0",
                        "--session",
                        "44444444",
                        "--limit",
                        "5",
                    ]
                )
            self.assertEqual(filtered, 0)
            self.assertIn("44444444", buffer.getvalue())
            self.assertIn("9,000", buffer.getvalue())

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                by_model = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "usage",
                        "--days",
                        "0",
                        "--group",
                        "model",
                    ]
                )
            self.assertEqual(by_model, 0)
            self.assertIn("gpt-5.6-luna", buffer.getvalue())

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                by_account = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "usage",
                        "--days",
                        "0",
                        "--group",
                        "account",
                        "--json",
                    ]
                )
            account_payload = json.loads(buffer.getvalue())

            self.assertEqual(by_account, 0)
            self.assertEqual(account_payload["search"]["group"], "account")
            self.assertEqual(
                [row["account"] for row in account_payload["search"]["rows"]],
                ["account-personal"],
            )
            self.assertEqual(
                account_payload["search"]["rows"][0]["products"],
                ["Codex CLI"],
            )
            self.assertEqual(
                account_payload["facets"]["accounts"],
                ["account-personal"],
            )

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                filtered_by_account = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "usage",
                        "--days",
                        "0",
                        "--group",
                        "account",
                        "--account",
                        "account-personal",
                    ]
                )
            self.assertEqual(filtered_by_account, 0)
            self.assertIn("account-personal", buffer.getvalue())
            self.assertIn("Codex CLI", buffer.getvalue())

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                missing_account = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "usage",
                        "--days",
                        "0",
                        "--account",
                        "nobody",
                    ]
                )
            self.assertEqual(missing_account, 0)
            self.assertIn("没有符合条件的用量记录", buffer.getvalue())

    def test_usage_command_without_index_is_empty(self) -> None:
        """用量索引不存在时给出提示而不是报错。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = main(
                    [
                        "--state-dir",
                        str(Path(temporary_directory) / "state"),
                        "usage",
                    ]
                )

        self.assertEqual(code, 0)
        self.assertIn("没有可检索的用量索引", buffer.getvalue())


    def test_parser_accepts_disk_and_session_cleanup_options(self) -> None:
        """disk 与 sessions 的归档/清理参数应被解析。"""

        parser = build_parser()
        disk_args = parser.parse_args(
            ["disk", "--days", "14", "--disk-warn-gb", "2.5", "--json"]
        )
        self.assertEqual(disk_args.command, "disk")
        self.assertEqual(disk_args.days, 14)
        self.assertEqual(disk_args.disk_warn_gb, 2.5)
        self.assertTrue(disk_args.json)
        self.assertEqual(
            disk_args.disk_total_warn_gb,
            DEFAULT_TOTAL_WARN_GIB,
        )

        session_args = parser.parse_args(
            [
                "sessions",
                "--archive",
                "--older-than",
                "7",
                "--min-size-mb",
                "1",
                "--yes",
            ]
        )
        self.assertTrue(session_args.archive)
        self.assertFalse(session_args.clean)
        self.assertEqual(session_args.older_than, 7)
        self.assertEqual(session_args.min_size_mb, 1.0)
        self.assertTrue(session_args.yes)
        self.assertEqual(
            session_args.session_turn_warn,
            DEFAULT_SESSION_TURN_WARN,
        )

        restore_args = parser.parse_args(
            ["sessions", "--restore", "/tmp/a.tar.gz", "--to", "/tmp/out"]
        )
        self.assertEqual(restore_args.restore, Path("/tmp/a.tar.gz"))
        self.assertEqual(restore_args.to, Path("/tmp/out"))

        daemon_args = parser.parse_args(
            [
                "daemon",
                "--session-turn-warn",
                "50",
                "--session-context-warn-tokens",
                "100000",
                "--disk-warn-gb",
                "1",
                "--disk-total-warn-gb",
                "3",
            ]
        )
        self.assertEqual(daemon_args.session_turn_warn, 50)
        self.assertEqual(daemon_args.session_context_warn_tokens, 100_000)
        self.assertEqual(daemon_args.disk_warn_gb, 1.0)
        self.assertEqual(daemon_args.disk_total_warn_gb, 3.0)

    def test_disk_command_reports_usage_and_preview(self) -> None:
        """disk 命令应输出目录占用、提醒和清理预览。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            _write_stale_session(home, 100)

            buffer = io.StringIO()
            with (
                mock.patch.dict(os.environ, _missing_provider_env(root)),
                contextlib.redirect_stdout(buffer),
            ):
                code = main(
                    [
                        "--state-dir",
                        str(root / "state"),
                        "--codex-home",
                        str(home),
                        "disk",
                        "--days",
                        "30",
                        "--disk-warn-gb",
                        "0.000001",
                        "--disk-total-warn-gb",
                        "0.000001",
                    ]
                )

        output = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Codex (codex)", output)
        self.assertIn("超过单目录", output)
        self.assertIn("预览", output)
        self.assertIn("1 个超过 30 天", output)

    def test_sessions_archive_and_restore_round_trip(self) -> None:
        """sessions --archive 先预览，带 --yes 才归档，并支持恢复。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            session = _write_stale_session(home, 100)
            state_dir = root / "state"

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                previewed = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "--codex-home",
                        str(home),
                        "sessions",
                        "--archive",
                        "--older-than",
                        "30",
                    ]
                )
            self.assertEqual(previewed, 0)
            self.assertIn("加上 --yes", buffer.getvalue())
            self.assertTrue(session.exists())

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                archived = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "--codex-home",
                        str(home),
                        "sessions",
                        "--archive",
                        "--older-than",
                        "30",
                        "--yes",
                        "--json",
                    ]
                )
            result = json.loads(buffer.getvalue())
            self.assertEqual(archived, 0)
            self.assertEqual(result["count"], 1)
            self.assertFalse(session.exists())
            archive = Path(result["archive"])
            self.assertTrue(archive.is_file())

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                restored = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "--codex-home",
                        str(home),
                        "sessions",
                        "--restore",
                        str(archive),
                        "--to",
                        str(root / "restore"),
                        "--json",
                    ]
                )
            payload = json.loads(buffer.getvalue())
            self.assertEqual(restored, 0)
            self.assertEqual(payload["restored"], 1)
            self.assertEqual(
                len(list((root / "restore").glob("**/rollout-*.jsonl"))),
                1,
            )

    def test_sessions_clean_requires_yes(self) -> None:
        """没有 --yes 时 sessions --clean 只预览，不删除文件。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            session = _write_stale_session(home, 100)

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = main(
                    [
                        "--state-dir",
                        str(root / "state"),
                        "--codex-home",
                        str(home),
                        "sessions",
                        "--clean",
                        "--older-than",
                        "30",
                        "--json",
                    ]
                )
            still_there = session.exists()

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["count"], 1)
        self.assertTrue(still_there)


    def test_sessions_archive_single_session_by_id(self) -> None:
        """--session 支持按会话 ID 或路径单独归档，缺省只预览。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            target = _write_stale_session(home, 100)
            other = _write_stale_session(
                home,
                100,
                session_id="77777777-7777-4777-8777-777777777777",
                day="02",
            )
            session_id = target.name.split("-", 6)[-1].removesuffix(".jsonl")

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                previewed = main(
                    [
                        "--state-dir",
                        str(root / "state"),
                        "--codex-home",
                        str(home),
                        "sessions",
                        "--archive",
                        "--session",
                        session_id,
                        "--json",
                    ]
                )
            preview = json.loads(buffer.getvalue())

            self.assertEqual(previewed, 0)
            self.assertEqual(preview["count"], 1)
            self.assertEqual(preview["files"][0]["path"], str(target))
            self.assertTrue(target.exists())
            self.assertTrue(other.exists())

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                archived = main(
                    [
                        "--state-dir",
                        str(root / "state"),
                        "--codex-home",
                        str(home),
                        "sessions",
                        "--archive",
                        "--session",
                        str(target),
                        "--yes",
                        "--json",
                    ]
                )
            result = json.loads(buffer.getvalue())
            target_exists = target.exists()
            other_exists = other.exists()

        self.assertEqual(archived, 0)
        self.assertEqual(result["count"], 1)
        self.assertFalse(target_exists)
        self.assertTrue(other_exists)

    def test_sessions_archive_unknown_session_is_rejected(self) -> None:
        """未知会话 ID 应给出提示并以退出码 2 结束。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            _write_stale_session(home, 100)

            code = main(
                [
                    "--state-dir",
                    str(root / "state"),
                    "--codex-home",
                    str(home),
                    "sessions",
                    "--archive",
                    "--session",
                    "not-a-real-session",
                ]
            )

        self.assertEqual(code, 2)


    def test_service_plist_prints_systemd_unit(self) -> None:
        """service plist 在 Linux 打印 systemd 单元。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = main(
                    ["--state-dir", str(root / "state"), "service", "plist"]
                )

        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("[Unit]", output)
        self.assertIn("ExecStart=", output)
        # systemd 单元里每个参数单独加引号
        self.assertIn('"service" "run"', output)

    def test_service_plist_prints_launchd_plist_on_macos(self) -> None:
        """service plist 在 macOS 打印 LaunchAgent plist。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            buffer = io.StringIO()
            with (
                mock.patch("a_token_monitor.service.sys.platform", "darwin"),
                contextlib.redirect_stdout(buffer),
            ):
                code = main(
                    ["--state-dir", str(root / "state"), "service", "plist"]
                )

        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("<key>Label</key>", output)
        self.assertIn("com.a-token-monitor.daemon", output)
        self.assertIn("<key>RunAtLoad</key>", output)
        self.assertIn("<string>service</string>", output)
        self.assertIn("<string>run</string>", output)
        self.assertIn(str(root / "state" / "launchd.log"), output)

    @requires_proc
    def test_traffic_without_netlink_degrades_instead_of_crashing(self) -> None:
        """没有 AF_NETLINK 时 traffic 命令给平台说明而不是抛异常。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            buffer = io.StringIO()
            saved = socket.AF_NETLINK
            del socket.AF_NETLINK
            try:
                with (
                    mock.patch("a_token_monitor.cli.time.sleep"),
                    mock.patch("a_token_monitor.traffic.time.sleep"),
                    contextlib.redirect_stdout(buffer),
                ):
                    code = main(
                        [
                            "--state-dir",
                            str(root / "state"),
                            "traffic",
                            "--sample-seconds",
                            "1",
                        ]
                    )
            finally:
                socket.AF_NETLINK = saved

        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("netlink", output)

    def test_session_usage_note_reads_unified_view(self) -> None:
        """会话行尾的轮数说明统一从视图 usage 字段读取。"""

        view = {
            "usage": {
                "turns": 120,
                "context_tokens": 300_000,
                "total_tokens": 1_000_000,
                "model": "claude-sonnet-4-5",
                "reminder": {"message": "会话建议切换"},
            }
        }

        note = _session_usage_note(view)
        self.assertIn("120 轮", note)
        self.assertIn("300,000 token", note)
        self.assertIn("1,000,000 token", note)
        self.assertIn("claude-sonnet-4-5", note)
        self.assertIn("建议开新会话", note)

        quiet = {"usage": {**view["usage"], "reminder": None}}
        self.assertNotIn("建议开新会话", _session_usage_note(quiet))
        self.assertEqual(_session_usage_note({}), "")
        self.assertEqual(_session_usage_note(None), "")

    def test_service_install_config_without_codex(self) -> None:
        """没有 Codex CLI/CODEX_HOME 也能生成后台服务配置。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = build_parser().parse_args(
                ["--state-dir", str(root / "state"), "service", "install"]
            )
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(root / "missing")}):
                config = _service_config(args)
            config.save()
            loaded = ServiceConfig.load(config.config_path)

        self.assertEqual(config.codex_homes, ())
        # 没有 Codex 账号时不解析可执行文件，保留原始命令名
        self.assertEqual(config.codex_path, "codex")
        self.assertEqual(loaded.codex_homes, ())

    def test_quota_without_any_provider_prints_hint(self) -> None:
        """没有任何账号时 quota 给出可读提示而不是空输出。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            buffer = io.StringIO()
            with (
                mock.patch.dict(os.environ, _missing_provider_env(root)),
                contextlib.redirect_stdout(buffer),
            ):
                code = main(["--state-dir", str(root / "state"), "quota"])

        self.assertEqual(code, 2)
        self.assertIn("未发现任何账号", buffer.getvalue())

    def test_daemon_monitor_prefers_web_scan_dirs(self) -> None:
        """daemon 组装监控器时 Web 扫描目录覆盖优先于命令行参数。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_dir = root / "state"
            state_dir.mkdir()
            cli_grok = root / "cli-grok"
            web_grok = root / "web-grok"
            ScanDirsConfig(overrides={"grok": (web_grok,)}).save(
                state_dir / "scan-dirs.json"
            )
            args = build_parser().parse_args(
                [
                    "--state-dir",
                    str(state_dir),
                    "--grok-home",
                    str(cli_grok),
                    "daemon",
                ]
            )
            with (
                mock.patch.dict(os.environ, _missing_provider_env(root)),
                mock.patch(
                    "a_token_monitor.cli.MultiAccountMonitor"
                ) as monitor_class,
            ):
                monitor = _monitor(args)
            kwargs = monitor_class.call_args.kwargs

        self.assertIs(monitor, monitor_class.return_value)
        self.assertEqual(kwargs["grok_homes"], (web_grok,))
        self.assertEqual(kwargs["accounts"], ())
        # 没有覆盖也没有命令行参数的 provider 传 None，保留自动探测。
        self.assertIsNone(kwargs["kimi_homes"])
        self.assertIsNone(kwargs["dsh_homes"])
        self.assertIsNone(kwargs["commandcode_homes"])
        self.assertIsNone(kwargs["claude_homes"])
        self.assertIsInstance(
            kwargs["scan_dirs_controller"],
            ScanDirsController,
        )

    def test_daemon_monitor_uses_cli_homes_without_web_override(self) -> None:
        """没有 Web 覆盖时命令行参数优先于自动探测。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_dir = root / "state"
            state_dir.mkdir()
            cli_grok = root / "cli-grok"
            cli_grok.mkdir()
            auto_grok = root / "auto-grok"
            auto_grok.mkdir()
            env = _missing_provider_env(root)
            env["GROK_HOME"] = str(auto_grok)
            args = build_parser().parse_args(
                [
                    "--state-dir",
                    str(state_dir),
                    "--grok-home",
                    str(cli_grok),
                    "daemon",
                ]
            )
            with (
                mock.patch.dict(os.environ, env),
                mock.patch(
                    "a_token_monitor.cli.MultiAccountMonitor"
                ) as monitor_class,
            ):
                _monitor(args)
            kwargs = monitor_class.call_args.kwargs

        self.assertEqual(kwargs["grok_homes"], (cli_grok,))
        self.assertIsInstance(
            kwargs["scan_dirs_controller"],
            ScanDirsController,
        )

    def test_one_shot_command_tolerates_corrupt_scan_dirs_config(self) -> None:
        """scan-dirs.json 损坏时一次性命令降级为命令行/自动探测而不是报错。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_dir = root / "state"
            state_dir.mkdir()
            (state_dir / "scan-dirs.json").write_text(
                "{ 这不是合法 JSON",
                encoding="utf-8",
            )
            buffer = io.StringIO()
            with (
                mock.patch.dict(os.environ, _missing_provider_env(root)),
                mock.patch("a_token_monitor.cli._accounts", return_value=()),
                contextlib.redirect_stdout(buffer),
            ):
                code = main(["--state-dir", str(state_dir), "quota"])

        self.assertEqual(code, 2)
        self.assertIn("未发现任何账号", buffer.getvalue())


def _write_stale_session(
    home: Path,
    days_old: float,
    session_id: str = "99999999-9999-4999-8999-999999999999",
    day: str = "01",
) -> Path:
    """写入一个超过保留期的 Codex session 文件。"""

    path = (
        home
        / "sessions"
        / "2026"
        / "06"
        / day
        / f"rollout-2026-06-{day}T01-00-00-{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 4096)
    stamp = time.time() - days_old * 86_400
    os.utime(path, (stamp, stamp))
    return path


def _write_usage_index(root: Path) -> None:
    """写入一个带会话目录的合成用量 JSONL。"""

    session = (
        root
        / ".codex"
        / "sessions"
        / "2026"
        / "08"
        / "27"
        / "rollout-2026-08-27T01-00-00-44444444-4444-4444-8444-444444444444.jsonl"
    )
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                {
                    "timestamp": "2026-08-27T01:00:00Z",
                    "type": "session_meta",
                    "payload": {"cwd": "/home/dev/delta"},
                },
                {
                    "timestamp": "2026-08-27T02:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "thread_settings": {"model": "gpt-5.6-luna"},
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 4_000,
                                "total_tokens": 4_000,
                            }
                        },
                    },
                },
                {
                    "timestamp": "2026-08-27T03:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "thread_settings": {"model": "gpt-5.6-luna"},
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 9_000,
                                "total_tokens": 9_000,
                            }
                        },
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
