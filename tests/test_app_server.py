"""App Server JSON-RPC 客户端测试，不启动真实 Codex。"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from token_monitor.app_server import AppServerClient, AppServerConfig


class _FakeProcess:
    """满足客户端请求前置检查的假进程。"""

    def poll(self) -> None:
        """表示进程仍在运行。"""

        return None


class AppServerClientTests(unittest.TestCase):
    """验证响应匹配、分页和额度读取调用。"""

    def test_builds_environment_for_a_separate_codex_home(self) -> None:
        client = AppServerClient(AppServerConfig(codex_home=Path("~/.codex-work")))

        with patch.dict("os.environ", {"CODEX_HOME": "/old-home"}, clear=False):
            environment = client._environment()

        self.assertIsNotNone(environment)
        assert environment is not None
        self.assertEqual(environment["CODEX_HOME"], str(Path.home() / ".codex-work"))

    def test_read_rate_limits_uses_json_rpc_method(self) -> None:
        client = AppServerClient()
        client.process = _FakeProcess()  # type: ignore[assignment]
        client._messages.put(
            {
                "id": 1,
                "result": {
                    "planType": "plus",
                    "rateLimits": {
                        "limitId": "codex",
                        "primary": {
                            "usedPercent": 12,
                            "windowDurationMins": 300,
                            "resetsAt": 200,
                        },
                    },
                },
            }
        )

        with patch.object(client, "_send") as send:
            snapshot = client.read_rate_limits(now=100)

        self.assertEqual(snapshot.plan_type, "plus")
        self.assertEqual(snapshot.window("codex", "primary").resets_at, 200)
        self.assertEqual(send.call_args.args[0]["method"], "account/rateLimits/read")

    def test_list_threads_reads_all_pages_and_passes_all_source_kinds(self) -> None:
        client = AppServerClient()
        client.process = _FakeProcess()  # type: ignore[assignment]
        client._messages.put(
            {"id": 1, "result": {"data": [{"id": "one"}], "nextCursor": "next"}}
        )
        client._messages.put({"id": 2, "result": {"data": [{"id": "two"}]}})

        with patch.object(client, "_send") as send:
            threads = client.list_threads(page_size=1)

        self.assertEqual([item["id"] for item in threads], ["one", "two"])
        self.assertEqual(send.call_count, 2)
        first_params = send.call_args_list[0].args[0]["params"]
        self.assertIn("cli", first_params["sourceKinds"])
        self.assertIn("exec", first_params["sourceKinds"])
        self.assertIn("subAgent", first_params["sourceKinds"])
        self.assertEqual(send.call_args_list[1].args[0]["params"]["cursor"], "next")


if __name__ == "__main__":
    unittest.main()
