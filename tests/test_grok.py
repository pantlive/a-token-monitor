"""Grok 本地用量、额度和身份读取测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from token_monitor.grok import (
    decode_grok_project,
    list_grok_active_sessions,
    parse_grok_log_chunk,
    read_grok_account,
    read_grok_quota,
    resolve_grok_homes,
)
from token_monitor.registry import MultiSessionRegistry
from token_monitor.usage import TokenUsage, UsageAggregator, _estimate_usage


class GrokUsageTests(unittest.TestCase):
    """验证 Grok unified.jsonl 请求用量和 SuperGrok 额度读取。"""

    def test_parses_inference_done_and_maps_session_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            grok_home = root / ".grok"
            session_dir = (
                grok_home
                / "sessions"
                / "%2Fworkspace%2Fdemo"
                / "session-1"
            )
            session_dir.mkdir(parents=True)
            (session_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "current_model_id": "grok-4.6",
                        "info": {"cwd": "/workspace/demo"},
                    }
                ),
                encoding="utf-8",
            )
            log_path = grok_home / "logs" / "unified.jsonl"
            log_path.parent.mkdir(parents=True)
            log_path.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in (
                        {
                            "ts": "2026-08-27T01:00:00Z",
                            "msg": "shell.turn.inference_start",
                            "sid": "session-1",
                            "ctx": {"loop_index": 0},
                        },
                        {
                            "ts": "2026-08-27T01:00:01Z",
                            "msg": "shell.turn.inference_done",
                            "sid": "session-1",
                            "ctx": {
                                "loop_index": 0,
                                "prompt_tokens": 1000,
                                "cached_prompt_tokens": 200,
                                "completion_tokens": 50,
                                "reasoning_tokens": 40,
                            },
                        },
                        {
                            "ts": "2026-08-27T01:00:02Z",
                            "msg": "shell.tool.exec_done",
                            "sid": "session-1",
                            "ctx": {"command": "should-not-be-parsed"},
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            (grok_home / "auth.json").write_text(
                json.dumps(
                    {
                        "https://auth.x.ai::client": {
                            "user_id": "grok-user-1",
                            "email": "user@example.com",
                            "principal_type": "User",
                            "key": "SECRET-TOKEN",
                            "refresh_token": "SECRET-REFRESH",
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry = MultiSessionRegistry(root / "state")
            aggregator = UsageAggregator(
                discovery_interval=0.01,
                grok_homes=(grok_home,),
            )

            state = aggregator.snapshot(
                {"codex": registry},
                now=_timestamp("2026-08-27T12:00:00Z"),
            )

        account = _period(state, "today")["accounts"]
        grok_accounts = [
            item for item in account if item["account_id"] == "grok-user-1"
        ]
        self.assertEqual(len(grok_accounts), 1)
        grok_account = grok_accounts[0]
        self.assertEqual(grok_account["total_tokens"], 1050)
        self.assertEqual(grok_account["input_tokens"], 1000)
        self.assertEqual(grok_account["cached_input_tokens"], 200)
        self.assertEqual(grok_account["output_tokens"], 50)
        self.assertEqual(grok_account["reasoning_output_tokens"], 40)
        self.assertEqual(grok_account["models"][0]["model"], "grok-4.6")
        self.assertEqual(grok_account["projects"][0]["project"], "/workspace/demo")
        self.assertEqual(
            grok_account["estimated_cost_usd"],
            _estimate_usage(
                TokenUsage(
                    input_tokens=1000,
                    cached_input_tokens=200,
                    output_tokens=50,
                    reasoning_output_tokens=40,
                    total_tokens=1050,
                ),
                "grok-4.6",
            )["estimated_cost_usd"],
        )
        dumped = json.dumps(state)
        self.assertNotIn("SECRET-TOKEN", dumped)
        self.assertNotIn("SECRET-REFRESH", dumped)

    def test_reads_weekly_credit_quota_from_billing_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            grok_home = Path(temporary_directory) / ".grok"
            log_path = grok_home / "logs" / "unified.jsonl"
            log_path.parent.mkdir(parents=True)
            log_path.write_text(
                json.dumps(
                    {
                        "ts": "2026-08-27T02:00:00Z",
                        "msg": "billing: fetched credits config",
                        "ctx": {
                            "subscriptionTier": "SuperGrok",
                            "config": {
                                "creditUsagePercent": 42.5,
                                "currentPeriod": {
                                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                                    "start": "2026-08-21T02:14:01+00:00",
                                    "end": "2026-08-28T02:14:01+00:00",
                                },
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            snapshot = read_grok_quota(grok_home)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.plan_type, "SuperGrok")
        self.assertEqual(snapshot.source, "grok-unified-log")
        window = snapshot.window("grok", "weekly")
        self.assertIsNotNone(window)
        assert window is not None
        self.assertEqual(window.used_percent, 42.5)
        self.assertEqual(window.window_minutes, 10080.0)

    def test_does_not_read_auth_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            grok_home = Path(temporary_directory) / ".grok"
            grok_home.mkdir()
            (grok_home / "auth.json").write_text(
                json.dumps(
                    {
                        "issuer": {
                            "user_id": "abc",
                            "principal_type": "User",
                            "key": "SECRET-TOKEN",
                            "refresh_token": "SECRET-REFRESH",
                        }
                    }
                ),
                encoding="utf-8",
            )

            account = read_grok_account(grok_home)

        self.assertEqual(account.account_id, "abc")
        self.assertNotIn("SECRET", account.display_name)

    def test_long_context_grok_pricing_uses_official_200k_threshold(self) -> None:
        short = _estimate_usage(
            TokenUsage(input_tokens=199_999, cached_input_tokens=0, output_tokens=1_000),
            "grok-4.6",
        )
        long = _estimate_usage(
            TokenUsage(input_tokens=200_000, cached_input_tokens=0, output_tokens=1_000),
            "grok-4.6",
        )

        self.assertEqual(short["estimated_cost_usd"], 0.405998)
        self.assertEqual(long["estimated_cost_usd"], 0.812)

    def test_skips_non_inference_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "unified.jsonl"
            path.write_text(
                '{"ts":"2026-08-27T01:00:00Z","msg":"shell.tool.exec_done",'
                '"ctx":{"prompt_tokens":999}}\n'
                '{"ts":"2026-08-27T01:00:01Z","msg":"shell.turn.inference_done",'
                '"sid":"s1","ctx":{"prompt_tokens":10,"completion_tokens":2}}\n',
                encoding="utf-8",
            )

            parsed = parse_grok_log_chunk(
                path,
                offset=0,
                session_index={},
                default_model="grok-4.6",
            )

        self.assertEqual(len(parsed.events), 1)
        self.assertEqual(parsed.events[0].input_tokens, 10)
        self.assertEqual(parsed.events[0].output_tokens, 2)
        self.assertTrue(parsed.reached_eof)

    def test_resolve_homes_deduplicates_and_empty_disables_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "nope"
            self.assertEqual(
                resolve_grok_homes((missing, missing)),
                (missing.resolve(),),
            )
        self.assertEqual(resolve_grok_homes(()), ())


class GrokActiveSessionTests(unittest.TestCase):
    """验证按进程打开文件识别 Grok 活动会话。"""

    def _home(self, root: Path, session_id: str = "session-1") -> Path:
        """构造一个带 summary.json 与会话日志的 Grok 目录。"""

        grok_home = root / ".grok"
        session_dir = (
            grok_home / "sessions" / "%2Fworkspace%2Fdemo" / session_id
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "summary.json").write_text(
            json.dumps(
                {
                    "current_model_id": "grok-4.6",
                    "info": {"cwd": "/workspace/demo"},
                    "created_at": "2026-09-20T01:00:00Z",
                    "updated_at": "2026-09-20T02:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        (session_dir / "chat_history.jsonl").write_text("", encoding="utf-8")
        (session_dir / "updates.jsonl").write_text("", encoding="utf-8")
        return grok_home

    def _process(
        self,
        proc_root: Path,
        pid: int,
        *,
        open_path: Path | None,
        cwd: Path | None = None,
        comm: str = "grok",
    ) -> None:
        """在临时 /proc 树里写入一个 grok 进程。"""

        directory = proc_root / str(pid)
        (directory / "fd").mkdir(parents=True, exist_ok=True)
        (directory / "comm").write_text(f"{comm}\n", encoding="utf-8")
        (directory / "cmdline").write_bytes(f"{comm}\0".encode("utf-8"))
        (directory / "stat").write_text(
            f"{pid} ({comm}) " + " ".join(["S", "1"] + ["0"] * 17 + ["9"]),
            encoding="utf-8",
        )
        if cwd is not None:
            (directory / "cwd").symlink_to(cwd)
        if open_path is not None:
            (directory / "fd" / "3").symlink_to(open_path)

    def test_detects_session_from_open_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            grok_home = self._home(root)
            session_dir = (
                grok_home / "sessions" / "%2Fworkspace%2Fdemo" / "session-1"
            )
            proc_root = root / "proc"
            self._process(proc_root, 4242, open_path=session_dir / "summary.json.lock")

            sessions = list_grok_active_sessions(
                grok_home,
                proc_root=proc_root,
                now=1_789_708_700.0,
            )

        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session.session_id, "session-1")
        self.assertEqual(session.thread_id, "grok:session-1")
        self.assertEqual(session.cwd, "/workspace/demo")
        self.assertEqual(session.pids, (4242,))
        self.assertEqual(session.source, "grok-cli")
        self.assertEqual(session.status.value, "running")
        # 最近事件取会话目录里最后改动的文件
        self.assertIn(
            session.last_event_type,
            {"updates.jsonl", "chat_history.jsonl", "summary.json"},
        )
        self.assertTrue(str(session.jsonl_path).endswith("chat_history.jsonl"))
        self.assertEqual(session.metadata.get("model"), "grok-4.6")

    def test_merges_multiple_processes_and_ignores_others(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            grok_home = self._home(root)
            session_dir = (
                grok_home / "sessions" / "%2Fworkspace%2Fdemo" / "session-1"
            )
            proc_root = root / "proc"
            self._process(proc_root, 100, open_path=session_dir / "events.jsonl")
            self._process(proc_root, 101, open_path=session_dir / "terminal")
            self._process(proc_root, 102, open_path=None, comm="python")

            sessions = list_grok_active_sessions(
                grok_home,
                proc_root=proc_root,
                now=1_789_708_700.0,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].pids, (100, 101))

    def test_sessions_disappear_when_process_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            grok_home = self._home(root)
            proc_root = root / "proc"
            proc_root.mkdir()

            sessions = list_grok_active_sessions(
                grok_home,
                proc_root=proc_root,
                now=1_789_708_700.0,
            )

        self.assertEqual(sessions, ())

    def test_falls_back_to_process_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            grok_home = self._home(root)
            workspace = root / "demo"
            workspace.mkdir()
            proc_root = root / "proc"
            self._process(proc_root, 200, open_path=None, cwd=workspace)

            # 索引里的 cwd 与实际工作目录一致时才算命中
            (grok_home / "sessions" / "%2Fworkspace%2Fdemo" / "session-1" / "summary.json").write_text(
                json.dumps(
                    {
                        "current_model_id": "grok-4.6",
                        "info": {"cwd": str(workspace)},
                    }
                ),
                encoding="utf-8",
            )
            sessions = list_grok_active_sessions(
                grok_home,
                proc_root=proc_root,
                now=1_789_708_700.0,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].pids, (200,))

    def test_rotated_session_still_reported_from_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            grok_home = root / ".grok"
            session_dir = (
                grok_home / "sessions" / "%2Fworkspace%2Frotated" / "session-old"
            )
            session_dir.mkdir(parents=True)
            proc_root = root / "proc"
            self._process(proc_root, 300, open_path=session_dir / "events.jsonl")
            # 模拟日志轮转：会话目录被移走，进程仍持有旧路径
            session_dir.rename(session_dir.parent / "session-old.moved")

            sessions = list_grok_active_sessions(
                grok_home,
                proc_root=proc_root,
                now=1_789_708_700.0,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_id, "session-old")
        self.assertEqual(sessions[0].cwd, "/workspace/rotated")
        self.assertEqual(sessions[0].pids, (300,))

    def test_decode_grok_project(self) -> None:
        self.assertEqual(
            decode_grok_project("%2Fhome%2Flsl%2Fproject%2Fgithub%2FHeart-Plan"),
            "/home/lsl/project/github/Heart-Plan",
        )
        self.assertIsNone(decode_grok_project("not-a-path"))
        self.assertIsNone(decode_grok_project(""))


def _period(state: dict[str, object], key: str) -> dict[str, object]:
    """取出一个时间窗口。"""

    periods = state["periods"]
    assert isinstance(periods, list)
    for period in periods:
        assert isinstance(period, dict)
        if period["key"] == key:
            return period
    raise AssertionError(f"缺少时间窗口: {key}")


def _timestamp(value: str) -> float:
    """测试时间转 Unix 秒。"""

    from datetime import datetime, timezone

    return (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


if __name__ == "__main__":
    unittest.main()
