"""DeepSeek Harness 身份、活动会话和 projcache 用量测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_reset_monitor.dsh import (
    list_dsh_active_sessions,
    parse_dsh_projcache,
    read_dsh_account,
    read_dsh_quota,
    resolve_dsh_homes,
)
from codex_reset_monitor.registry import MultiSessionRegistry
from codex_reset_monitor.usage import TokenUsage, UsageAggregator, _estimate_usage


class DshAccountTests(unittest.TestCase):
    """验证凭据和模型读取不会泄漏 API Key。"""

    def test_reads_identity_without_exposing_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / ".dsh"
            home.mkdir()
            (home / ".anonymous-user-id").write_text("anon-1\n", encoding="utf-8")
            (home / ".credentials.yaml").write_text(
                "apiKey: SECRET-DSH-KEY\n",
                encoding="utf-8",
            )
            (home / "settings.yaml").write_text(
                "agent-default-model:\n"
                "  provider: command-code\n"
                "  model: deepseek/deepseek-v4.1-flash\n"
                "llm-pi-ai:\n"
                "  providers:\n"
                "    command-code:\n"
                "      apiKey: SECRET-DSH-KEY\n",
                encoding="utf-8",
            )

            account = read_dsh_account(home)
            quota = read_dsh_quota(home, now=1_000.0)

        self.assertEqual(account.account_id, "anon-1")
        self.assertTrue(account.has_credentials)
        self.assertEqual(account.model, "deepseek/deepseek-v4.1-flash")
        self.assertIsNotNone(quota)
        dumped = json.dumps(quota.metadata)
        self.assertNotIn("SECRET-DSH-KEY", dumped)
        self.assertEqual(quota.source, "dsh-local")
        self.assertEqual(resolve_dsh_homes(()), ())


class DshSessionTests(unittest.TestCase):
    """验证只把打开 session.lock 的进程算作活动会话。"""

    def test_lists_lock_backed_session_and_skips_title(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = _make_dsh_home(root)
            session_id = "session-1"
            lock = (
                home
                / "sessions"
                / "--workspace-demo--"
                / session_id
                / "session.lock"
            )
            proc_root = root / "proc"
            _write_process(
                proc_root,
                pid=70,
                comm="MainThread",
                command=("node", "/usr/bin/dsh", "web"),
                open_files=(lock,),
            )

            sessions = list_dsh_active_sessions(home, proc_root=proc_root, now=2_000.0)

        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session.session_id, session_id)
        self.assertEqual(session.cwd, "/workspace/demo")
        self.assertEqual(session.pids, (70,))
        self.assertEqual(session.source, "dsh")
        self.assertNotIn("SECRET-PROMPT", json.dumps(session.to_record()))


class DshUsageTests(unittest.TestCase):
    """验证 projcache 累计 token 转成增量和 API 等价估算。"""

    def test_indexes_cumulative_totals_as_usage_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = _make_dsh_home(root)
            registry = MultiSessionRegistry(root / "state")
            aggregator = UsageAggregator(
                discovery_interval=0.01,
                dsh_homes=(home,),
            )
            state = aggregator.snapshot({"codex": registry}, now=1_789_872_559.0)

        today = next(item for item in state["periods"] if item["key"] == "today")
        accounts = [
            item
            for item in today["accounts"]
            if item["account"] in {"anon-1", "dsh"}
        ]
        self.assertEqual(len(accounts), 1)
        account = accounts[0]
        self.assertEqual(account["input_tokens"], 1200)
        self.assertEqual(account["cached_input_tokens"], 200)
        self.assertEqual(account["output_tokens"], 50)
        self.assertEqual(account["models"][0]["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(account["projects"][0]["project"], "/workspace/demo")
        self.assertAlmostEqual(
            account["estimated_cost_usd"],
            _estimate_usage(
                TokenUsage(
                    input_tokens=1200,
                    cached_input_tokens=200,
                    output_tokens=50,
                    total_tokens=1250,
                ),
                "deepseek/deepseek-v4.1-flash",
            )["estimated_cost_usd"],
        )

    def test_parse_ignores_title_and_prompt_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "session-1.json"
            path.write_text(
                json.dumps(_projcache_payload()),
                encoding="utf-8",
            )
            snapshot = parse_dsh_projcache(path)

        self.assertIsNotNone(snapshot)
        dumped = json.dumps(snapshot.__dict__)
        self.assertNotIn("SECRET-PROMPT", dumped)
        self.assertNotIn("帮我把图片收藏", dumped)


def _make_dsh_home(root: Path) -> Path:
    """构造带身份、projcache 和一个 session.lock 的 DSH_HOME。"""

    home = root / ".dsh"
    home.mkdir()
    (home / ".anonymous-user-id").write_text("anon-1\n", encoding="utf-8")
    (home / ".credentials.yaml").write_text("apiKey: SECRET-DSH-KEY\n", encoding="utf-8")
    cache_dir = home / "storages" / "session_projcache" / "sessions"
    cache_dir.mkdir(parents=True)
    (cache_dir / "session-1.json").write_text(
        json.dumps(_projcache_payload()),
        encoding="utf-8",
    )
    session_dir = home / "sessions" / "--workspace-demo--" / "session-1"
    session_dir.mkdir(parents=True)
    (session_dir / "session.lock").write_text("", encoding="utf-8")
    return home


def _projcache_payload() -> dict[str, object]:
    return {
        "version": 7,
        "record": {
            "identity": {
                "cwd": "/workspace/demo",
            },
            "rows": {
                "title": {"val": "帮我把图片收藏 SECRET-PROMPT"},
                "tokenUsage": {
                    "val": {
                        "totals": {
                            "uncachedInputTokens": 1000,
                            "outputTokens": 50,
                            "cacheReadTokens": 200,
                            "cacheWriteTokens": 0,
                        }
                    }
                },
                "modelSelection": {
                    "val": {
                        "lastUsed": {
                            "provider": "command-code",
                            "model": "deepseek/deepseek-v4.1-flash",
                        }
                    }
                },
                "sessionListMetadata": {
                    "val": {"lastPromptAt": 1_789_872_559_000}
                },
            },
        },
    }


def _write_process(
    proc_root: Path,
    pid: int,
    comm: str,
    command: tuple[str, ...],
    open_files: tuple[Path, ...] = (),
    ppid: int = 1,
    start: str = "1000",
) -> None:
    directory = proc_root / str(pid)
    (directory / "fd").mkdir(parents=True)
    (directory / "comm").write_text(f"{comm}\n", encoding="utf-8")
    (directory / "cmdline").write_bytes(
        b"\0".join(item.encode("utf-8") for item in command) + b"\0"
    )
    fields = ["S", str(ppid)] + ["0"] * 17 + [start]
    (directory / "stat").write_text(
        f"{pid} ({comm}) " + " ".join(fields),
        encoding="utf-8",
    )
    for index, path in enumerate(open_files, start=3):
        (directory / "fd" / str(index)).symlink_to(path)


if __name__ == "__main__":
    unittest.main()
