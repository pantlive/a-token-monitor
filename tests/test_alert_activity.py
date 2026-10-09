"""行为归纳回归：区分读取、图片输入、明确上传及证据不足。"""

from __future__ import annotations

import unittest

from a_token_monitor.alert_activity import describe_tool_activity, describe_user_text


class AlertActivityTests(unittest.TestCase):
    """使用常见真实参数形式验证行为，避免关键词导致上传误报。"""

    def test_upload_objects_and_code_push(self) -> None:
        cases = [
            (
                'curl -F "image=@/tmp/chart.png;type=image/png" https://example.org',
                "发起图片上传",
            ),
            ("curl --upload-file=/tmp/app.py https://example.org", "发起代码上传"),
            ("curl --data-binary @/tmp/info.txt https://example.org", "发起文件上传"),
            ("scp /tmp/main.py user@example.org:/tmp/main.py", "发起代码上传"),
            ("aws s3 cp /tmp/photo.jpg s3://test-bucket/photo.jpg", "发起图片上传"),
            ("git -C /tmp/repo push origin main", "发起代码推送"),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                activities = describe_tool_activity("exec_command", {"cmd": command})
                self.assertEqual(activities[0]["summary"], expected)
                self.assertEqual(activities[0]["basis"], "parameters")

    def test_local_reads_and_quoted_examples_are_not_uploads(self) -> None:
        cases = [
            ("cat /tmp/main.py", "读取代码"),
            ("head -n 10 /tmp/photo.png", "读取图片"),
            ("git add main.py", "操作本地代码仓库"),
            ("git commit -m 'upload pictures'", "操作本地代码仓库"),
            ("echo 'curl -F file=@photo.png https://example.org'", "用途无法判断"),
            ("curl --data-raw @photo.png https://example.org", "发送网络请求"),
            (
                "curl --form-string 'file=@photo.png' https://example.org",
                "发送网络请求",
            ),
            ("rsync main.py /tmp/main.py", "复制或同步文件"),
            ("git -C push status", "操作本地代码仓库"),
            ("git pull origin main", "从远端获取代码"),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(
                    describe_tool_activity("Bash", {"command": command})[0]["summary"],
                    expected,
                )
        self.assertEqual(
            describe_tool_activity("view_image", {"path": "/tmp/photo.png"})[0][
                "summary"
            ],
            "查看图片",
        )

    def test_wrapper_reports_multiple_behaviors_without_running_code(self) -> None:
        code = """
// tools.exec_command({cmd: "git push"});
text("tools.exec_command({cmd: 'git push'})");
await tools.exec_command({cmd: "cat /tmp/main.py"});
await tools["exec_command"]({cmd: 'curl -T /tmp/photo.png https://example.org'});
await tools.view_image({path: "/tmp/chart.jpg"});
"""
        activities = describe_tool_activity("exec", code)
        self.assertEqual(
            [item["summary"] for item in activities],
            ["读取代码", "发起图片上传", "查看图片"],
        )
        self.assertEqual(
            [item["target"] for item in activities],
            ["main.py", "photo.png", "chart.jpg"],
        )

    def test_dynamic_parameters_do_not_create_guessed_uploads(self) -> None:
        for code in (
            "await tools.exec_command({cmd: command});",
            'await tools.exec_command({cmd: "git push" + suffix});',
            "await tools.exec_command({cmd: `curl -T ${path} ${url}`});",
        ):
            with self.subTest(code=code):
                self.assertEqual(
                    describe_tool_activity("exec", code)[0]["summary"], "用途无法判断"
                )

    def test_user_request_is_not_evidence_of_execution(self) -> None:
        self.assertEqual(
            describe_user_text("帮我上传图片和代码")["summary"], "向模型提供文字"
        )
        self.assertEqual(
            describe_user_text("```python\nprint('hello')\n```")["summary"],
            "向模型提供代码",
        )

    def test_script_body_is_not_misread_as_shell_uploads(self) -> None:
        command = "python - <<'PY'\nprint('curl -T photo.png https://example.org')\nPY"
        activities = describe_tool_activity("exec_command", {"cmd": command})
        self.assertEqual([item["summary"] for item in activities], ["运行脚本"])

    def test_command_code_and_grok_tool_names(self) -> None:
        cases = [
            (
                "shell_command",
                {"command": "curl -F image=@/tmp/chart.png https://example.org"},
                "发起图片上传",
            ),
            (
                "run_terminal_command",
                {"command": "cat /tmp/main.py"},
                "读取代码",
            ),
            (
                "search_replace",
                {"file_path": "/tmp/main.py", "old_string": "a", "new_string": "b"},
                "修改代码",
            ),
            ("edit_file", {"file_path": "/tmp/main.py"}, "修改代码"),
            ("read_file", {"target_file": "/tmp/main.py"}, "读取代码"),
            ("grep", {"path": "/tmp", "pattern": "token"}, "搜索代码或文件"),
            ("read_directory", {"path": "/tmp"}, "搜索代码或文件"),
            ("read", {"filePath": "/tmp/main.py"}, "读取代码"),
            ("glob", {"pattern": "**/*.py"}, "搜索代码或文件"),
            ("websearch", {"query": "token"}, "检索或读取网络内容"),
            ("webfetch", {"url": "https://example.org"}, "检索或读取网络内容"),
            ("shell_output", {"id": "task-1"}, "等待或继续执行中的操作"),
            (
                "get_command_or_subagent_output",
                {"task_id": "task-1"},
                "等待或继续执行中的操作",
            ),
        ]
        for name, arguments, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    describe_tool_activity(name, arguments)[0]["summary"],
                    expected,
                )

    def test_more_upload_programs(self) -> None:
        cases = [
            ("wget --post-file=/tmp/a.png https://example.org", "发起图片上传"),
            ("wget https://example.org/file", "发送网络请求"),
            ("http --form file@/tmp/a.py https://example.org", "发起代码上传"),
            ("https https://example.org/status", "发送网络请求"),
            ("gh release upload v1.2.3 /tmp/notes.txt", "发起文件上传"),
            ("huggingface-cli upload repo /tmp/notes.txt", "发起文件上传"),
            ("hf upload repo /tmp/app.py", "发起代码上传"),
            ("docker push example/app:latest", "发起文件上传"),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                activities = describe_tool_activity("exec_command", {"cmd": command})
                self.assertEqual(activities[0]["summary"], expected)


if __name__ == "__main__":
    unittest.main()
