"""实时速度：Codex 精确 TTFT/TPS 与其它 provider 的采样速率。"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from a_token_monitor.speed import SpeedTracker


def _codex_line(payload: dict, kind: str = "event_msg", at: str = "2026-10-19T03:31:40.020Z") -> str:
    return json.dumps({"timestamp": at, "type": kind, "payload": payload})


def _item_completed(
    item_type: str,
    started_ms: int,
    completed_ms: int,
    at: str = "2026-10-19T03:31:41.714Z",
) -> str:
    return _codex_line(
        {
            "type": "item_completed",
            "item": {"type": item_type},
            "started_at_ms": started_ms,
            "completed_at_ms": completed_ms,
        },
        at=at,
    )


def _usage_record(output: int) -> str:
    return _codex_line(
        {"usage": {"output_tokens": output, "input_tokens": 100}},
        kind="token_usage_record",
    )


class CodexSpeedTests(unittest.TestCase):
    """Codex rollout：TTFT 从触发请求的上一条事件算，TPS 用生成窗口。"""

    def setUp(self) -> None:
        self.tracker = SpeedTracker()
        self.key = "codex|/tmp/rollout.jsonl"

    def _feed(self, *lines: str) -> None:
        for line in lines:
            self.tracker._observe_codex("codex", self.key, line, time.time())

    def test_measures_ttft_and_tps_from_item_windows(self) -> None:
        self._feed(
            _codex_line({"type": "task_started", "started_at": 1792380700}),
            _item_completed("UserMessage", 1792380701714, 1792380701714),
            # 请求在用户消息（41.714s）之后开始，首个输出 item 43.375s 才出现 → TTFT 1.661s
            _item_completed(
                "AgentMessage",
                1792380703375,
                1792380704037,
                at="2026-10-19T03:31:44.037Z",
            ),
            _usage_record(120),
        )
        history = list(self.tracker._responses["codex"])
        self.assertEqual(len(history), 1)
        _, ttft, tps, output = history[0]
        self.assertAlmostEqual(ttft, 1.661, places=2)
        self.assertAlmostEqual(tps, 120 / 0.662, places=1)
        self.assertEqual(output, 120)

    def test_ttft_uses_the_event_that_triggered_the_request(self) -> None:
        """一轮里的第二次请求：TTFT 从工具输出算起，而不是回合开始。"""

        self._feed(
            _codex_line({"type": "task_started", "started_at": 1792380700}),
            _item_completed("UserMessage", 1792380701714, 1792380701714),
            _item_completed(
                "AgentMessage",
                1792380703375,
                1792380704037,
                at="2026-10-19T03:31:44.037Z",
            ),
            _usage_record(120),
            # 工具调用在 45.0s 完成，下一次请求 45.5s 才吐出第一个 item
            _codex_line(
                {"type": "custom_tool_call_output", "call_id": "c1", "output": "ok"},
                kind="response_item",
                at="2026-10-19T03:31:45.000Z",
            ),
            _item_completed("AgentMessage", 1792380705500, 1792380706000),
            _usage_record(60),
        )
        history = list(self.tracker._responses["codex"])
        self.assertEqual(len(history), 2)
        self.assertAlmostEqual(history[1][1], 0.5, places=2)
        self.assertAlmostEqual(history[1][2], 120.0, places=1)

    def test_tool_items_do_not_start_a_generation_window(self) -> None:
        self._feed(
            _codex_line({"type": "task_started", "started_at": 1792380700}),
            _item_completed("CommandExecution", 1792380705277, 1792380705278),
            _usage_record(10),
        )
        self.assertEqual(list(self.tracker._responses.get("codex", [])), [])

    def test_zero_output_is_ignored(self) -> None:
        self._feed(
            _item_completed("AgentMessage", 1792380703375, 1792380704037),
            _usage_record(0),
        )
        self.assertEqual(list(self.tracker._responses.get("codex", [])), [])

    def test_snapshot_reports_the_latest_response(self) -> None:
        self._feed(
            _item_completed("AgentMessage", 1792380703375, 1792380704037),
            _usage_record(120),
        )
        at = self.tracker._responses["codex"][-1][0]
        snapshot = self.tracker.snapshot(now=at + 1)
        entry = snapshot["accounts"]["codex"]
        self.assertEqual(entry["source"], "codex-log")
        self.assertEqual(entry["output_tokens"], 120)
        self.assertAlmostEqual(entry["tps"], 181.3, places=1)
        self.assertIsNone(entry["ttft_ms"])
        self.assertFalse(entry["live"])
        self.assertEqual(snapshot["totals"]["accounts"], 1)

    def test_stale_response_stays_visible_but_not_live(self) -> None:
        """最后一次测量要一直看得到（带年龄），只是不再计入实时合计。"""

        self._feed(
            _item_completed("AgentMessage", 1792380703375, 1792380704037),
            _usage_record(120),
        )
        at = self.tracker._responses["codex"][-1][0]
        snapshot = self.tracker.snapshot(now=at + 120)
        entry = snapshot["accounts"]["codex"]
        self.assertFalse(entry["live"])
        self.assertGreaterEqual(entry["age_seconds"], 100)
        self.assertAlmostEqual(entry["tps"], 181.3, places=1)
        # 两分钟内仍计入合计，超过保留期就只留在账号卡片里。
        self.assertAlmostEqual(snapshot["totals"]["tps"], 181.3, places=1)
        far = self.tracker.snapshot(now=at + 10_000)
        self.assertEqual(far["totals"]["tps"], 0.0)
        self.assertIn("codex", far["accounts"])


class BackfillTests(unittest.TestCase):
    """第一次见到会话文件时回读尾部，重启后立刻能看到最近一次测量。"""

    def test_first_sight_backfills_the_precise_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rollout.jsonl"
            lines = [
                _codex_line({"type": "task_started", "started_at": 1792380700}),
                _item_completed("UserMessage", 1792380701714, 1792380701714),
                _item_completed(
                    "AgentMessage",
                    1792380703375,
                    1792380704037,
                    at="2026-10-19T03:31:44.037Z",
                ),
                _usage_record(120),
            ]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tracker = SpeedTracker()
            session = {
                "account": "codex",
                "product": "codex",
                "jsonl_path": str(path),
            }
            tracker.sample_sessions([session], now=time.time())
            at = tracker._responses["codex"][-1][0]
            entry = tracker.snapshot(now=at + 5)["accounts"]["codex"]
            self.assertEqual(entry["source"], "codex-log")
            # 回读可能从回合中间开始，TTFT 不猜；TPS 由生成窗口得出。
            self.assertIsNone(entry["ttft_ms"])
            self.assertAlmostEqual(entry["tps"], 181.3, places=1)
            # 历史内容不算实时流量。
            self.assertEqual(tracker._samples, {})


class SampledSpeedTests(unittest.TestCase):
    """其它 provider：按新增字节里的输出 token 算滚动速率。"""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.path = self.root / "session.jsonl"
        self.path.write_text("", encoding="utf-8")
        self.session = {
            "account": "claude-work",
            "product": "claude",
            "jsonl_path": str(self.path),
        }

    def _append(self, output_tokens: int) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-10-19T03:31:45.000Z",
                        "message": {"usage": {"output_tokens": output_tokens}},
                    }
                )
                + "\n"
            )

    def test_reports_a_sampled_rate_for_other_products(self) -> None:
        tracker = SpeedTracker(window_seconds=10.0)
        tracker.sample_sessions([self.session], now=1000.0)  # 首次只记位置
        self._append(50)
        tracker.sample_sessions([self.session], now=1002.0)
        self._append(50)
        tracker.sample_sessions([self.session], now=1004.0)
        snapshot = tracker.snapshot(now=1004.0)
        entry = snapshot["accounts"]["claude-work"]
        self.assertEqual(entry["source"], "sampled")
        self.assertIsNone(entry["ttft_ms"])
        self.assertTrue(entry["live"])
        # 首个采样点是基线（50），其后 2 秒又追加 50 → 25 tps
        self.assertAlmostEqual(entry["tps"], 25.0, places=1)
        self.assertAlmostEqual(snapshot["totals"]["tps"], 25.0, places=1)

    def test_history_is_not_counted_as_live_traffic(self) -> None:
        tracker = SpeedTracker(window_seconds=10.0)
        self._append(500)
        tracker.sample_sessions([self.session], now=1000.0)  # 首次只记位置
        snapshot = tracker.snapshot(now=1001.0)
        self.assertEqual(snapshot["accounts"], {})

    def test_windows_are_pruned(self) -> None:
        tracker = SpeedTracker(window_seconds=4.0)
        tracker.sample_sessions([self.session], now=1000.0)
        self._append(10)
        tracker.sample_sessions([self.session], now=1001.0)
        self._append(10)
        tracker.sample_sessions([self.session], now=1020.0)
        snapshot = tracker.snapshot(now=1020.0)
        self.assertEqual(snapshot["accounts"], {})

    def test_duplicate_codex_token_count_is_not_double_counted(self) -> None:
        from a_token_monitor.speed import _line_output_tokens

        record = json.loads(_usage_record(120))
        token_count = json.loads(
            _codex_line(
                {
                    "type": "token_count",
                    "info": {"last_token_usage": {"output_tokens": 120}},
                }
            )
        )
        self.assertEqual(_line_output_tokens(record), 120)
        self.assertEqual(_line_output_tokens(token_count), 0)


if __name__ == "__main__":  # pragma: no cover - 便于单独运行
    unittest.main()
