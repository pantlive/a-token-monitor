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
    """Codex rollout：TTFT 从触发请求的上一条事件算，TPS 用「请求开始 → 最后一个输出 item」。"""

    def setUp(self) -> None:
        self.tracker = SpeedTracker()
        self.key = "codex|/tmp/rollout.jsonl"

    def _feed(self, *lines: str) -> None:
        for line in lines:
            self.tracker._observe_codex("codex", self.key, line, time.time())

    def test_measures_ttft_and_tps_from_the_request_window(self) -> None:
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
        # 120 token 覆盖整个请求（含推理），分母是请求开始到最后一个输出 item 结束
        self.assertAlmostEqual(tps, 120 / (1792380704.037 - 1792380701.714), places=1)
        self.assertEqual(output, 120)

    def test_item_window_is_not_used_as_the_generation_window(self) -> None:
        """推理 item 的 started/completed 常只差 1 毫秒，不能拿它当分母。"""

        self._feed(
            _codex_line({"type": "task_started", "started_at": 1792380700}),
            _item_completed("UserMessage", 1792380701714, 1792380701714),
            _item_completed("Reasoning", 1792380703000, 1792380703001),
            _item_completed(
                "AgentMessage",
                1792380703375,
                1792380704037,
                at="2026-10-19T03:31:44.037Z",
            ),
            _usage_record(120),
        )
        _, ttft, tps, _ = list(self.tracker._responses["codex"])[0]
        self.assertAlmostEqual(ttft, 1.286, places=2)
        # 用 item 窗口会得到 120/0.038 ≈ 3158 tps；正确口径是请求窗口
        self.assertAlmostEqual(tps, 120 / (1792380704.037 - 1792380701.714), places=1)
        self.assertLess(tps, 100)

    def test_tool_events_do_not_trigger_a_new_request(self) -> None:
        """同一响应中间的工具调用不是下一次请求的起点。"""

        self._feed(
            _codex_line({"type": "task_started", "started_at": 1792380700}),
            _item_completed("UserMessage", 1792380701714, 1792380701714),
            _item_completed("Reasoning", 1792380703000, 1792380703001),
            _item_completed(
                "AgentMessage",
                1792380703375,
                1792380704037,
                at="2026-10-19T03:31:44.037Z",
            ),
            _usage_record(200),
            # 工具执行完成（不是一次新的模型请求），随后模型继续输出
            _item_completed("CommandExecution", 1792380705000, 1792380705001),
            _item_completed(
                "AgentMessage",
                1792380706000,
                1792380707000,
                at="2026-10-19T03:31:47.000Z",
            ),
            _usage_record(100),
        )
        first, second = list(self.tracker._responses["codex"])
        # 第一条请求从用户消息（41.714s）算起，而不是从命令执行（45.000s）算起
        self.assertAlmostEqual(first[1], 1.286, places=2)
        self.assertAlmostEqual(first[2], 200 / (1792380704.037 - 1792380701.714), places=1)
        # 第二条请求没有触发事件（工具输出没写），TTFT 不给
        self.assertAlmostEqual(second[1], -1.0, places=2)
        self.assertAlmostEqual(second[2], 100.0, places=1)

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
        self.assertAlmostEqual(history[1][2], 60.0, places=1)

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
        # 没有触发事件时退回 item 窗口：120 token / (44.037 - 43.375)
        expected = 120 / (1792380704.037 - 1792380703.375)
        self.assertAlmostEqual(entry["tps"], expected, places=1)
        # 两分钟内仍计入合计，超过保留期就只留在账号卡片里。
        self.assertAlmostEqual(snapshot["totals"]["tps"], expected, places=1)
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
            # 回读可能从回合中间开始，TTFT 不猜；TPS 用请求窗口（本文件里触发事件齐全）。
            self.assertIsNone(entry["ttft_ms"])
            self.assertAlmostEqual(
                entry["tps"], 120 / (1792380704.037 - 1792380701.714), places=1
            )
            # 历史内容不算实时流量。
            self.assertEqual(tracker._samples, {})


