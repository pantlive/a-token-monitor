"""Windows 计划任务（Task Scheduler）后台服务配置和 XML 生成测试。"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from unittest.mock import Mock, patch

from token_monitor.service import (
    LaunchdServiceManager,
    ServiceConfig,
    ServiceError,
    TaskSchedulerServiceManager,
    UserServiceManager,
    _current_uid,
    create_service_manager,
    windows_service_available,
)

TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _element(root: ElementTree.Element, path: str) -> ElementTree.Element:
    """按去掉命名空间前缀的路径查找计划任务 XML 元素。"""

    current = root
    for part in path.split("/"):
        found = current.find(f"{{{TASK_NAMESPACE}}}{part}")
        if found is None:
            raise AssertionError(f"计划任务 XML 缺少元素: {path}")
        current = found
    return current


def _text(root: ElementTree.Element, path: str) -> str:
    """读取计划任务 XML 元素的文本内容，自闭合元素视为空字符串。"""

    return _element(root, path).text or ""


class TaskSchedulerServiceTests(unittest.TestCase):
    """验证计划任务 XML 的生成、安装、查询、日志和卸载。"""

    def test_render_task_xml_is_valid_and_uses_expected_settings(self) -> None:
        """XML 应可解析，并覆盖登录触发、普通权限和不限时重启策略。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = TaskSchedulerServiceManager(
                state_dir=root / "state with space",
                unit_dir=root / "units",
                python_executable=root / "Python With Space" / "python.exe",
            )

            document = ElementTree.fromstring(manager.render_task_xml())

            self.assertTrue(_text(document, "RegistrationInfo/Description"))
            self.assertEqual(
                _text(document, "RegistrationInfo/URI"),
                "\\TokenMonitor",
            )
            self.assertEqual(
                _text(document, "Triggers/LogonTrigger/Enabled"),
                "true",
            )
            # 缺省不写 UserId 元素：空元素会被部分 Windows 版本的 schtasks 拒绝
            self.assertIsNone(document.find("Principals/Principal/UserId"))
            self.assertEqual(
                _text(document, "Principals/Principal/LogonType"),
                "InteractiveToken",
            )
            self.assertEqual(
                _text(document, "Principals/Principal/RunLevel"),
                "LeastPrivilege",
            )
            self.assertEqual(
                _text(document, "Settings/MultipleInstancesPolicy"),
                "IgnoreNew",
            )
            self.assertEqual(
                _text(document, "Settings/DisallowStartIfOnBatteries"),
                "false",
            )
            self.assertEqual(
                _text(document, "Settings/StopIfGoingOnBatteries"),
                "false",
            )
            self.assertEqual(
                _text(document, "Settings/AllowHardTerminate"),
                "true",
            )
            self.assertEqual(
                _text(document, "Settings/StartWhenAvailable"),
                "true",
            )
            self.assertEqual(
                _text(document, "Settings/ExecutionTimeLimit"),
                "PT0S",
            )
            self.assertIsNotNone(
                document.find(
                    f"{{{TASK_NAMESPACE}}}Settings"
                    f"/{{{TASK_NAMESPACE}}}RestartOnFailure"
                )
            )
            self.assertEqual(
                _text(document, "Settings/RestartOnFailure/Interval"),
                "PT1M",
            )
            self.assertEqual(
                _text(document, "Settings/RestartOnFailure/Count"),
                "3",
            )
            self.assertEqual(
                _text(document, "Actions/Exec/Command"),
                "cmd.exe",
            )
            arguments = _text(document, "Actions/Exec/Arguments")
            self.assertIn("service run", arguments)
            self.assertIn(str(manager.python_executable), arguments)
            self.assertIn(str(manager.state_dir), arguments)
            self.assertIn(str(manager.log_path), arguments)
            self.assertEqual(
                _text(document, "Actions/Exec/WorkingDirectory"),
                str(manager.state_dir),
            )

    def test_render_arguments_nests_quotes_for_cmd(self) -> None:
        """cmd.exe /c 需要整体再包一层引号，内部路径各自加引号。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = TaskSchedulerServiceManager(
                state_dir=root / "state",
                python_executable=root / "python.exe",
            )

            arguments = manager.render_arguments()

            self.assertTrue(arguments.startswith('/c ""'))
            self.assertTrue(arguments.endswith('2>&1"'))
            self.assertIn(f'"{manager.python_executable}"', arguments)
            self.assertIn(f'--state-dir "{manager.state_dir}"', arguments)
            self.assertIn(f'>> "{manager.log_path}"', arguments)

    def test_render_task_xml_escapes_special_characters(self) -> None:
        """路径里的 & 和尖括号必须转义，转义后仍能解析回原值。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = TaskSchedulerServiceManager(
                state_dir=root / "state & <dir>",
                python_executable=root / "py&thon.exe",
            )

            content = manager.render_task_xml()
            document = ElementTree.fromstring(content)

            self.assertIn("&amp;", content)
            self.assertIn("&lt;", content)
            self.assertIn(
                str(manager.python_executable),
                _text(document, "Actions/Exec/Arguments"),
            )
            self.assertEqual(
                _text(document, "Actions/Exec/WorkingDirectory"),
                str(manager.state_dir),
            )

    def test_defaults_follow_task_scheduler_conventions(self) -> None:
        """默认任务名、XML、配置和日志都放在状态目录下。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = TaskSchedulerServiceManager(state_dir=root / "state")

            self.assertEqual(manager.task_name, "TokenMonitor")
            self.assertEqual(
                manager.task_path,
                manager.state_dir / "token-monitor-task.xml",
            )
            self.assertEqual(
                manager.config_path,
                manager.state_dir / "service.json",
            )
            self.assertEqual(manager.log_path, manager.state_dir / "daemon.log")

    def test_install_writes_utf16_task_and_registers_it(self) -> None:
        """安装应写配置和计划任务 XML，并按顺序注册、启动任务。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._config(root)
            manager = TaskSchedulerServiceManager(
                state_dir=config.state_dir,
                python_executable=root / "python.exe",
            )

            with patch("token_monitor.service.subprocess.run") as run:
                run.return_value = Mock(returncode=0)
                manager.install(config, start=True)

            self.assertTrue(config.config_path.is_file())
            self.assertTrue(manager.task_path.is_file())
            document = ElementTree.fromstring(
                manager.task_path.read_text(encoding="utf-16")
            )
            self.assertEqual(
                _text(document, "Actions/Exec/Command"),
                "cmd.exe",
            )
            commands = [" ".join(call.args[0]) for call in run.call_args_list]
            self.assertEqual(
                commands,
                [
                    "schtasks /Create /TN TokenMonitor /XML "
                    f"{manager.task_path} /F",
                    "schtasks /Run /TN TokenMonitor",
                ],
            )

    def test_task_xml_file_is_utf16_with_bom(self) -> None:
        """schtasks 要求 XML 为带 BOM 的 UTF-16 文件。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._config(root)
            manager = TaskSchedulerServiceManager(
                state_dir=config.state_dir,
                python_executable=root / "python.exe",
            )

            with patch("token_monitor.service.subprocess.run") as run:
                run.return_value = Mock(returncode=0)
                manager.install(config, start=False)

            raw = manager.task_path.read_bytes()
            self.assertIn(raw[:2], (b"\xff\xfe", b"\xfe\xff"))
            self.assertIn("LogonTrigger", raw.decode("utf-16"))

    def test_render_task_xml_writes_user_when_configured(self) -> None:
        """显式指定 user 时才写出 UserId 元素。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = TaskSchedulerServiceManager(
                state_dir=root / "state",
                python_executable=root / "python.exe",
                user="dev@example.com",
            )

            document = ElementTree.fromstring(manager.render_task_xml())

        self.assertEqual(
            _text(document, "Principals/Principal/UserId"),
            "dev@example.com",
        )

    def test_install_without_start_skips_run(self) -> None:
        """start=False 时只注册任务，不应立即启动进程。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._config(root)
            manager = TaskSchedulerServiceManager(
                state_dir=config.state_dir,
                python_executable=root / "python.exe",
            )

            with patch("token_monitor.service.subprocess.run") as run:
                run.return_value = Mock(returncode=0)
                manager.install(config, start=False)

            commands = [" ".join(call.args[0]) for call in run.call_args_list]
            self.assertEqual(len(commands), 1)
            self.assertIn("/Create", commands[0])
            self.assertFalse(any("/Run" in command for command in commands))

    def test_install_rejects_mismatched_state_dir(self) -> None:
        """配置与管理器的 state_dir 不一致时应拒绝安装。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._config(root)
            manager = TaskSchedulerServiceManager(
                state_dir=root / "other-state",
                python_executable=root / "python.exe",
            )

            with patch("token_monitor.service.subprocess.run") as run:
                run.return_value = Mock(returncode=0)
                with self.assertRaises(ServiceError):
                    manager.install(config)

    def test_stop_ends_task_without_raising(self) -> None:
        """停止应结束任务，任务未运行时也不抛异常。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = TaskSchedulerServiceManager(state_dir=root / "state")

            with patch("token_monitor.service.subprocess.run") as run:
                run.return_value = Mock(returncode=1)

                manager.stop()

            self.assertEqual(
                run.call_args_list[0].args[0],
                ["schtasks", "/End", "/TN", "TokenMonitor"],
            )
            self.assertEqual(run.call_args_list[0].kwargs, {"check": False})

    def test_restart_ends_then_runs(self) -> None:
        """重启必须先结束旧进程再启动，让 daemon 重读 service.json。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = TaskSchedulerServiceManager(state_dir=root / "state")

            with patch("token_monitor.service.subprocess.run") as run:
                run.return_value = Mock(returncode=0)

                manager.restart()

            commands = [" ".join(call.args[0]) for call in run.call_args_list]
            self.assertEqual(
                commands,
                [
                    "schtasks /End /TN TokenMonitor",
                    "schtasks /Run /TN TokenMonitor",
                ],
            )

    def test_status_returns_exit_code_without_raising(self) -> None:
        """任务不存在时 schtasks /Query 返回非 0，应原样返回退出码。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = TaskSchedulerServiceManager(state_dir=root / "state")

            with patch("token_monitor.service.subprocess.run") as run:
                run.return_value = Mock(returncode=1)

                status = manager.status()

            self.assertEqual(status, 1)
            self.assertEqual(
                run.call_args_list[0].args[0],
                ["schtasks", "/Query", "/TN", "TokenMonitor"],
            )
            self.assertEqual(run.call_args_list[0].kwargs, {"check": False})

    def test_uninstall_deletes_task_and_files_idempotently(self) -> None:
        """卸载应注销任务并删除 XML 与配置，重复卸载不报错。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = TaskSchedulerServiceManager(
                state_dir=root / "state",
                python_executable=root / "python.exe",
            )
            manager.state_dir.mkdir(parents=True, exist_ok=True)
            manager.task_path.write_text(
                manager.render_task_xml(),
                encoding="utf-16",
            )
            manager.config_path.write_text("{}", encoding="utf-8")

            with patch("token_monitor.service.subprocess.run") as run:
                run.return_value = Mock(returncode=0)
                manager.uninstall()
                manager.uninstall()

            self.assertFalse(manager.task_path.exists())
            self.assertFalse(manager.config_path.exists())
            commands = [" ".join(call.args[0]) for call in run.call_args_list]
            self.assertTrue(all("/Delete" in command for command in commands))
            self.assertTrue(all("schtasks" in command for command in commands))
            self.assertEqual(run.call_args_list[0].kwargs, {"check": False})

    def test_start_reports_missing_schtasks(self) -> None:
        """找不到 schtasks 时必须以 ServiceError 报告并带上命令名。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = TaskSchedulerServiceManager(state_dir=root / "state")

            with patch(
                "token_monitor.service.subprocess.run",
                side_effect=FileNotFoundError("schtasks"),
            ):
                with self.assertRaisesRegex(ServiceError, "schtasks"):
                    manager.start()

    def test_logs_prints_last_lines(self) -> None:
        """logs 应打印最后若干行日志并返回 0。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = TaskSchedulerServiceManager(state_dir=root / "state")
            manager.state_dir.mkdir(parents=True, exist_ok=True)
            manager.log_path.write_text(
                "第一行\n第二行\n第三行\n",
                encoding="utf-8",
            )
            buffer = io.StringIO()

            with contextlib.redirect_stdout(buffer):
                status = manager.logs(lines=2)

            self.assertEqual(status, 0)
            self.assertEqual(buffer.getvalue(), "第二行\n第三行\n")

    def test_logs_replaces_invalid_utf8(self) -> None:
        """日志里的非法 UTF-8 字节应被替换，而不是中断输出。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = TaskSchedulerServiceManager(state_dir=root / "state")
            manager.state_dir.mkdir(parents=True, exist_ok=True)
            manager.log_path.write_bytes(b"\xff\xfe bad\n")
            buffer = io.StringIO()

            with contextlib.redirect_stdout(buffer):
                status = manager.logs(lines=5)

            self.assertEqual(status, 0)
            self.assertIn("bad", buffer.getvalue())

    def test_logs_without_file_prints_hint(self) -> None:
        """日志文件不存在时应给出中文提示而不是报错。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = TaskSchedulerServiceManager(state_dir=root / "state")
            buffer = io.StringIO()

            with contextlib.redirect_stdout(buffer):
                status = manager.logs(lines=10)

            self.assertEqual(status, 0)
            self.assertIn("日志", buffer.getvalue())

    def test_logs_rejects_non_positive_lines(self) -> None:
        """日志行数必须大于 0。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = TaskSchedulerServiceManager(state_dir=root / "state")

            with self.assertRaises(ValueError):
                manager.logs(lines=0)

    def test_logs_follow_prints_appended_content(self) -> None:
        """follow 模式应先打印尾部，再持续输出新增内容。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = TaskSchedulerServiceManager(state_dir=root / "state")
            manager.state_dir.mkdir(parents=True, exist_ok=True)
            manager.log_path.write_text("第一行\n", encoding="utf-8")
            buffer = io.StringIO()
            sleeps: list[float] = []

            def fake_sleep(seconds: float) -> None:
                """第一次追加日志，第二次模拟用户按 Ctrl+C 退出。"""

                if sleeps:
                    raise KeyboardInterrupt
                sleeps.append(seconds)
                with manager.log_path.open("a", encoding="utf-8") as handle:
                    handle.write("追加行\n")

            with patch(
                "token_monitor.service.time.sleep",
                side_effect=fake_sleep,
            ):
                with contextlib.redirect_stdout(buffer):
                    status = manager.logs(lines=10, follow=True)

            self.assertEqual(status, 0)
            self.assertEqual(buffer.getvalue(), "第一行\n追加行\n")

    def test_logs_follow_returns_zero_on_interrupt(self) -> None:
        """用户按 Ctrl+C 退出跟踪时应返回 0，不视为错误。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = TaskSchedulerServiceManager(state_dir=root / "state")

            with patch(
                "token_monitor.service.time.sleep",
                side_effect=KeyboardInterrupt,
            ):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    status = manager.logs(lines=5, follow=True)

            self.assertEqual(status, 0)

    def test_current_uid_tolerates_missing_getuid(self) -> None:
        """没有 os.getuid 的 Windows 上调用也不能抛 AttributeError。"""

        with patch("token_monitor.service.os.getuid", None):
            self.assertEqual(_current_uid(), 0)

    def test_create_service_manager_selects_platform(self) -> None:
        """工厂函数应按 platform 参数选择实现，不依赖真实系统。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)

            linux_manager = create_service_manager(
                root / "state",
                root / "units",
                None,
                platform="linux",
            )
            darwin_manager = create_service_manager(
                root / "state",
                root / "agents",
                None,
                platform="darwin",
            )
            windows_manager = create_service_manager(
                root / "state",
                root / "units",
                None,
                platform="win32",
            )
            alias_manager = create_service_manager(
                root / "state",
                root / "units",
                None,
                platform="windows",
            )

        self.assertIs(type(linux_manager), UserServiceManager)
        self.assertIsInstance(darwin_manager, LaunchdServiceManager)
        self.assertIsInstance(windows_manager, TaskSchedulerServiceManager)
        self.assertIsInstance(alias_manager, TaskSchedulerServiceManager)
        self.assertEqual(windows_manager.state_dir, (root / "state").resolve())
        self.assertEqual(windows_manager.task_name, "TokenMonitor")

    def test_windows_service_available_requires_windows_and_schtasks(
        self,
    ) -> None:
        """只有 Windows 且 PATH 中存在 schtasks 时才认为可用。"""

        with patch("token_monitor.service.sys.platform", "win32"):
            with patch(
                "token_monitor.service.shutil.which",
                return_value="C:/Windows/System32/schtasks.exe",
            ):
                self.assertTrue(windows_service_available())
            with patch("token_monitor.service.shutil.which", return_value=None):
                self.assertFalse(windows_service_available())
        with patch("token_monitor.service.sys.platform", "linux"):
            with patch(
                "token_monitor.service.shutil.which",
                return_value="/usr/bin/schtasks",
            ):
                self.assertFalse(windows_service_available())

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
