"""命令行多账号参数测试。"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_reset_monitor.accounts import build_account_specs
from codex_reset_monitor.cli import build_parser, main
from codex_reset_monitor.quota import QuotaSnapshot, QuotaWindow


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
                "~/.codex-reset-monitor",
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

    def test_quota_command_prints_kimi_windows(self) -> None:
        """quota 子命令应展示 Kimi /usages 返回的窗口。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            kimi_home = Path(temporary_directory) / ".kimi-code"
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
                mock.patch("codex_reset_monitor.cli._accounts", return_value=()),
                mock.patch(
                    "codex_reset_monitor.cli.resolve_grok_homes",
                    return_value=(),
                ),
                mock.patch(
                    "codex_reset_monitor.cli.read_kimi_quota",
                    return_value=snapshot,
                ),
            ):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    exit_code = main(["--kimi-home", str(kimi_home), "quota"])

            self.assertEqual(exit_code, 0)
            output = buffer.getvalue()
            self.assertIn("kimi/limit_5h", output)
            self.assertIn("25%", output)
            self.assertIn("已登录", output)
            self.assertNotIn("SECRET-TOKEN", output)

    def test_quota_command_notes_kimi_quota_failure(self) -> None:
        """Kimi 配额读取失败时应提示但不影响退出码。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            kimi_home = Path(temporary_directory) / ".kimi-code"
            credentials = kimi_home / "credentials"
            credentials.mkdir(parents=True)
            (credentials / "kimi-code.json").write_text(
                json.dumps({"access_token": "SECRET-TOKEN"}),
                encoding="utf-8",
            )
            with (
                mock.patch("codex_reset_monitor.cli._accounts", return_value=()),
                mock.patch(
                    "codex_reset_monitor.cli.resolve_grok_homes",
                    return_value=(),
                ),
                mock.patch(
                    "codex_reset_monitor.cli.read_kimi_quota",
                    return_value=None,
                ),
            ):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    exit_code = main(["--kimi-home", str(kimi_home), "quota"])

            self.assertEqual(exit_code, 0)
            self.assertIn("配额暂不可读", buffer.getvalue())
            self.assertNotIn("SECRET-TOKEN", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
