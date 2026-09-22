"""systemd 用户服务配置和单元生成测试。"""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from token_monitor.service import (
    SERVICE_NAME,
    ServiceConfig,
    ServiceError,
    UserServiceManager,
    _systemd_quote,
)


class ServiceTests(unittest.TestCase):
    """验证后台服务不会丢失账号、路径和安全配置。"""

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

    @patch("token_monitor.service._run_command", return_value=0)
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

    def test_systemd_quote_blocks_expansion(self) -> None:
        """路径中的空格、美元符和百分号不得被 systemd 二次解释。"""

        quoted = _systemd_quote('/tmp/a b/$HOME/100%/"x"')

        self.assertEqual(
            quoted,
            '"/tmp/a b/$$HOME/100%%/\\"x\\""',
        )

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
