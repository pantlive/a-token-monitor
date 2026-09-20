"""事件解析单元测试。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from codex_reset_monitor.events import parse_event_line


class EventParserTests(unittest.TestCase):
    """验证 session、额度失败和 reset 时间的识别。"""

    def test_extracts_session_id_from_thread_started(self) -> None:
        observation = parse_event_line(
            '{"type":"thread.started","thread_id":"session-123"}'
        )

        self.assertEqual(observation.session_id, "session-123")
        self.assertFalse(observation.quota_exhausted)

    def test_detects_primary_rate_limit_and_prefers_its_reset(self) -> None:
        now = 1_700_000_000.0
        line = (
            '{"type":"turn.failed",'
            '"rate_limit_reached_type":"primary",'
            '"error":{"code":"rate_limit_exceeded",'
            '"rate_limits":{"primary":{"resets_at":1700007200},'
            '"secondary":{"resets_at":1700864000}}}}'
        )

        observation = parse_event_line(line, now=now)

        self.assertTrue(observation.quota_exhausted)
        self.assertEqual(observation.reset_at, 1_700_007_200.0)

    def test_detects_relative_reset_seconds(self) -> None:
        now = 1_700_000_000.0
        line = (
            '{"type":"turn.failed","error":{"code":"usage_limit_reached",'
            '"reset_after_seconds":120}}'
        )

        observation = parse_event_line(line, now=now)

        self.assertTrue(observation.quota_exhausted)
        self.assertEqual(observation.reset_at, now + 120)

    def test_keeps_rate_limit_window_metadata_without_labeling_it(self) -> None:
        line = (
            '{"type":"token_count","rate_limits":'
            '{"primary":{"used_percent":42,"window_minutes":300,'
            '"resets_at":1700000300}}}'
        )

        observation = parse_event_line(line, now=1_700_000_000.0)

        self.assertFalse(observation.quota_exhausted)
        self.assertEqual(len(observation.rate_limits), 1)
        self.assertEqual(observation.rate_limits[0].name, "primary")
        self.assertEqual(observation.rate_limits[0].used_percent, 42.0)
        self.assertEqual(observation.rate_limits[0].window_minutes, 300.0)
        self.assertEqual(observation.rate_limits[0].reset_at, 1_700_000_300.0)

    def test_detects_plain_text_reset_duration(self) -> None:
        observation = parse_event_line(
            "You've hit your usage limit. Reset in 2h 10m",
            now=1_000.0,
        )

        self.assertTrue(observation.quota_exhausted)
        self.assertEqual(observation.reset_at, 8_800.0)

    def test_detects_usage_limit_inside_task_complete(self) -> None:
        local = datetime.now().astimezone().tzinfo
        event_time = datetime(2026, 8, 28, 17, 28, tzinfo=local)
        expected = datetime(2026, 8, 28, 19, 3, tzinfo=local)
        line = json.dumps(
            {
                "timestamp": event_time.isoformat(),
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "error": {
                        "message": (
                            "You've hit your usage limit. Upgrade to Pro "
                            "or try again at 7:03 PM."
                        ),
                        "codex_error_info": "usage_limit_exceeded",
                    },
                },
            }
        )

        observation = parse_event_line(line, now=event_time.timestamp())

        self.assertTrue(observation.quota_exhausted)
        self.assertEqual(observation.event_type, "task_complete")
        self.assertEqual(int(observation.reset_at or 0), int(expected.timestamp()))

    def test_does_not_classify_network_failure_as_quota_failure(self) -> None:
        observation = parse_event_line(
            '{"type":"turn.failed","error":{"message":"network timeout"}}'
        )

        self.assertFalse(observation.quota_exhausted)
        self.assertIsNone(observation.reset_at)


if __name__ == "__main__":
    unittest.main()
