"""告警 → 会话上下文：按 cwd + 时间窗定位会话文件并提取事件明细。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from a_token_monitor.alert_context import _MAX_EVENTS, AlertContextRoots, load_alert_context
from a_token_monitor.alerts import StoredAlert


def _visible_alert_context(alert: StoredAlert, roots: AlertContextRoots) -> dict[str, object]:
    """旧提取用例显式打开摘要；默认隐私行为由独立用例验证。"""

    return load_alert_context(alert, roots, include_content=True)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _alert(
    *,
    product: str = "codex",
    cwd: str | None = "/home/dev/project",
    first: float = 1_000_000.0,
    last: float = 1_000_030.0,
) -> StoredAlert:
    return StoredAlert(
        id=1,
        level="danger",
        kind="burst",
        product=product,
        pid=11,
        process_key=f"{product}:11:10",
        command=product,
        cwd=cwd,
        remote="203.0.113.10:443",
        bytes=40 * 1024 * 1024,
        peak_bytes=40 * 1024 * 1024,
        window_seconds=15.0,
        message="突发外发",
        first_seen_at=first,
        last_seen_at=last,
        count=1,
        acknowledged_at=None,
    )


def _write_codex_session(
    root: Path,
    *,
    cwd: str = "/home/dev/project",
    session_id: str = "019ed409-4ff8-7083-98a5-2502125735ce",
    mtime: float | None = None,
    started: float = 1_000_000.0,
) -> Path:
    # 中文注释：rollout 文件名内嵌会话开始的本地时间，候选过滤会用到。
    name_stamp = datetime.fromtimestamp(started).strftime("%Y-%m-%dT%H-%M-%S")
    day = datetime.fromtimestamp(started)
    path = (
        root
        / day.strftime("%Y")
        / day.strftime("%m")
        / day.strftime("%d")
        / f"rollout-{name_stamp}-{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    inside = 1_000_010.0
    outside = 1_000_000.0 - 3600
    lines = [
        {"timestamp": _iso(1_000_000.0 - 7200), "type": "session_meta", "payload": {"id": session_id, "cwd": cwd}},
        # 窗口外的事件必须被过滤掉。
        {"timestamp": _iso(outside), "type": "event_msg", "payload": {"type": "user_message", "message": "窗口外的消息"}},
        {"timestamp": _iso(inside), "type": "event_msg", "payload": {"type": "user_message", "message": "帮我把这个 2MB 的日志分析一下"}},
        {
            "timestamp": _iso(inside + 1),
            "type": "response_item",
            "payload": {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "cat big.log"}), "call_id": "c1"},
        },
        {
            "timestamp": _iso(inside + 2),
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "c1", "output": "x" * 2048},
        },
        {
            "timestamp": _iso(inside + 3),
            "type": "response_item",
            "payload": {"type": "web_search_call", "status": "completed", "action": {"type": "search", "query": "token monitor"}},
        },
    ]
    path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n", encoding="utf-8")
    stamp = mtime if mtime is not None else 1_000_040.0
    os.utime(path, (stamp, stamp))
    return path


class AlertContextCodexTests(unittest.TestCase):
    def test_finds_session_and_extracts_window_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = _write_codex_session(root / "sessions")
            context = _visible_alert_context(
                _alert(),
                AlertContextRoots(codex_sessions=(root / "sessions",)),
            )
            self.assertTrue(context["found"])
            self.assertEqual(context["session"]["path"], str(session))
            self.assertEqual(context["session"]["session_id"], "019ed409-4ff8-7083-98a5-2502125735ce")
            kinds = [event["kind"] for event in context["events"]]
            self.assertEqual(kinds, ["user", "tool", "tool_output", "search"])
            self.assertNotIn("窗口外的消息", json.dumps(context["events"], ensure_ascii=False))
            tool = context["events"][1]
            self.assertEqual(tool["label"], "exec_command")
            self.assertEqual(tool["detail"], "cat big.log")
            totals = context["totals"]
            self.assertEqual(totals["events"], 4)
            self.assertGreaterEqual(totals["output_bytes"], 2048)
            self.assertGreater(totals["input_bytes"], 0)

    def test_cwd_mismatch_finds_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_codex_session(root / "sessions", cwd="/home/dev/other")
            context = _visible_alert_context(
                _alert(cwd="/home/dev/project"),
                AlertContextRoots(codex_sessions=(root / "sessions",)),
            )
            self.assertFalse(context["found"])
            self.assertEqual(context["reason"], "no_session")

    def test_cwd_prefix_counts_as_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_codex_session(root / "sessions", cwd="/home/dev/project")
            context = _visible_alert_context(
                _alert(cwd="/home/dev/project/sub"),
                AlertContextRoots(codex_sessions=(root / "sessions",)),
            )
            self.assertTrue(context["found"])

    def test_stale_mtime_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 文件在窗口开始前 5 分钟以上没有写入：不是告警时的活动会话。
            _write_codex_session(root / "sessions", mtime=1_000_000.0 - 3600)
            context = _visible_alert_context(
                _alert(),
                AlertContextRoots(codex_sessions=(root / "sessions",)),
            )
            self.assertFalse(context["found"])
            self.assertEqual(context["reason"], "no_session")

    def test_unsupported_product(self) -> None:
        context = _visible_alert_context(_alert(product="grok"), AlertContextRoots())
        self.assertFalse(context["found"])
        self.assertEqual(context["reason"], "unsupported_product")

    def test_fallback_shows_recent_activity_when_window_empty(self) -> None:
        """窗口内没有事件时（如 MCP 子进程外传），退回展示告警前最近的活动。"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 事件停在 1_000_013，告警发生在一小时后；mtime 拨到告警时刻附近
            # 让候选通过 mtime 预筛。
            alert = _alert(first=1_000_000.0 + 3600, last=1_000_000.0 + 3600)
            _write_codex_session(root / "sessions", mtime=1_000_000.0 + 3500)
            context = _visible_alert_context(
                alert,
                AlertContextRoots(codex_sessions=(root / "sessions",)),
            )
            self.assertTrue(context["found"])
            self.assertTrue(context["fallback"])
            self.assertEqual(
                [event["kind"] for event in context["events"]],
                ["user", "user", "tool", "tool_output", "search"],
            )
            self.assertGreater(context["totals"]["output_bytes"], 0)

    def test_event_cap_marks_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = _write_codex_session(root / "sessions")
            extra = [
                {"timestamp": _iso(1_000_011.0 + index), "type": "event_msg", "payload": {"type": "user_message", "message": f"消息 {index}"}}
                for index in range(_MAX_EVENTS + 10)
            ]
            with session.open("a", encoding="utf-8") as handle:
                for line in extra:
                    handle.write(json.dumps(line, ensure_ascii=False) + "\n")
            context = _visible_alert_context(
                _alert(last=1_000_100.0),
                AlertContextRoots(codex_sessions=(root / "sessions",)),
            )
            self.assertTrue(context["found"])
            self.assertTrue(context["truncated"])
            self.assertEqual(len(context["events"]), _MAX_EVENTS)


