"""code agent 进程识别测试。"""

from __future__ import annotations

import unittest

from token_monitor.agents import identify_agent, product_label


class AgentIdentificationTests(unittest.TestCase):
    """验证只把真实 CLI 识别成监控对象。"""

    def test_identifies_common_cli_binaries(self) -> None:
        cases = (
            (("codex", "exec"), "", "codex"),
            (("grok", "--resume", "abc"), "grok", "grok"),
            (("kimi",), "kimi", "kimi"),
            (("claude",), "claude", "claude"),
            (("opencode",), "opencode", "opencode"),
            (
                ("node", "/home/lsl/env/node/bin/dsh", "web"),
                "MainThread",
                "dsh",
            ),
            (("python3", "/opt/kimi-code/bin/kimi"), "python3", "kimi"),
        )
        for command, comm, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(identify_agent(command, comm), expected)

    def test_does_not_treat_search_arguments_as_agents(self) -> None:
        self.assertIsNone(identify_agent(("rg", "-i", "grok"), "rg"))
        self.assertIsNone(identify_agent(("grep", "dsh"), "grep"))

    def test_ignores_the_monitor_process_itself(self) -> None:
        self.assertIsNone(
            identify_agent(
                ("python", "-m", "token_monitor", "daemon"),
                "python",
            )
        )
        self.assertIsNone(
            identify_agent(("codex-reset-monitor", "daemon"), "codex-reset-monitor")
        )

    def test_product_labels_cover_requested_agents(self) -> None:
        self.assertEqual(product_label("dsh"), "DeepSeek Harness")
        self.assertEqual(product_label("kimi"), "Kimi Code")
        self.assertEqual(product_label("grok"), "Grok CLI")


if __name__ == "__main__":
    unittest.main()