class ClaudeRequestWindowTests(unittest.TestCase):
    """Claude transcript：用 api-request 的开始时间算请求窗口，而不是消息间隔。"""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "session.jsonl"
        self.path.write_text("", encoding="utf-8")
        self.session = {
            "account_id": "claude-account",
            "product": "claude",
            "jsonl_path": str(self.path),
        }

    def _write(self, *lines: dict) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")

    @staticmethod
    def _request(ident: str, at: str) -> dict:
        return {"type": "api-request", "id": ident, "timestamp": at}

    @staticmethod
    def _message(ident: str, at: str, output: int, request: str | None) -> dict:
        line = {
            "type": "assistant",
            "timestamp": at,
            "message": {"id": ident, "usage": {"output_tokens": output}},
        }
        if request is not None:
            line["requestRef"] = request
        return line

    def test_rate_uses_the_request_window_not_the_gap_between_messages(self) -> None:
        tracker = SpeedTracker()
        # 上一条消息在 52.0s 完成；新请求 60.0s 才开始，62.0s 写完 400 token。
        # 用消息间隔会得到 400/10 = 40 tps，用请求窗口才是 400/2 = 200 tps。
        self._write(
            self._message("m0", "2026-10-19T03:31:52.000Z", 100, None),
            self._request("r1", "2026-10-19T03:32:00.000Z"),
            self._message("m1", "2026-10-19T03:32:02.000Z", 400, "r1"),
        )
        tracker.sample_sessions([self.session], now=1_000.0)
        entry = tracker.snapshot(now=1_000.0)["accounts"]["claude-account"]
        self.assertAlmostEqual(entry["tps"], 200.0, places=1)

    def test_repeated_writes_of_one_response_keep_a_single_history_point(self) -> None:
        tracker = SpeedTracker()
        self._write(
            self._request("r1", "2026-10-19T03:31:52.000Z"),
            self._message("m1", "2026-10-19T03:31:54.000Z", 258, "r1"),
            self._message("m1", "2026-10-19T03:31:54.100Z", 258, "r1"),
            self._message("m1", "2026-10-19T03:31:54.200Z", 258, "r1"),
        )
        tracker.sample_sessions([self.session], now=1_000.0)
        entry = tracker.snapshot(now=1_000.0)["accounts"]["claude-account"]
        self.assertEqual(len(entry["history"]), 1)
        self.assertAlmostEqual(entry["tps"], 258 / 2.2, places=1)

    def test_history_is_populated_for_message_based_measurements(self) -> None:
        """卡片走势条不能只依赖滚动采样点，否则 Claude 永远是空的。"""

        tracker = SpeedTracker()
        self._write(
            self._request("r1", "2026-10-19T03:31:52.000Z"),
            self._message("m1", "2026-10-19T03:31:54.000Z", 200, "r1"),
            self._request("r2", "2026-10-19T03:32:00.000Z"),
            self._message("m2", "2026-10-19T03:32:02.000Z", 400, "r2"),
        )
        tracker.sample_sessions([self.session], now=1_000.0)
        entry = tracker.snapshot(now=1_000.0)["accounts"]["claude-account"]
        self.assertEqual(len(entry["history"]), 2)
        self.assertAlmostEqual(entry["history"][-1], 200.0, places=1)

    def test_old_transcripts_without_requests_fall_back_to_the_message_gap(self) -> None:
        tracker = SpeedTracker()
        self._write(
            self._message("m1", "2026-10-19T03:31:50.000Z", 100, None),
            self._message("m2", "2026-10-19T03:31:54.000Z", 200, None),
        )
        tracker.sample_sessions([self.session], now=1_000.0)
        entry = tracker.snapshot(now=1_000.0)["accounts"]["claude-account"]
        self.assertAlmostEqual(entry["tps"], 50.0, places=1)

    def test_recent_measurement_counts_as_live_for_a_minute(self) -> None:
        """两次消息之间常隔着工具执行，不能 12 秒就变灰。"""

        tracker = SpeedTracker()
        self._write(
            self._request("r1", "2026-10-19T03:31:52.000Z"),
            self._message("m1", "2026-10-19T03:31:54.000Z", 200, "r1"),
        )
        tracker.sample_sessions([self.session], now=1_000.0)
        measured_at = tracker._last_live["claude-account"][0]
        self.assertTrue(
            tracker.snapshot(now=measured_at + 30)["accounts"]["claude-account"]["live"]
        )
        self.assertFalse(
            tracker.snapshot(now=measured_at + 90)["accounts"]["claude-account"]["live"]
        )

    def test_backfill_reads_requests_before_messages(self) -> None:
        """首见回读时先登记 api-request，尾部那条消息才能用上请求窗口。"""

        self._write(
            self._request("r1", "2026-10-19T03:31:52.000Z"),
            self._message("m1", "2026-10-19T03:31:54.000Z", 200, "r1"),
        )
        tracker = SpeedTracker()
        tracker.sample_sessions([self.session], now=1_000.0)
        entry = tracker.snapshot(now=1_000.0)["accounts"]["claude-account"]
        self.assertAlmostEqual(entry["tps"], 100.0, places=1)
        self.assertEqual(len(entry["history"]), 1)


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

    def test_last_rate_survives_after_the_window(self) -> None:
        """采样账号停下来后仍要保留最近一次速率（否则只有正在出字时才看得到）。"""

        tracker = SpeedTracker(window_seconds=4.0)
        tracker.sample_sessions([self.session], now=1000.0)
        self._append(50)
        tracker.sample_sessions([self.session], now=1001.0)
        self._append(50)
        tracker.sample_sessions([self.session], now=1003.0)
        snapshot = tracker.snapshot(now=1600.0)
        entry = snapshot["accounts"]["claude-work"]
        self.assertFalse(entry["live"])
        self.assertGreaterEqual(entry["age_seconds"], 500)
        self.assertAlmostEqual(entry["tps"], 25.0, places=1)

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
