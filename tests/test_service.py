"""systemd 用户服务配置和单元生成测试。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from _platform_support import requires_chmod
from a_token_monitor.retention import (
    DEFAULT_SESSION_RETENTION_DAYS,
    DEFAULT_USAGE_RETENTION_DAYS,
)
from a_token_monitor.scan_dirs import ScanDirsConfig, ScanDirsError
from a_token_monitor.service import (
    LEGACY_SERVICE_NAMES,
    SERVICE_NAME,
    ServiceConfig,
    ServiceError,
    UserServiceManager,
    _systemd_quote,
)


class ServiceTests(unittest.TestCase):
    """验证后台服务不会丢失账号、路径和安全配置。"""

    @requires_chmod
    def test_config_round_trip_and_permissions(self) -> None:
        """服务配置应完整往返并限制为当前用户可读写。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._config(root)

            config.save()
            loaded = ServiceConfig.load(config.config_path)

            self.assertEqual(loaded, config)
            mode = stat.S_IMODE(config.config_path.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_config_round_trip_with_kimi_homes(self) -> None:
        """Kimi 数据目录应随服务配置完整往返。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = ServiceConfig(
                state_dir=root / "state",
                codex_homes=(root / ".codex",),
                session_root=None,
                verbose=False,
                codex_path=str(root / "bin" / "codex"),
                scan_interval=2.0,
                reconcile_interval=30.0,
                quota_interval=300.0,
                dashboard=False,
                dashboard_host="127.0.0.1",
                dashboard_port=8765,
                grok_homes=(),
                kimi_homes=(root / ".kimi-code",),
            )

            config.save()
            loaded = ServiceConfig.load(config.config_path)

            self.assertEqual(loaded, config)
            self.assertEqual(loaded.kimi_homes, (root / ".kimi-code",))

    def test_config_round_trip_with_commandcode_homes(self) -> None:
        """Command Code 数据目录应随服务配置完整往返。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = ServiceConfig(
                state_dir=root / "state",
                codex_homes=(root / ".codex",),
                session_root=None,
                verbose=False,
                codex_path=str(root / "bin" / "codex"),
                scan_interval=2.0,
                reconcile_interval=30.0,
                quota_interval=300.0,
                dashboard=False,
                dashboard_host="127.0.0.1",
                dashboard_port=8765,
                grok_homes=(),
                commandcode_homes=(root / ".commandcode",),
            )

            config.save()
            loaded = ServiceConfig.load(config.config_path)

            self.assertEqual(loaded, config)
            self.assertEqual(
                loaded.commandcode_homes,
                (root / ".commandcode",),
            )

    def test_legacy_config_without_commandcode_homes_loads(self) -> None:
        """缺少 commandcode_homes 键的旧配置应加载为空列表。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._config(root)
            config.save()
            payload = json.loads(config.config_path.read_text(encoding="utf-8"))
            payload.pop("commandcode_homes", None)
            config.config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            loaded = ServiceConfig.load(config.config_path)

            self.assertEqual(loaded.commandcode_homes, ())

    def test_legacy_config_without_kimi_homes_loads(self) -> None:
        """缺少 kimi_homes 键的旧配置应加载为空列表。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._config(root)
            config.save()
            payload = json.loads(config.config_path.read_text(encoding="utf-8"))
            payload.pop("kimi_homes", None)
            config.config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            loaded = ServiceConfig.load(config.config_path)

            self.assertEqual(loaded.kimi_homes, ())

    def test_config_round_trip_with_budget(self) -> None:
        """月度预算应随服务配置完整往返。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = ServiceConfig(
                state_dir=root / "state",
                codex_homes=(root / ".codex",),
                session_root=None,
                verbose=False,
                codex_path=str(root / "bin" / "codex"),
                scan_interval=2.0,
                reconcile_interval=30.0,
                quota_interval=300.0,
                dashboard=True,
                dashboard_host="127.0.0.1",
                dashboard_port=8765,
                grok_homes=(),
                budget_usd=99.5,
            )

            config.save()
            loaded = ServiceConfig.load(config.config_path)

            self.assertEqual(loaded, config)
            self.assertEqual(loaded.budget_usd, 99.5)

    def test_legacy_config_without_budget_loads(self) -> None:
        """缺少 budget_usd 键的旧配置应加载为未设置预算。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._config(root)
            config.save()
            payload = json.loads(config.config_path.read_text(encoding="utf-8"))
            payload.pop("budget_usd", None)
            config.config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            loaded = ServiceConfig.load(config.config_path)

            self.assertIsNone(loaded.budget_usd)

    def test_legacy_config_without_upload_thresholds_loads_defaults(self) -> None:
        """缺少外发阈值键的旧配置应使用默认告警阈值。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._config(root)
            config.save()
            payload = json.loads(config.config_path.read_text(encoding="utf-8"))
            for key in (
                "upload_burst_warn_mb",
                "upload_burst_danger_mb",
                "upload_window_warn_mb",
                "upload_window_danger_mb",
            ):
                payload.pop(key, None)
            config.config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            loaded = ServiceConfig.load(config.config_path)

            self.assertEqual(loaded.upload_burst_warn_mb, 8.0)
            self.assertEqual(loaded.upload_burst_danger_mb, 32.0)

    def test_budget_must_be_positive(self) -> None:
        """预算为零或负数时应拒绝配置。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaises(ValueError):
                ServiceConfig(
                    state_dir=root / "state",
                    codex_homes=(root / ".codex",),
                    session_root=None,
                    verbose=False,
                    codex_path=str(root / "bin" / "codex"),
                    scan_interval=2.0,
                    reconcile_interval=30.0,
                    quota_interval=300.0,
                    dashboard=False,
                    dashboard_host="127.0.0.1",
                    dashboard_port=8765,
                    grok_homes=(),
                    budget_usd=0,
                )

    def test_config_round_trip_with_retention_days(self) -> None:
        """用量与会话历史保留天数应随服务配置完整往返。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = ServiceConfig(
                state_dir=root / "state",
                codex_homes=(root / ".codex",),
                session_root=None,
                verbose=False,
                codex_path=str(root / "bin" / "codex"),
                scan_interval=2.0,
                reconcile_interval=30.0,
                quota_interval=300.0,
                dashboard=False,
                dashboard_host="127.0.0.1",
                dashboard_port=8765,
                grok_homes=(),
                usage_retention_days=45.0,
                session_retention_days=7.5,
            )

            config.save()
            loaded = ServiceConfig.load(config.config_path)

            self.assertEqual(loaded, config)
            self.assertEqual(loaded.usage_retention_days, 45.0)
            self.assertEqual(loaded.session_retention_days, 7.5)

    def test_legacy_config_without_retention_days_loads_defaults(self) -> None:
        """缺少保留天数字段的旧配置应加载为默认保留天数。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._config(root)
            config.save()
            payload = json.loads(config.config_path.read_text(encoding="utf-8"))
            payload.pop("usage_retention_days", None)
            payload.pop("session_retention_days", None)
            config.config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            loaded = ServiceConfig.load(config.config_path)

            self.assertEqual(
                loaded.usage_retention_days,
                DEFAULT_USAGE_RETENTION_DAYS,
            )
            self.assertEqual(
                loaded.session_retention_days,
                DEFAULT_SESSION_RETENTION_DAYS,
            )

    def test_retention_days_must_be_valid(self) -> None:
        """保留天数为 0、负数或布尔值时应拒绝配置。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for field, value in (
                ("usage_retention_days", 0),
                ("usage_retention_days", -3.0),
                ("usage_retention_days", True),
                ("session_retention_days", 0),
                ("session_retention_days", -3.0),
                ("session_retention_days", True),
            ):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        ServiceConfig(
                            state_dir=root / "state",
                            codex_homes=(root / ".codex",),
                            session_root=None,
                            verbose=False,
                            codex_path=str(root / "bin" / "codex"),
                            scan_interval=2.0,
                            reconcile_interval=30.0,
                            quota_interval=300.0,
                            dashboard=False,
                            dashboard_host="127.0.0.1",
                            dashboard_port=8765,
                            grok_homes=(),
                            **{field: value},
                        )

    def test_invalid_schema_is_rejected(self) -> None:
        """配置版本不匹配时必须拒绝猜测性启动。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._config(root)
            config.save()
            payload = config.config_path.read_text(encoding="utf-8")
            config.config_path.write_text(
                payload.replace('"schema_version": 1', '"schema_version": 9'),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ServiceError, "不支持"):
                ServiceConfig.load(config.config_path)

    def test_unit_only_contains_stable_bootstrap_arguments(self) -> None:
        """账号配置应保存在 JSON，而不是直接嵌入单元命令。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            python_executable = root / "Python With Space" / "python"
            manager = UserServiceManager(
                state_dir=root / "state with space",
                unit_dir=root / "units",
                python_executable=python_executable,
            )

            unit = manager.render_unit()

            self.assertIn('"service" "run"', unit)
            self.assertIn("Restart=on-failure", unit)
            self.assertIn("KillMode=control-group", unit)
            self.assertIn("UMask=0077", unit)
            self.assertNotIn(".codex-work", unit)

    @patch("a_token_monitor.service._run_command", return_value=0)
    def test_install_writes_files_and_enables_unit(
        self,
        run_command: Mock,
    ) -> None:
        """安装应原子写入配置和单元，并通知 systemd 启用。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            python_executable = root / "python"
            python_executable.touch()
            config = self._config(root)
            manager = UserServiceManager(
                state_dir=config.state_dir,
                unit_dir=root / "units",
                python_executable=python_executable,
            )

            manager.install(config, start=True)

            self.assertTrue(config.config_path.is_file())
            self.assertTrue((root / "units" / SERVICE_NAME).is_file())
            commands = [call.args[0] for call in run_command.call_args_list]
            self.assertEqual(
                commands,
                [
                    ["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", SERVICE_NAME],
                    ["systemctl", "--user", "restart", SERVICE_NAME],
                ],
            )

    @patch("a_token_monitor.service._run_command", return_value=0)
    def test_status_and_logs_fall_back_to_legacy_unit(
        self,
        run_command: Mock,
    ) -> None:
        """改名前的旧单元仍应能被查询，避免升级后看不到后台服务。"""

        for legacy_name in LEGACY_SERVICE_NAMES:
            with self.subTest(legacy_name=legacy_name):
                run_command.reset_mock()
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    unit_dir = root / "units"
                    unit_dir.mkdir()
                    (unit_dir / legacy_name).write_text("", encoding="utf-8")
                    manager = UserServiceManager(
                        state_dir=root / "state",
                        unit_dir=unit_dir,
                    )

                    self.assertEqual(manager.active_service_name, legacy_name)
                    manager.status()
                    manager.logs(lines=10, follow=False)

                    status_command = run_command.call_args_list[0].args[0]
                    self.assertEqual(
                        status_command,
                        ["systemctl", "--user", "status", legacy_name, "--no-pager"],
                    )
                    logs_command = run_command.call_args_list[1].args[0]
                    self.assertIn(legacy_name, logs_command)

    @patch("a_token_monitor.service._run_command", return_value=0)
    def test_new_unit_takes_precedence_over_legacy(
        self,
        run_command: Mock,
    ) -> None:
        """新旧单元同时存在时，只操作新单元。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            unit_dir = root / "units"
            unit_dir.mkdir()
            (unit_dir / SERVICE_NAME).write_text("", encoding="utf-8")
            for legacy_name in LEGACY_SERVICE_NAMES:
                (unit_dir / legacy_name).write_text("", encoding="utf-8")
            manager = UserServiceManager(
                state_dir=root / "state",
                unit_dir=unit_dir,
            )

            self.assertEqual(manager.active_service_name, SERVICE_NAME)
            manager.status()

            self.assertEqual(
                run_command.call_args_list[0].args[0],
                ["systemctl", "--user", "status", SERVICE_NAME, "--no-pager"],
            )

    @patch("a_token_monitor.service._run_command", return_value=0)
    def test_uninstall_removes_legacy_unit(
        self,
        run_command: Mock,
    ) -> None:
        """卸载应识别旧安装并移除旧单元文件。"""

        for legacy_name in LEGACY_SERVICE_NAMES:
            with self.subTest(legacy_name=legacy_name):
                run_command.reset_mock()
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    unit_dir = root / "units"
                    unit_dir.mkdir()
                    legacy_unit = unit_dir / legacy_name
                    legacy_unit.write_text("", encoding="utf-8")
                    manager = UserServiceManager(
                        state_dir=root / "state",
                        unit_dir=unit_dir,
                    )

                    manager.uninstall()

                    self.assertFalse(legacy_unit.exists())
                    commands = [call.args[0] for call in run_command.call_args_list]
                    self.assertEqual(
                        commands[0],
                        [
                            "systemctl",
                            "--user",
                            "disable",
                            "--now",
                            legacy_name,
                        ],
                    )

    def test_systemd_quote_blocks_expansion(self) -> None:
        """路径中的空格、美元符和百分号不得被 systemd 二次解释。"""

        quoted = _systemd_quote('/tmp/a b/$HOME/100%/"x"')

        self.assertEqual(
            quoted,
            '"/tmp/a b/$$HOME/100%%/\\"x\\""',
        )

    def test_config_without_codex_home_builds_monitor(self) -> None:
        """没有 CODEX_HOME 也允许保存配置并启动零账号 daemon。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = ServiceConfig(
                state_dir=root / "state",
                codex_homes=(),
                session_root=None,
                verbose=False,
                codex_path="codex",
                scan_interval=2.0,
                reconcile_interval=30.0,
                quota_interval=300.0,
                dashboard=True,
                dashboard_host="127.0.0.1",
                dashboard_port=8765,
                grok_homes=(),
                kimi_homes=(),
                dsh_homes=(),
                commandcode_homes=(),
                claude_homes=(),
            )

            config.save()
            loaded = ServiceConfig.load(config.config_path)
            # 中文注释：codex_homes 为空表示「未配置」，build_monitor 会退回
            # 自动探测；把 CODEX_HOME 指向不存在的路径以保持测试隔离。
            with patch.dict(os.environ, {"CODEX_HOME": str(root / "missing")}):
                monitor = loaded.build_monitor()
                try:
                    registries = dict(monitor.registries)
                finally:
                    monitor.close()
                built_accounts = config.build_monitor().accounts

        self.assertEqual(loaded.codex_homes, ())
        self.assertEqual(built_accounts, ())
        self.assertEqual(registries, {})

    def test_build_monitor_prefers_web_scan_dirs(self) -> None:
        """Web 扫描目录覆盖应优先于 service.json 持久化的目录。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            persisted_grok = root / "persisted-grok"
            web_grok = root / "web-grok"
            config = ServiceConfig(
                state_dir=root / "state",
                codex_homes=(root / ".codex",),
                session_root=None,
                verbose=False,
                codex_path="codex",
                scan_interval=2.0,
                reconcile_interval=30.0,
                quota_interval=300.0,
                dashboard=False,
                dashboard_host="127.0.0.1",
                dashboard_port=8765,
                grok_homes=(persisted_grok,),
            )
            config.save()
            ScanDirsConfig(overrides={"grok": (web_grok,)}).save(
                config.state_dir / "scan-dirs.json"
            )

            monitor = config.build_monitor()
            try:
                grok_homes = monitor.grok_homes
            finally:
                monitor.close()

        self.assertEqual(grok_homes, (web_grok,))

    def test_build_monitor_auto_detects_when_persisted_homes_empty(self) -> None:
        """持久化目录为空时应传 None，让监控器保留自动探测而不是禁用 provider。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            grok_home = root / "auto-grok"
            grok_home.mkdir()
            config = ServiceConfig(
                state_dir=root / "state",
                codex_homes=(root / ".codex",),
                session_root=None,
                verbose=False,
                codex_path="codex",
                scan_interval=2.0,
                reconcile_interval=30.0,
                quota_interval=300.0,
                dashboard=False,
                dashboard_host="127.0.0.1",
                dashboard_port=8765,
                grok_homes=(),
            )
            config.save()

            with patch.dict(os.environ, {"GROK_HOME": str(grok_home)}):
                monitor = config.build_monitor()
                try:
                    grok_homes = monitor.grok_homes
                finally:
                    monitor.close()

        self.assertEqual(grok_homes, (grok_home,))

    def test_build_monitor_rejects_corrupt_scan_dirs_config(self) -> None:
        """scan-dirs.json 损坏时 build_monitor 必须报错而不是静默忽略。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._config(root)
            config.save()
            (config.state_dir / "scan-dirs.json").write_text(
                "{ 这不是合法 JSON",
                encoding="utf-8",
            )

            with self.assertRaises(ScanDirsError):
                config.build_monitor()

    @staticmethod
    def _config(root: Path) -> ServiceConfig:
        """生成覆盖多账号和 Dashboard 的最小有效配置。"""

        return ServiceConfig(
            state_dir=root / "state",
            codex_homes=(root / ".codex", root / ".codex-work"),
            session_root=None,
            verbose=True,
            codex_path=str(root / "bin" / "codex"),
            scan_interval=2.0,
            reconcile_interval=30.0,
            quota_interval=300.0,
            dashboard=True,
            dashboard_host="0.0.0.0",
            dashboard_port=8765,
            grok_homes=(),
        )


if __name__ == "__main__":
    unittest.main()
