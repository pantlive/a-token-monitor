"""统一会话模型、序列化视图和用量 enrichment 测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from token_monitor.multi_models import (
    DetectionConfidence,
    SessionStatus,
    TrackedSession,
    display_session_error,
    session_view,
)
from token_monitor.registry import MultiSessionRegistry
from token_monitor.usage import (
    SessionSwitchThresholds,
    UsageAggregator,
    enrich_session_views,
)


def _session(**overrides: object) -> TrackedSession:
    """构造一个用于视图测试的统一会话。"""

    fields: dict[str, object] = {
        "thread_id": "grok:session-1",
        "session_id": "session-1",
        "jsonl_path": (
            "/home/dev/.grok/sessions/%2Fhome%2Fdev%2Fproj/session-1/"
            "chat_history.jsonl"
        ),
        "cwd": "/home/dev/proj",
        "source": "grok-cli",
        "status": SessionStatus.RUNNING,
        "confidence": DetectionConfidence.OPEN_FILE,
        "first_seen_at": 1_000.0,
        "last_seen_at": 2_000.0,
        "pids": (42,),
        "last_event_at": 1_500.0,
        "last_event_type": "updates.jsonl",
        "product": "grok",
        "model": "grok-4.6",
        "project": "/home/dev/proj",
    }
    fields.update(overrides)
    return TrackedSession(**fields)  # type: ignore[arg-type]


class UnifiedSessionModelTests(unittest.TestCase):
    """验证统一字段、属性别名和序列化视图。"""

    def test_unified_fields_and_aliases(self) -> None:
        session = _session()

        self.assertEqual(session.product, "grok")
        self.assertEqual(session.model, "grok-4.6")
        self.assertEqual(session.resolved_project, "/home/dev/proj")
        self.assertEqual(session.started_at, 1_000.0)
        # 最后活动时间优先取最近事件
        self.assertEqual(session.last_activity_at, 1_500.0)
        self.assertEqual(_session(last_event_at=None).last_activity_at, 2_000.0)
        # 适配器没填 project 时退回 cwd
        self.assertEqual(_session(project=None).resolved_project, "/home/dev/proj")
        # discovery 阶段 token 未知
        self.assertEqual(
            (session.tokens, session.context_tokens, session.turns),
            (0, 0, 0),
        )

    def test_session_view_uses_one_shape_for_all_providers(self) -> None:
        session = _session()

        view = session_view(session, "grok-user", profile_name="grok")

        for field_name in (
            "account",
            "account_id",
            "profile_name",
            "codex_home",
            "product",
            "project",
            "model",
            "status",
            "tokens",
            "context_tokens",
            "turns",
            "started_at",
            "last_activity_at",
            "jsonl_path",
        ):
            self.assertIn(field_name, view)
        self.assertEqual(view["product"], "grok")
        self.assertEqual(view["model"], "grok-4.6")
        self.assertEqual(view["project"], "/home/dev/proj")
        self.assertEqual(view["started_at"], 1_000.0)
        self.assertEqual(view["last_activity_at"], 1_500.0)
        self.assertEqual(view["account"], "grok-user")
        self.assertEqual(view["status"], "running")
        self.assertTrue(view["active"])
        self.assertTrue(view["process_backed"])
        # 视图不包含 resume 调度等内部字段
        for internal in (
            "auto_resume",
            "next_attempt_at",
            "resume_attempts",
            "metadata",
        ):
            self.assertNotIn(internal, view)

    def test_session_view_never_exposes_raw_resume_errors(self) -> None:
        session = _session(
            last_error="额度中断后不自动恢复，历史会话状态",
            status=SessionStatus.LIMIT_BLOCKED,
        )

        view = session_view(session, "codex-user")

        self.assertEqual(view["last_error"], "额度限制事件")
        self.assertEqual(
            display_session_error("读取 JSONL 时磁盘满了"),
            "读取 JSONL 时磁盘满了",
        )
        self.assertIsNone(display_session_error(None))

    def test_session_view_prefers_model_product_override(self) -> None:
        session = _session(product="codex")

        self.assertEqual(session_view(session)["product"], "codex")
        self.assertEqual(
            session_view(session, product="command-code")["product"],
            "command-code",
        )


class SessionEnrichmentTests(unittest.TestCase):
    """验证统一用量 enrichment 回填 token 字段并生成提醒。"""

    def _aggregator(self, root: Path) -> tuple[UsageAggregator, Path]:
        home = root / ".codex"
        session = (
            home
            / "sessions"
            / "2026"
            / "08"
            / "27"
            / "rollout-2026-08-27T01-00-00-77777777-7777-4777-8777-777777777777.jsonl"
        )
        session.parent.mkdir(parents=True, exist_ok=True)
        events = [
            {
                "timestamp": "2026-08-27T01:00:00Z",
                "type": "session_meta",
                "payload": {"cwd": "/home/dev/theta"},
            },
            {
                "timestamp": "2026-08-27T02:00:00Z",
                "type": "event_msg",
                "payload": {
                    "thread_settings": {"model": "claude-sonnet-4-5"},
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 900_000,
                            "total_tokens": 900_000,
                        }
                    },
                },
            },
            {
                "timestamp": "2026-08-27T03:00:00Z",
                "type": "event_msg",
                "payload": {
                    "thread_settings": {"model": "claude-sonnet-4-5"},
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 1_100_000,
                            "total_tokens": 1_100_000,
                        }
                    },
                },
            },
        ]
        session.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
        aggregator = UsageAggregator(
            discovery_interval=0.01,
            refresh_interval=0.01,
            cache_path=root / "state" / "usage-index.sqlite3",
            grok_homes=(),
            kimi_homes=(),
            dsh_homes=(),
            claude_homes=(),
        )
        aggregator.snapshot(
            {"codex": MultiSessionRegistry(root / "state")},
            account_metadata={
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(home),
                }
            },
            now=_timestamp("2026-08-27T12:00:00Z"),
        )
        return aggregator, session

    def test_enrich_fills_tokens_and_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            aggregator, path = self._aggregator(Path(temporary_directory))
            view = session_view(
                _session(
                    thread_id="codex:thread-1",
                    session_id="77777777-7777-4777-8777-777777777777",
                    jsonl_path=str(path),
                    source="process",
                    product="codex",
                    model=None,
                ),
                "codex-user",
            )

            views, reminders = enrich_session_views(
                [view],
                aggregator,
                SessionSwitchThresholds(turn_warn=1, context_warn_tokens=1_000_000),
            )

        enriched = views[0]
        # 累计计数 900k → 1.1M，两条增量合计 1.1M，最近一条上下文 200k
        self.assertEqual(enriched["tokens"], 1_100_000)
        self.assertEqual(enriched["context_tokens"], 200_000)
        self.assertEqual(enriched["turns"], 2)
        # discovery 阶段没拿到模型时由用量索引回填
        self.assertEqual(enriched["model"], "claude-sonnet-4-5")
        self.assertIsNotNone(enriched["usage"]["reminder"])
        self.assertEqual(len(reminders), 1)
        reminder = reminders[0]
        self.assertEqual(reminder["thread_id"], "codex:thread-1")
        self.assertEqual(reminder["account"], "codex-user")
        self.assertEqual(reminder["product"], "codex")
        self.assertIn("已进行 2 轮", reminder["message"])
        self.assertEqual(reminder["project"], "/home/dev/proj")

    def test_enrich_without_aggregator_returns_views_unchanged(self) -> None:
        view = session_view(_session(), "grok-user")

        views, reminders = enrich_session_views([view], None)

        self.assertEqual(views[0]["tokens"], 0)
        self.assertNotIn("usage", views[0])
        self.assertEqual(reminders, [])

    def test_enrich_skips_missing_paths(self) -> None:
        view = session_view(_session(jsonl_path=None), "kimi-user")

        views, reminders = enrich_session_views([view], UsageAggregator())

        self.assertEqual(reminders, [])
        self.assertEqual(views[0].get("usage"), None)


def _timestamp(value: str) -> float:
    """把 ISO8601 时间转成 Unix 秒。"""

    return (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


if __name__ == "__main__":
    unittest.main()
