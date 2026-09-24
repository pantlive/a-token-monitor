"""macOS launchd 用户服务（LaunchAgent）配置和 plist 生成测试。"""

from __future__ import annotations

import contextlib
import io
import os
import plistlib
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from _platform_support import posix_only, requires_chmod
from token_monitor.service import (
    LAUNCHD_LABEL,
    LaunchdServiceManager,
    ServiceConfig,
    ServiceError,
    UserServiceManager,
    create_service_manager,
    launchd_available,
)


class LaunchdServiceTests(unittest.TestCase):
    """验证 LaunchAgent plist 的生成、安装、查询和卸载。"""

    def test_render_plist_contains_stable_bootstrap_arguments(self) -> None:
        """plist 只应携带解释器、状态目录和 launchd 运行参数。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = LaunchdServiceManager(
                state_dir=root / "state with space",
                unit_dir=root / "agents",
                python_executable=root / "Python With Space" / "python",
            )

            payload = plistlib.loads(manager.render_plist().encode("utf-8"))

            self.assertEqual(payload["Label"], LAUNCHD_LABEL)
            arguments = payload["ProgramArguments"]
            self.assertEqual(arguments[-2:], ["service", "run"])
            self.assertEqual(arguments[1:3], ["-m", "token_monitor"])
            self.assertEqual(arguments[3], "--state-dir")
            self.assertEqual(arguments[0], str(manager.python_executable))
            self.assertIs(payload["RunAtLoad"], True)
            self.assertEqual(payload["KeepAlive"], {"SuccessfulExit": False})
            self.assertEqual(
                payload["WorkingDirectory"],
                str(manager.state_dir),
            )
            self.assertEqual(payload["StandardOutPath"], str(manager.log_path))
            self.assertEqual(payload["StandardErrorPath"], str(manager.log_path))
            self.assertEqual(
                payload["EnvironmentVariables"],
                {"PYTHONUNBUFFERED": "1"},
            )
            self.assertEqual(payload["ProcessType"], "Background")

    @posix_only
    def test_defaults_follow_launch_agent_conventions(self) -> None:
        """默认 plist 目录、日志路径和 domain 应符合 launchd 约定。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = LaunchdServiceManager(state_dir=root / "state")

            expected_unit_dir = (
                Path.home() / "Library" / "LaunchAgents"
            ).resolve()

            self.assertEqual(manager.unit_dir, expected_unit_dir)
            self.assertEqual(
                manager.plist_path,
                expected_unit_dir / f"{LAUNCHD_LABEL}.plist",
            )
            self.assertEqual(manager.label, LAUNCHD_LABEL)
            self.assertEqual(manager.domain, f"gui/{os.getuid()}")
            self.assertEqual(manager.log_path, manager.state_dir / "launchd.log")
            self.assertEqual(manager.config_path, manager.state_dir / "service.json")

    @requires_chmod
    def test_install_writes_files_and_bootstraps_service(self) -> None:
        """安装应原子写入配置和 plist，并通知 launchd 引导服务。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._config(root)
            manager = LaunchdServiceManager(
                state_dir=config.state_dir,
                unit_dir=root / "agents",
                python_executable=root / "python",
            )

            with patch("token_monitor.service.subprocess.run") as run:
                run.return_value = Mock(returncode=0)
                manager.install(config, start=True)

            self.assertTrue(config.config_path.is_file())
            self.assertTrue(manager.plist_path.is_file())
            mode = stat.S_IMODE(manager.plist_path.stat().st_mode)
            self.assertEqual(mode, 0o600)
            stored = plistlib.loads(manager.plist_path.read_bytes())
            self.assertEqual(stored["Label"], LAUNCHD_LABEL)

            commands = [
                " ".join(call.args[0]) for call in run.call_args_list
            ]
            self.assertTrue(
                all(command.startswith("launchctl ") for command in commands),
                commands,
            )
            self.assertTrue(
                any("bootstrap" in command for command in commands),
                commands,
            )
            bootout_index = next(
                index
                for index, command in enumerate(commands)
                if "bootout" in command
            )
            bootstrap_index = next(
                index
                for index, command in enumerate(commands)
                if "bootstrap" in command
            )
            self.assertLess(bootout_index, bootstrap_index)
            self.assertIn(str(manager.plist_path), commands[bootstrap_index])
            self.assertIn(manager.domain, commands[bootstrap_index])
            self.assertTrue(
                any("kickstart" in command for command in commands),
                commands,
            )

    def test_install_without_start_skips_kickstart(self) -> None:
        """start=False 时只注册 plist，不应立即启动进程。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._config(root)
            manager = LaunchdServiceManager(
                state_dir=config.state_dir,
                unit_dir=root / "agents",
                python_executable=root / "python",
            )

            with patch("token_monitor.service.subprocess.run") as run:
                run.return_value = Mock(returncode=0)
                manager.install(config, start=False)

            commands = [" ".join(call.args[0]) for call in run.call_args_list]
            self.assertTrue(any("bootstrap" in command for command in commands))
            self.assertFalse(any("kickstart" in command for command in commands))

    def test_install_rejects_mismatched_state_dir(self) -> None:
        """配置与管理器的 state_dir 不一致时应拒绝安装。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._config(root)
            manager = LaunchdServiceManager(
                state_dir=root / "other-state",
                unit_dir=root / "agents",
            )

            with patch("token_monitor.service.subprocess.run") as run:
                run.return_value = Mock(returncode=0)
                with self.assertRaises(ServiceError):
                    manager.install(config)

    def test_start_uses_kickstart(self) -> None:
        """启动应通过 kickstart 强制重建进程。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = LaunchdServiceManager(
                state_dir=root / "state",
                unit_dir=root / "agents",
            )

            with patch("token_monitor.service.subprocess.run") as run:
                run.return_value = Mock(returncode=0)
                manager.start()

            self.assertEqual(
                run.call_args_list[0].args[0],
                [
                    "launchctl",
                    "kickstart",
                    "-k",
                    f"{manager.domain}/{LAUNCHD_LABEL}",
                ],
            )

    def test_stop_falls_back_to_bootout(self) -> None:
        """kill 失败（服务未运行）时应退回 bootout 且不抛异常。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = LaunchdServiceManager(
                state_dir=root / "state",
                unit_dir=root / "agents",
            )

            with patch("token_monitor.service.subprocess.run") as run:
                run.return_value = Mock(returncode=3)
                manager.stop()

            commands = [" ".join(call.args[0]) for call in run.call_args_list]
            self.assertTrue(any("kill SIGTERM" in command for command in commands))
            self.assertTrue(any("bootout" in command for command in commands))
            self.assertEqual(run.call_args_list[1].kwargs, {"check": False})

    def test_status_returns_exit_code_without_raising(self) -> None:
        """未安装服务的 print 返回非 0 时应原样返回退出码。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = LaunchdServiceManager(
                state_dir=root / "state",
                unit_dir=root / "agents",
            )

            with patch("token_monitor.service.subprocess.run") as run:
                run.return_value = Mock(returncode=113)

                status = manager.status()

            self.assertEqual(status, 113)
            self.assertEqual(
                run.call_args_list[0].args[0],
                ["launchctl", "print", manager.service_target],
            )

    def test_uninstall_boots_out_and_removes_plist(self) -> None:
        """卸载应注销服务并删除 plist，重复卸载也不报错。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = LaunchdServiceManager(
                state_dir=root / "state",
                unit_dir=root / "agents",
                python_executable=root / "python",
            )
            manager.unit_dir.mkdir(parents=True, exist_ok=True)
            manager.plist_path.write_text(
                manager.render_plist(),
                encoding="utf-8",
            )
            manager.config_path.parent.mkdir(parents=True, exist_ok=True)
            manager.config_path.write_text("{}", encoding="utf-8")

            with patch("token_monitor.service.subprocess.run") as run:
                run.return_value = Mock(returncode=0)
                manager.uninstall()
                manager.uninstall()

            self.assertFalse(manager.plist_path.exists())
            # 与 systemd 版一致：service.json 也一并移除
            self.assertFalse(manager.config_path.exists())
            commands = [" ".join(call.args[0]) for call in run.call_args_list]
            self.assertTrue(all("bootout" in command for command in commands))
            self.assertTrue(all("launchctl" in command for command in commands))

    def test_start_reports_missing_launchctl(self) -> None:
        """找不到 launchctl 时必须以 ServiceError 报告并带上命令名。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = LaunchdServiceManager(
                state_dir=root / "state",
                unit_dir=root / "agents",
            )

            with patch(
                "token_monitor.service.subprocess.run",
                side_effect=FileNotFoundError("launchctl"),
            ):
                with self.assertRaisesRegex(ServiceError, "launchctl"):
                    manager.start()

    def test_logs_prints_last_lines(self) -> None:
        """logs 应打印最后若干行日志并返回 0。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = LaunchdServiceManager(
                state_dir=root / "state",
                unit_dir=root / "agents",
            )
            manager.state_dir.mkdir(parents=True, exist_ok=True)
            manager.log_path.write_text("旧日志\n新日志\n", encoding="utf-8")
            buffer = io.StringIO()

            with contextlib.redirect_stdout(buffer):
                status = manager.logs(lines=1)

            self.assertEqual(status, 0)
            self.assertEqual(buffer.getvalue(), "新日志\n")

    def test_logs_without_file_prints_hint(self) -> None:
        """日志文件不存在时应给出中文提示而不是报错。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = LaunchdServiceManager(
                state_dir=root / "state",
                unit_dir=root / "agents",
            )
            buffer = io.StringIO()

            with contextlib.redirect_stdout(buffer):
                status = manager.logs(lines=10)

            self.assertEqual(status, 0)
            self.assertIn("日志", buffer.getvalue())

    def test_logs_follow_passes_through_tail_exit_code(self) -> None:
        """follow 模式应调用 tail -f 并透传它的退出码。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = LaunchdServiceManager(
                state_dir=root / "state",
                unit_dir=root / "agents",
            )

            with patch("token_monitor.service.subprocess.run") as run:
                run.return_value = Mock(returncode=7)

                status = manager.logs(lines=5, follow=True)

            self.assertEqual(status, 7)
            self.assertEqual(
                run.call_args_list[0].args[0],
                ["tail", "-n", "5", "-f", str(manager.log_path)],
            )

    def test_logs_rejects_non_positive_lines(self) -> None:
        """日志行数必须大于 0。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = LaunchdServiceManager(
                state_dir=root / "state",
                unit_dir=root / "agents",
            )

            with self.assertRaises(ValueError):
                manager.logs(lines=0)

    def test_create_service_manager_selects_platform(self) -> None:
        """工厂函数应按 platform 参数选择实现，不依赖真实系统。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)

            darwin_manager = create_service_manager(
                root / "state",
                root / "agents",
                None,
                platform="darwin",
            )
            linux_manager = create_service_manager(
                root / "state",
                root / "units",
                None,
                platform="linux",
            )

        self.assertIsInstance(darwin_manager, LaunchdServiceManager)
        self.assertEqual(darwin_manager.unit_dir, (root / "agents").resolve())
        self.assertIs(type(linux_manager), UserServiceManager)

    def test_launchd_available_requires_macos_and_launchctl(self) -> None:
        """只有 macOS 且 PATH 中存在 launchctl 时才认为可用。"""

        with patch("token_monitor.service.sys.platform", "darwin"):
            with patch(
                "token_monitor.service.shutil.which",
                return_value="/bin/launchctl",
            ):
                self.assertTrue(launchd_available())
            with patch("token_monitor.service.shutil.which", return_value=None):
                self.assertFalse(launchd_available())
        with patch("token_monitor.service.sys.platform", "linux"):
            with patch(
                "token_monitor.service.shutil.which",
                return_value="/bin/launchctl",
            ):
                self.assertFalse(launchd_available())

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
