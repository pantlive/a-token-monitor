"""Windows 平台下的 CLI 集成测试（服务定义与工厂接线）。"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from token_monitor.cli import main


class WindowsServiceCliTests(unittest.TestCase):
    """验证 service 子命令在 Windows 上使用计划任务实现。"""

    def test_service_plist_prints_task_xml(self) -> None:
        """service plist 在 Windows 上打印计划任务 XML。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            buffer = io.StringIO()
            with (
                mock.patch("token_monitor.service.sys.platform", "win32"),
                contextlib.redirect_stdout(buffer),
            ):
                code = main(
                    ["--state-dir", str(root / "state"), "service", "plist"]
                )

        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("<Task ", output)
        self.assertIn("<LogonTrigger>", output)
        self.assertIn("<RunLevel>LeastPrivilege</RunLevel>", output)
        self.assertIn("service run", output)

    def test_service_install_reports_task_definition_path(self) -> None:
        """service install 在 Windows 上写出计划任务并提示定义路径。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            buffer = io.StringIO()
            with (
                mock.patch("token_monitor.service.sys.platform", "win32"),
                mock.patch("token_monitor.service.subprocess.run") as run,
                mock.patch.dict(
                    os.environ,
                    {"CODEX_HOME": str(root / "missing-codex-home")},
                ),
                contextlib.redirect_stdout(buffer),
            ):
                run.return_value = mock.Mock(returncode=0)
                code = main(
                    [
                        "--state-dir",
                        str(root / "state"),
                        "service",
                        "install",
                    ]
                )

            task_path = root / "state" / "token-monitor-task.xml"
            task_exists = task_path.exists()
            config_exists = (root / "state" / "service.json").exists()

        self.assertEqual(code, 0)
        self.assertTrue(task_exists)
        self.assertTrue(config_exists)
        output = buffer.getvalue()
        self.assertIn("token-monitor-task.xml", output)
        commands = [" ".join(call.args[0]) for call in run.call_args_list]
        self.assertTrue(any("schtasks" in command for command in commands), commands)
        self.assertTrue(any("/Create" in command for command in commands), commands)


if __name__ == "__main__":
    unittest.main()
