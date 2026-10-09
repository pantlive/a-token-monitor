"""告警上传原因分析：模型请求提取、主因判定与入口函数。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from a_token_monitor.alert_context import (
    AlertContextRoots,
    default_alert_context_roots,
    load_alert_context,
)
from a_token_monitor.alert_context.common import _Extraction
from a_token_monitor.alert_context.diagnosis import diagnose_upload
from a_token_monitor.alerts import StoredAlert


_MIB = 1024 * 1024


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _alert(
    *,
    product: str = "claude",
    cwd: str = "/home/dev/app",
    peak: int = 8 * _MIB,
) -> StoredAlert:
    return StoredAlert(
        id=1,
        level="warn",
        kind="burst",
        product=product,
        pid=11,
        process_key=f"{product}:11:10",
        command=product,
        cwd=cwd,
        remote="203.0.113.10:443",
        bytes=peak,
        peak_bytes=peak,
        window_seconds=15.0,
        message="突发外发",
        first_seen_at=1_000_000.0,
        last_seen_at=1_000_030.0,
        count=1,
        acknowledged_at=None,
    )


def _write_lines(path: Path, lines: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )
    os.utime(path, (1_000_040.0, 1_000_040.0))


def _claude_assistant(at: float, request_id: str, tokens: int) -> dict[str, object]:
    return {
        "type": "assistant",
        "timestamp": _iso(at),
        "requestId": request_id,
        "cwd": "/home/dev/app",
        "message": {
            "id": f"msg-{request_id}",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": tokens - 1_010,
                "cache_creation_input_tokens": 1_000,
                "output_tokens": 50,
            },
        },
    }


class ClaudeRequestTests(unittest.TestCase):
    """Claude Code：按请求去重，上下文 = 未缓存 + 读缓存 + 写缓存。"""

    def test_long_context_resend_is_the_main_cause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            _write_lines(
                projects / "-home-dev-app" / "s1.jsonl",
                [
                    # 窗口外的请求不计入。
                    _claude_assistant(1_000_000.0 - 3_600, "req-old", 500_000),
                    {
                        "type": "user",
                        "timestamp": _iso(1_000_005.0),
                        "cwd": "/home/dev/app",
                        "message": {"role": "user", "content": "继续"},
                    },
                    _claude_assistant(1_000_010.0, "req-1", 900_000),
                    # 同一请求拆成多条记录，只算一次。
                    _claude_assistant(1_000_011.0, "req-1", 900_000),
                    _claude_assistant(1_000_020.0, "req-2", 950_000),
                ],
            )
            context = load_alert_context(
                _alert(), AlertContextRoots(claude_projects=(projects,))
            )

        diagnosis = context["diagnosis"]
        self.assertEqual(diagnosis["cause"], "context_resend")
        self.assertEqual(diagnosis["requests"], 2)
        self.assertEqual(diagnosis["context_tokens"], 950_000)
        self.assertEqual(diagnosis["per_request_bytes"], 950_000 * 4)
        self.assertEqual(diagnosis["resend_bytes"], (900_000 + 950_000) * 4)
        self.assertEqual(diagnosis["requests_to_peak"], 3)


class CodexRequestTests(unittest.TestCase):
    """Codex：token_count 的 last_token_usage 即单次请求，重复写出的用量去重。"""

    @staticmethod
    def _token_count(at: float, total: int, context: int) -> dict[str, object]:
        return {
            "timestamp": _iso(at),
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"total_tokens": total},
                    "last_token_usage": {
                        "input_tokens": context,
                        "cached_input_tokens": context - 500,
                    },
                },
            },
        }

    def test_large_new_tool_output_is_the_main_cause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            _write_lines(
                sessions / "1970" / "01" / "12" / "rollout-1970-01-12T21-46-40-abc.jsonl",
                [
                    {
                        "timestamp": _iso(1_000_000.0 - 7_200),
                        "type": "session_meta",
                        "payload": {"id": "abc", "cwd": "/home/dev/app"},
                    },
                    {
                        "timestamp": _iso(1_000_005.0),
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "name": "view_image",
                            "input": "{}",
                            "call_id": "c1",
                        },
                    },
                    {
                        "timestamp": _iso(1_000_006.0),
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "call_id": "c1",
                            "output": "x" * (6 * _MIB),
                        },
                    },
                    self._token_count(1_000_010.0, 200_000, 160_000),
                    # 只更新额度时重复写出的同一份用量。
                    self._token_count(1_000_011.0, 200_000, 160_000),
                    self._token_count(1_000_020.0, 380_000, 170_000),
                ],
            )
            context = load_alert_context(
                _alert(product="codex"), AlertContextRoots(codex_sessions=(sessions,))
            )

        diagnosis = context["diagnosis"]
        self.assertEqual(diagnosis["cause"], "new_content")
        self.assertEqual(diagnosis["requests"], 2)
        self.assertEqual(diagnosis["context_tokens"], 170_000)
        self.assertGreaterEqual(diagnosis["new_bytes"], 6 * _MIB)
        self.assertEqual(diagnosis["largest"][0]["kind"], "tool_output")
        self.assertEqual(diagnosis["largest"][0]["label"], "view_image")


class DiagnoseUploadTests(unittest.TestCase):
    """日志解释不了观测流量时，区分「工具在发网络数据」和「原因不明」。"""

    def test_network_tool_calls_explain_unlogged_traffic(self) -> None:
        events = (
            {
                "t": 1_000_010.0,
                "kind": "tool",
                "label": "Bash",
                "size": 120,
                "activities": [{"summary": "发送网络请求", "basis": "parameters"}],
            },
        )
        diagnosis = diagnose_upload(
            _alert(peak=20 * _MIB), _Extraction(events, False, 120, 0), events
        )
        self.assertEqual(diagnosis["cause"], "tool_network")
        self.assertEqual(diagnosis["network_activities"], ["发送网络请求"])

    def test_user_text_mentioning_upload_is_not_network_evidence(self) -> None:
        events = (
            {
                "t": 1_000_010.0,
                "kind": "user",
                "label": "",
                "size": 40,
                "activities": [{"summary": "发起文件上传", "basis": "record"}],
            },
        )
        diagnosis = diagnose_upload(
            _alert(peak=20 * _MIB), _Extraction(events, False, 40, 0), events
        )
        self.assertEqual(diagnosis["cause"], "unexplained")
        self.assertEqual(diagnosis["network_activities"], [])
        self.assertIsNone(diagnosis["requests_to_peak"])


class DefaultRootsTests(unittest.TestCase):
    """命令行入口能解析默认目录（相对导入曾指错包）。"""

    def test_default_roots_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            sessions.mkdir()
            with (
                mock.patch(
                    "a_token_monitor.discovery.default_session_root",
                    return_value=sessions,
                ),
                mock.patch(
                    "a_token_monitor.providers.resolve_provider_homes",
                    return_value={},
                ),
            ):
                roots = default_alert_context_roots()

        self.assertEqual(roots.codex_sessions, (sessions,))


if __name__ == "__main__":
    unittest.main()