class AlertContextKimiTests(unittest.TestCase):
    def _write_kimi_session(self, root: Path, cwd: str, *, mtime: float) -> Path:
        session_dir = root / "wd_project_abc" / "session_11111111-2222-3333-4444-555555555555"
        agents = session_dir / "agents" / "main"
        agents.mkdir(parents=True)
        (session_dir / "state.json").write_text(json.dumps({"cwd": cwd}), encoding="utf-8")

        def ms(ts: float) -> int:
            return int(ts * 1000)

        inside = 1_000_010.0
        lines = [
            {"type": "context.append_message", "agentId": "main", "time": ms(inside),
             "message": {"role": "user", "content": [{"type": "text", "text": "看下这个大文件"}], "origin": {"kind": "user"}}},
            {"type": "context.append_loop_event", "agentId": "main", "time": ms(inside + 1),
             "event": {"type": "tool.call", "name": "Read", "args": {"path": "/home/dev/project/big.txt"}}},
            {"type": "context.append_loop_event", "agentId": "main", "time": ms(inside + 2),
             "event": {"type": "tool.result", "result": {"output": "z" * 4096}}},
            # 非用户来源的消息（如系统注入）不应算作用户输入。
            {"type": "context.append_message", "agentId": "main", "time": ms(inside + 3),
             "message": {"role": "user", "content": [{"type": "text", "text": "系统注入"}], "origin": {"kind": "system"}}},
            # 窗口外的事件应被过滤。
            {"type": "context.append_message", "agentId": "main", "time": ms(inside - 7200),
             "message": {"role": "user", "content": [{"type": "text", "text": "窗口外的消息"}], "origin": {"kind": "user"}}},
        ]
        wire = agents / "wire.jsonl"
        wire.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n", encoding="utf-8")
        os.utime(wire, (mtime, mtime))
        return session_dir

    def test_content_is_hidden_unless_explicitly_enabled(self) -> None:
        """默认响应不应携带用户消息、命令或文件参数摘要。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_codex_session(root / "sessions")
            roots = AlertContextRoots(codex_sessions=(root / "sessions",))
            hidden = load_alert_context(_alert(), roots)
            visible = load_alert_context(_alert(), roots, include_content=True)
            self.assertTrue(hidden["found"])
            self.assertFalse(hidden["content_enabled"])
            self.assertTrue(all(item["detail"] == "" for item in hidden["events"]))
            self.assertEqual(hidden["totals"], visible["totals"])
            self.assertIn("cat big.log", [item["detail"] for item in visible["events"]])

    def test_kimi_wire_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = self._write_kimi_session(root, "/home/dev/project", mtime=1_000_040.0)
            context = _visible_alert_context(
                _alert(product="kimi", cwd="/home/dev/project"),
                AlertContextRoots(kimi_sessions=(root,)),
            )
            self.assertTrue(context["found"])
            self.assertEqual(context["session"]["path"], str(session_dir))
            kinds = [event["kind"] for event in context["events"]]
            self.assertEqual(kinds, ["user", "tool", "tool_output"])
            self.assertEqual(context["events"][1]["label"], "Read")
            self.assertEqual(context["events"][1]["detail"], "/home/dev/project/big.txt")
            dumped = json.dumps(context["events"], ensure_ascii=False)
            self.assertNotIn("系统注入", dumped)
            self.assertNotIn("窗口外的消息", dumped)
            self.assertGreaterEqual(context["totals"]["output_bytes"], 4096)

    def test_kimi_cwd_mismatch_finds_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_kimi_session(root, "/home/dev/other", mtime=1_000_040.0)
            context = _visible_alert_context(
                _alert(product="kimi", cwd="/home/dev/project"),
                AlertContextRoots(kimi_sessions=(root,)),
            )
            self.assertFalse(context["found"])
            self.assertEqual(context["reason"], "no_session")


class AlertContextClaudeTests(unittest.TestCase):
    def test_claude_transcript_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            project_dir = projects / "-home-dev-app"
            project_dir.mkdir(parents=True)
            session = project_dir / "abc123.jsonl"
            inside = 1_000_010.0
            lines = [
                {
                    "type": "user",
                    "timestamp": _iso(inside),
                    "cwd": "/home/dev/app",
                    "message": {"role": "user", "content": "读取那个大文件"},
                },
                {
                    "type": "assistant",
                    "timestamp": _iso(inside + 1),
                    "cwd": "/home/dev/app",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "/home/dev/app/big.txt"}},
                        ],
                    },
                },
                {
                    "type": "user",
                    "timestamp": _iso(inside + 2),
                    "cwd": "/home/dev/app",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "content": "y" * 4096}],
                    },
                },
                {
                    "type": "assistant",
                    "timestamp": _iso(inside + 3),
                    "isSidechain": True,
                    "cwd": "/home/dev/app",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "子代理不该出现"}}],
                    },
                },
            ]
            session.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n", encoding="utf-8")
            stamp = 1_000_040.0
            os.utime(session, (stamp, stamp))
            context = _visible_alert_context(
                _alert(product="claude", cwd="/home/dev/app"),
                AlertContextRoots(claude_projects=(projects,)),
            )
            self.assertTrue(context["found"])
            kinds = [event["kind"] for event in context["events"]]
            self.assertEqual(kinds, ["user", "tool", "tool_output"])
            self.assertEqual(context["events"][1]["label"], "Read")
            self.assertEqual(context["events"][1]["detail"], "/home/dev/app/big.txt")
            self.assertNotIn("子代理不该出现", json.dumps(context["events"], ensure_ascii=False))
            self.assertGreaterEqual(context["totals"]["output_bytes"], 4096)

    def test_claude_slug_mismatch_finds_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            (projects / "-home-dev-other").mkdir(parents=True)
            context = _visible_alert_context(
                _alert(product="claude", cwd="/home/dev/app"),
                AlertContextRoots(claude_projects=(projects,)),
            )
            self.assertFalse(context["found"])
            self.assertEqual(context["reason"], "no_session")


if __name__ == "__main__":
    unittest.main()
