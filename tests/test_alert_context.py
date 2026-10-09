"""告警 → 会话上下文：按 cwd + 时间窗定位会话文件并提取事件明细。"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from a_token_monitor.alert_context import AlertContextRoots, load_alert_context
from a_token_monitor.alert_context.claude import _extract_claude
from a_token_monitor.alert_context.codex import _codex_tool_label, _extract_codex
from a_token_monitor.alert_context.common import (
    _MAX_EVENTS,
    _cwd_matches,
    _iso_record_ts,
    _suffix_offset,
)
from a_token_monitor.alert_context.kimi import _extract_kimi
from a_token_monitor.alerts import StoredAlert
from a_token_monitor.local_time import to_local


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
    name_stamp = to_local(started).strftime("%Y-%m-%dT%H-%M-%S")
    day = to_local(started)
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
            self.assertEqual(context["events"][2]["label"], "exec_command")
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

    def test_backslash_separators_match_like_slashes(self) -> None:
        # 中文注释：Windows 上会话头的 cwd 经 Path 读出后是反斜杠写法。
        self.assertTrue(_cwd_matches("/home/dev/project", "\\home\\dev\\project"))
        self.assertTrue(_cwd_matches("C:\\work\\proj\\src", "C:\\work\\proj"))
        self.assertTrue(_cwd_matches("C:/work/proj/src", "C:\\work\\proj\\"))
        self.assertFalse(_cwd_matches("C:\\work\\project", "C:\\work\\proj"))

    def test_parent_alert_cwd_does_not_match_child_session(self) -> None:
        self.assertFalse(_cwd_matches("/home/dev", ""))
        self.assertFalse(_cwd_matches("/home/dev", "/home/dev/project"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_codex_session(root / "sessions", cwd="/home/dev/project")
            context = _visible_alert_context(
                _alert(cwd="/home/dev"),
                AlertContextRoots(codex_sessions=(root / "sessions",)),
            )
            self.assertFalse(context["found"])
            self.assertEqual(context["reason"], "no_session")

    def test_suffix_offset_keeps_nearby_tool_calls(self) -> None:
        moment = 1_789_000_000.0
        window_start = moment - 120
        pad = "x" * 1000
        lines: list[dict[str, object]] = []
        for index in range(300):
            lines.append(
                {
                    "timestamp": _iso(window_start - 10_000 - index),
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": f"前缀{index}{pad}"},
                }
            )
        lines.append(
            {
                "timestamp": _iso(window_start - 10),
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "cat /tmp/main.py"}),
                    "call_id": "near",
                },
            }
        )
        lines.append(
            {
                "timestamp": _iso(moment),
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "near",
                    "output": "ok",
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text(
                "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
                encoding="utf-8",
            )
            offset = _suffix_offset(path, window_start, _iso_record_ts)
            self.assertGreater(offset, 0)
            self.assertLess(offset, path.stat().st_size / 2)
            result = _extract_codex(path, window_start, moment + 30)
            self.assertIsNotNone(result.events)
            outputs = [
                event for event in result.events or () if event["kind"] == "tool_output"
            ]
            self.assertEqual(outputs[0]["label"], "exec_command")
            self.assertNotIn("前缀0", json.dumps(outputs, ensure_ascii=False))

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
        context = _visible_alert_context(
            _alert(product="unknown"), AlertContextRoots()
        )
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
                            {"type": "tool_use", "id": "read-1", "name": "Read", "input": {"file_path": "/home/dev/app/big.txt"}},
                        ],
                    },
                },
                {
                    "type": "user",
                    "timestamp": _iso(inside + 2),
                    "cwd": "/home/dev/app",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "read-1", "content": "y" * 4096}],
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
            self.assertEqual(context["events"][2]["label"], "Read")
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


class AlertContextToolNameTests(unittest.TestCase):
    """验证真实 Codex 包装器格式、输出关联与默认名称展示。"""

    def test_wrapper_names_skip_literals_and_comments(self) -> None:
        code = """
// tools.not_a_tool({})
/* await tools.also_not_a_tool({}) */
const example = "tools.fake({})";
const template = `tools.fake_template({})`;
await Promise.all([
  tools.exec_command({cmd: "echo tools.fake_command({})"}),
  tools["web__run"]({search_query: []}),
]);
await tools.exec_command({cmd: "pwd"});
"""
        self.assertEqual(
            _codex_tool_label("exec", code), "exec → exec_command · web__run"
        )

    def test_wrapper_falls_back_to_recorded_name(self) -> None:
        self.assertEqual(_codex_tool_label("exec", "text('hello');"), "exec")
        self.assertEqual(_codex_tool_label("Read", "tools.exec_command({});"), "Read")
        self.assertEqual(
            _codex_tool_label("functions.exec", 'await tools.apply_patch("patch");'),
            "functions.exec → apply_patch",
        )

    def test_wrapped_names_visible_with_content_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _write_codex_session(root / "sessions")
            # 中文注释：追加生产日志使用的 custom_tool_call/input 包装器格式。
            with path.open("a", encoding="utf-8") as stream:
                for kind, extra in (
                    (
                        "custom_tool_call",
                        {
                            "name": "exec",
                            "input": 'await tools.exec_command({cmd: "private-command"});',
                        },
                    ),
                    ("custom_tool_call_output", {"output": "private-output"}),
                ):
                    stream.write(
                        json.dumps(
                            {
                                "timestamp": _iso(1_000_020.0),
                                "type": "response_item",
                                "payload": {
                                    "type": kind,
                                    "call_id": "wrapped",
                                    **extra,
                                },
                            }
                        )
                        + "\n"
                    )
            context = load_alert_context(
                _alert(), AlertContextRoots(codex_sessions=(root / "sessions",))
            )
            self.assertTrue(context["found"])
            self.assertEqual(
                [event["label"] for event in context["events"][-2:]],
                ["exec → exec_command", "exec → exec_command"],
            )
            self.assertTrue(all(not event["detail"] for event in context["events"]))
            self.assertNotIn("private-command", json.dumps(context))
            self.assertNotIn("private-output", json.dumps(context))

    def test_outputs_match_ids_instead_of_adjacent_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            payloads = [
                {
                    "type": "function_call",
                    "name": "Read",
                    "call_id": "read",
                    "arguments": "{}",
                },
                {
                    "type": "function_call",
                    "name": "Bash",
                    "call_id": "bash",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "read", "output": "first"},
                {
                    "type": "function_call_output",
                    "call_id": "missing",
                    "output": "unknown",
                },
                {
                    "type": "function_call_output",
                    "call_id": ["bad-id"],
                    "output": "invalid",
                },
                {"type": "function_call_output", "call_id": "bash", "output": "last"},
            ]
            path.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "timestamp": _iso(1_000_000.0 + i),
                            "type": "response_item",
                            "payload": payload,
                        }
                    )
                    for i, payload in enumerate(payloads)
                )
                + "\n",
                encoding="utf-8",
            )
            # 中文注释：调用发生在窗口前，窗口内仍可通过 ID 找到名称。
            extraction = _extract_codex(path, 1_000_002.0, 1_000_010.0)
            self.assertEqual(
                [event["label"] for event in extraction.events or ()],
                ["Read", "", "", "Bash"],
            )

    def test_modern_image_input_and_duplicate_text(self) -> None:
        """新的消息块包含图片；旧事件副本不应重复计数或泄露图片数据。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _write_codex_session(root / "sessions")
            data_url = "data:image/png;base64,c2FtcGxlLWltYWdl"
            records = [
                {"type": "session_meta", "payload": {"cwd": "/home/dev/project"}},
                {
                    "type": "response_item",
                    "timestamp": _iso(1_000_010.0),
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "分析图片"},
                            {"type": "input_image", "image_url": data_url},
                        ],
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": _iso(1_000_010.2),
                    "payload": {"type": "user_message", "message": "分析图片"},
                },
                # 中文注释：真实重复输入仍需计数，不能被先前的双格式记录吞掉。
                {
                    "type": "response_item",
                    "timestamp": _iso(1_000_010.8),
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "分析图片"}],
                    },
                },
            ]
            path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )
            context = load_alert_context(
                _alert(), AlertContextRoots(codex_sessions=(root / "sessions",))
            )
            self.assertEqual(
                [event["kind"] for event in context["events"]],
                ["user", "image", "user"],
            )
            self.assertEqual(
                context["activity_summary"], ["向模型提供文字", "向模型提供图片"]
            )
            self.assertEqual(
                context["totals"]["input_bytes"],
                2 * len("分析图片".encode()) + len(data_url),
            )
            self.assertNotIn(data_url, json.dumps(context))
            self.assertNotIn("分析图片", json.dumps(context, ensure_ascii=False))

    def test_claude_and_kimi_image_only_inputs(self) -> None:
        """无文字的图片输入也要计入活动，图片源数据不能进入明细。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude = root / "claude.jsonl"
            claude.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": _iso(1_000_010.0),
                        "message": {
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "data": "aW1hZ2U=",
                                        "media_type": "image/png",
                                    },
                                }
                            ]
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            kimi = root / "kimi"
            wire = kimi / "agents" / "main" / "wire.jsonl"
            wire.parent.mkdir(parents=True)
            wire.write_text(
                json.dumps(
                    {
                        "type": "context.append_message",
                        "time": 1_000_010_000,
                        "message": {
                            "role": "user",
                            "origin": {"kind": "user"},
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": "data:image/png;base64,aW1hZ2U=",
                                }
                            ],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            for extraction in (
                _extract_claude(claude, 1_000_000, 1_000_030),
                _extract_kimi(kimi, 1_000_000, 1_000_030),
            ):
                self.assertEqual(len(extraction.events or ()), 1)
                event = extraction.events[0]
                self.assertEqual(event["activities"][0]["summary"], "向模型提供图片")
                self.assertGreater(extraction.input_bytes, 0)
                self.assertNotIn("aW1hZ2U=", json.dumps(event))


def _write_jsonl(path: Path, lines: list[dict[str, object]], mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))


class AlertContextCommandCodeTests(unittest.TestCase):
    def test_transcript_events_and_tool_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            session_id = "72be7711-615e-4d09-b176-d8ec114aa8f8"
            session = projects / "home-dev-app" / f"{session_id}.jsonl"
            inside = 1_000_010.0
            command = 'curl -F "image=@/tmp/chart.png" https://example.org'
            _write_jsonl(
                session,
                [
                    {
                        "type": "session",
                        "id": session_id,
                        "cwd": "/home/dev/app",
                        "timestamp": _iso(1_000_000.0),
                    },
                    {
                        "type": "message",
                        "timestamp": _iso(inside - 3600),
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "窗口外的消息"}],
                        },
                    },
                    {
                        "type": "message",
                        "timestamp": _iso(inside),
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "把图片传上去"}],
                        },
                    },
                    {
                        "type": "message",
                        "timestamp": _iso(inside + 1),
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "tool-1",
                                    "name": "shell_command",
                                    "input": {"command": command},
                                }
                            ],
                        },
                    },
                    {
                        "type": "message",
                        "timestamp": _iso(inside + 2),
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool-1",
                                    "content": "ok",
                                }
                            ],
                        },
                    },
                ],
                1_000_040.0,
            )
            _write_jsonl(
                projects / "home-dev-app" / f"{session_id}.checkpoints.jsonl",
                [
                    {
                        "type": "message",
                        "timestamp": _iso(inside),
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "检查点不该出现"}],
                        },
                    }
                ],
                1_000_040.0,
            )
            _write_jsonl(
                projects / "home-dev-other" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl",
                [
                    {
                        "type": "session",
                        "cwd": "/home/dev/other",
                        "timestamp": _iso(1_000_000.0),
                    },
                    {
                        "type": "message",
                        "timestamp": _iso(inside),
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "别的项目"}],
                        },
                    },
                ],
                1_000_040.0,
            )
            context = _visible_alert_context(
                _alert(product="command-code", cwd="/home/dev/app"),
                AlertContextRoots(commandcode_projects=(projects,)),
            )
            self.assertTrue(context["found"])
            self.assertEqual(context["session"]["session_id"], session_id)
            self.assertEqual(context["session"]["path"], str(session))
            self.assertEqual(
                [event["kind"] for event in context["events"]],
                ["user", "tool", "tool_output"],
            )
            tool = context["events"][1]
            self.assertEqual(tool["label"], "shell_command")
            self.assertEqual(tool["activities"][0]["summary"], "发起图片上传")
            self.assertEqual(context["events"][2]["label"], "shell_command")
            rendered = json.dumps(context["events"], ensure_ascii=False)
            self.assertNotIn("窗口外的消息", rendered)
            self.assertNotIn("检查点不该出现", rendered)
            self.assertNotIn("别的项目", rendered)

    def test_header_cwd_mismatch_finds_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            _write_jsonl(
                projects / "home-dev-app" / "72be7711-615e-4d09-b176-d8ec114aa8f8.jsonl",
                [
                    {
                        "type": "session",
                        "cwd": "/tmp/elsewhere",
                        "timestamp": _iso(1_000_000.0),
                    },
                    {
                        "type": "message",
                        "timestamp": _iso(1_000_010.0),
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "目录不对"}],
                        },
                    },
                ],
                1_000_040.0,
            )
            context = _visible_alert_context(
                _alert(product="command-code", cwd="/home/dev/app"),
                AlertContextRoots(commandcode_projects=(projects,)),
            )
            self.assertFalse(context["found"])
            self.assertEqual(context["reason"], "no_session")


class AlertContextGrokTests(unittest.TestCase):
    def _update(self, timestamp: float, update: dict[str, object]) -> dict[str, object]:
        return {
            "timestamp": timestamp,
            "method": "session/update",
            "params": {"sessionId": "sess-1", "update": update},
        }

    def test_updates_events_link_completed_output(self) -> None:
        moment = 1_789_869_213.0
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            session_dir = sessions / "encoded-project" / "sess-1"
            summary = {
                "info": {"cwd": "/home/dev/app", "id": "sess-1"},
            }
            (session_dir).mkdir(parents=True)
            (session_dir / "summary.json").write_text(
                json.dumps(summary), encoding="utf-8"
            )
            output = "y" * 128
            _write_jsonl(
                session_dir / "updates.jsonl",
                [
                    self._update(
                        moment - 3600,
                        {
                            "sessionUpdate": "user_message_chunk",
                            "content": {"type": "text", "text": "窗口外的消息"},
                        },
                    ),
                    self._update(
                        moment * 1000,
                        {
                            "sessionUpdate": "user_message_chunk",
                            "content": {"type": "text", "text": "看看这个文件"},
                        },
                    ),
                    self._update(
                        moment + 1,
                        {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "call-1",
                            "title": "read_file",
                            "rawInput": {"target_file": "/home/dev/app/main.py"},
                            "_meta": {"x.ai/tool": {"name": "read_file"}},
                        },
                    ),
                    self._update(
                        moment + 2,
                        {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "call-1",
                            "status": None,
                            "rawOutput": "还在跑",
                        },
                    ),
                    self._update(
                        moment + 3,
                        {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "call-1",
                            "status": "completed",
                            "rawOutput": output,
                        },
                    ),
                    self._update(
                        moment + 4,
                        {
                            "sessionUpdate": "agent_thought_chunk",
                            "content": {"type": "text", "text": "内部推理不该出现"},
                        },
                    ),
                    self._update(
                        moment + 5,
                        {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "助手正文不该出现"},
                        },
                    ),
                ],
                moment + 10,
            )
            other = sessions / "encoded-project" / "sess-other"
            other.mkdir()
            (other / "summary.json").write_text(
                json.dumps({"info": {"cwd": "/home/dev/other"}}),
                encoding="utf-8",
            )
            _write_jsonl(
                other / "updates.jsonl",
                [
                    self._update(
                        moment + 1,
                        {
                            "sessionUpdate": "user_message_chunk",
                            "content": {"type": "text", "text": "别的项目"},
                        },
                    )
                ],
                moment + 10,
            )
            context = _visible_alert_context(
                _alert(
                    product="grok",
                    cwd="/home/dev/app",
                    first=moment,
                    last=moment + 5,
                ),
                AlertContextRoots(grok_sessions=(sessions,)),
            )
            self.assertTrue(context["found"])
            self.assertEqual(context["session"]["session_id"], "sess-1")
            self.assertEqual(
                [event["kind"] for event in context["events"]],
                ["user", "tool", "tool_output"],
            )
            tool = context["events"][1]
            self.assertEqual(tool["label"], "read_file")
            self.assertEqual(tool["detail"], "/home/dev/app/main.py")
            self.assertEqual(tool["activities"][0]["summary"], "读取代码")
            self.assertEqual(context["events"][2]["label"], "read_file")
            self.assertEqual(context["events"][2]["size"], len(output))
            rendered = json.dumps(context["events"], ensure_ascii=False)
            self.assertNotIn("窗口外的消息", rendered)
            self.assertNotIn("还在跑", rendered)
            self.assertNotIn("内部推理不该出现", rendered)
            self.assertNotIn("助手正文不该出现", rendered)
            self.assertNotIn("别的项目", rendered)

    def test_decoded_project_matches_when_summary_is_missing(self) -> None:
        from urllib.parse import quote

        moment = 1_789_869_213.0
        cwd = "/home/dev/app"
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            encoded = quote(cwd, safe="")
            session_dir = sessions / encoded / "sess-decoded"
            _write_jsonl(
                session_dir / "updates.jsonl",
                [
                    {
                        "timestamp": moment,
                        "params": {
                            "update": {
                                "sessionUpdate": "user_message_chunk",
                                "content": {"type": "text", "text": "解码后的项目"},
                            }
                        },
                    }
                ],
                moment + 10,
            )
            wrong = sessions / encoded / "sess-summary-wins"
            wrong.mkdir()
            (wrong / "summary.json").write_text(
                json.dumps({"info": {"cwd": "/tmp/elsewhere"}}),
                encoding="utf-8",
            )
            _write_jsonl(
                wrong / "updates.jsonl",
                [
                    {
                        "timestamp": moment,
                        "params": {
                            "update": {
                                "sessionUpdate": "user_message_chunk",
                                "content": {"type": "text", "text": "摘要优先"},
                            }
                        },
                    }
                ],
                moment + 10,
            )
            context = _visible_alert_context(
                _alert(product="grok", cwd=cwd, first=moment, last=moment),
                AlertContextRoots(grok_sessions=(sessions,)),
            )
            self.assertTrue(context["found"])
            self.assertEqual(context["session"]["session_id"], "sess-decoded")
            self.assertNotIn(
                "摘要优先", json.dumps(context["events"], ensure_ascii=False)
            )

    def test_fallback_keeps_events_before_window(self) -> None:
        moment = 1_789_869_213.0
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            session_dir = sessions / "project" / "sess-old"
            session_dir.mkdir(parents=True)
            (session_dir / "summary.json").write_text(
                json.dumps({"info": {"cwd": "/home/dev/app"}}),
                encoding="utf-8",
            )
            _write_jsonl(
                session_dir / "updates.jsonl",
                [
                    {
                        "timestamp": moment - 3600,
                        "params": {
                            "update": {
                                "sessionUpdate": "user_message_chunk",
                                "content": {"type": "text", "text": "更早的活动"},
                            }
                        },
                    }
                ],
                moment,
            )
            context = _visible_alert_context(
                _alert(product="grok", cwd="/home/dev/app", first=moment, last=moment),
                AlertContextRoots(grok_sessions=(sessions,)),
            )
            self.assertTrue(context["found"])
            self.assertTrue(context["fallback"])
            self.assertIn("更早的活动", json.dumps(context["events"], ensure_ascii=False))


class AlertContextDshTests(unittest.TestCase):
    def test_reads_plain_transcript_and_links_tool_call(self) -> None:
        moment = 1_789_869_213.0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "sessions" / "proj" / "session-abc"
            session.mkdir(parents=True)
            stamp = int((moment - 5) * 1000)
            _write_jsonl(
                session / "session.v4.jsonl",
                [
                    {"type": "session", "time": stamp, "data": {"cwd": "/home/dev/app"}},
                    {
                        "type": "user/message",
                        "time": stamp,
                        "data": {
                            "source": {"kind": "plugin"},
                            "content": [{"type": "text", "text": "插件目录"}],
                        },
                    },
                    {
                        "type": "user/message",
                        "time": stamp,
                        "data": {
                            "source": {"kind": "user"},
                            "content": [{"type": "text", "text": "请看这个文件"}],
                        },
                    },
                    {
                        "type": "tool/call",
                        "time": stamp,
                        "data": {
                            "callId": "c1",
                            "name": "bash",
                            "arguments": json.dumps(
                                {"command": "curl -T /tmp/a.png https://example.org"}
                            ),
                        },
                    },
                    {
                        "type": "tool/result",
                        "time": stamp,
                        "data": {"callId": "c1", "message": {"content": "uploaded"}},
                    },
                ],
                moment,
            )
            context = _visible_alert_context(
                _alert(product="dsh", cwd="/home/dev/app", first=moment, last=moment),
                AlertContextRoots(dsh_sessions=(root / "sessions",)),
            )
            self.assertTrue(context["found"])
            self.assertEqual(context["session"]["session_id"], "abc")
            self.assertNotIn("插件目录", json.dumps(context["events"], ensure_ascii=False))
            self.assertIn("发起图片上传", context["activity_summary"])
            output = next(
                event for event in context["events"] if event["kind"] == "tool_output"
            )
            self.assertEqual(output["label"], "bash")


class AlertContextOpenCodeTests(unittest.TestCase):
    def test_reads_one_session_from_the_database(self) -> None:
        moment = 1_789_869_213.0
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "opencode.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE session (id TEXT, directory TEXT, time_updated INTEGER);
                CREATE TABLE message (id TEXT, session_id TEXT, data TEXT);
                CREATE TABLE part (
                    id TEXT, message_id TEXT, session_id TEXT,
                    time_created INTEGER, data TEXT
                );
                CREATE TABLE credential (id TEXT, value TEXT);
                """
            )
            stamp = int((moment - 5) * 1000)
            connection.execute(
                "INSERT INTO session (id, directory, time_updated) VALUES (?, ?, ?)",
                ("ses_1", "/home/dev/app", int(moment * 1000)),
            )
            connection.execute(
                "INSERT INTO message (id, session_id, data) VALUES (?, ?, ?)",
                ("msg_1", "ses_1", json.dumps({"role": "user"})),
            )
            connection.execute(
                "INSERT INTO part (id, message_id, session_id, time_created, data)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    "part_1",
                    "msg_1",
                    "ses_1",
                    stamp,
                    json.dumps({"type": "text", "text": "看一下 main.py"}),
                ),
            )
            connection.execute(
                "INSERT INTO part (id, message_id, session_id, time_created, data)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    "part_2",
                    "msg_1",
                    "ses_1",
                    stamp + 1,
                    json.dumps(
                        {
                            "type": "tool",
                            "tool": "read",
                            "callID": "call_1",
                            "state": {
                                "status": "completed",
                                "input": {"filePath": "/tmp/main.py"},
                                "output": "print(1)",
                            },
                        }
                    ),
                ),
            )
            connection.execute(
                "INSERT INTO credential (id, value) VALUES (?, ?)",
                ("token", "SECRET-OPENCODE-TOKEN"),
            )
            connection.commit()
            connection.close()
            os.utime(database, (moment, moment))
            context = _visible_alert_context(
                _alert(product="opencode", cwd="/home/dev/app", first=moment, last=moment),
                AlertContextRoots(opencode_dbs=(database,)),
            )
            rendered = json.dumps(context, ensure_ascii=False)
            self.assertTrue(context["found"])
            self.assertEqual(context["session"]["session_id"], "ses_1")
            self.assertIn("读取代码", context["activity_summary"])
            self.assertNotIn("SECRET-OPENCODE-TOKEN", rendered)


class AlertContextCursorTests(unittest.TestCase):
    def test_uses_mtime_when_lines_have_no_timestamp(self) -> None:
        moment = 1_789_869_213.0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = (
                root
                / "-home-dev-app"
                / "agent-transcripts"
                / "abc"
                / "abc.jsonl"
            )
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "role": "user",
                                "message": {
                                    "content": [
                                        {"type": "text", "text": "<user_query>看代码</user_query>"}
                                    ]
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "role": "assistant",
                                "message": {
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "id": "t1",
                                            "name": "read",
                                            "input": {"filePath": "/tmp/main.py"},
                                        }
                                    ]
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            os.utime(transcript, (moment, moment))
            context = _visible_alert_context(
                _alert(product="cursor", cwd="/home/dev/app", first=moment, last=moment),
                AlertContextRoots(cursor_projects=(root,)),
            )
            self.assertTrue(context["found"])
            self.assertFalse(context["fallback"])
            self.assertIn("读取代码", context["activity_summary"])

            later = moment + 3600
            os.utime(transcript, (later, later))
            continued = _visible_alert_context(
                _alert(product="cursor", cwd="/home/dev/app", first=moment, last=moment),
                AlertContextRoots(cursor_projects=(root,)),
            )
            self.assertTrue(continued["fallback"])


class AlertContextGeminiTests(unittest.TestCase):
    def test_jsonl_tools_skip_model_prose(self) -> None:
        moment = 1_789_869_213.0
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            chats = home / "tmp" / "proj" / "chats"
            chats.mkdir(parents=True)
            (home / "projects.json").write_text(
                json.dumps({"proj": "/home/dev/app"}),
                encoding="utf-8",
            )
            _write_jsonl(
                chats / "session.jsonl",
                [
                    {
                        "type": "user",
                        "id": "u1",
                        "timestamp": _iso(moment - 5),
                        "content": "看一下这个文件",
                    },
                    {
                        "type": "gemini",
                        "id": "g1",
                        "timestamp": _iso(moment - 4),
                        "content": "这段模型正文不应出现",
                        "toolCalls": [
                            {
                                "name": "read_file",
                                "id": "c1",
                                "args": {"file_path": "/tmp/main.py"},
                            }
                        ],
                    },
                ],
                moment,
            )
            context = _visible_alert_context(
                _alert(product="gemini", cwd="/home/dev/app", first=moment, last=moment),
                AlertContextRoots(gemini_homes=(home,)),
            )
            rendered = json.dumps(context["events"], ensure_ascii=False)
            self.assertTrue(context["found"])
            self.assertIn("读取代码", context["activity_summary"])
            self.assertNotIn("模型正文", rendered)


class AlertContextQwenTests(unittest.TestCase):
    def test_project_slug_chat(self) -> None:
        moment = 1_789_869_213.0
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            chats = home / "projects" / "home-dev-app" / "chats"
            chats.mkdir(parents=True)
            _write_jsonl(
                chats / "session.jsonl",
                [
                    {
                        "type": "user",
                        "id": "u1",
                        "timestamp": _iso(moment - 5),
                        "content": "推上去",
                    },
                    {
                        "type": "gemini",
                        "id": "g1",
                        "timestamp": _iso(moment - 4),
                        "toolCalls": [
                            {
                                "name": "shell",
                                "id": "c1",
                                "args": {"command": "docker push example/app:latest"},
                            }
                        ],
                    },
                ],
                moment,
            )
            context = _visible_alert_context(
                _alert(product="qwen", cwd="/home/dev/app", first=moment, last=moment),
                AlertContextRoots(qwen_homes=(home,)),
            )
            self.assertTrue(context["found"])
            self.assertIn("发起文件上传", context["activity_summary"])


class AlertContextCacheTests(unittest.TestCase):
    """同一文件签名和时间窗只提取一次，内容开关共用这次结果。"""

    def test_repeat_loads_share_one_extraction(self) -> None:
        from unittest.mock import patch

        from a_token_monitor.alert_context import lookup as alert_context_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _write_codex_session(root / "sessions")
            roots = AlertContextRoots(codex_sessions=(root / "sessions",))
            alert = _alert()
            original = alert_context_module._dispatch_extract
            with patch.object(
                alert_context_module,
                "_dispatch_extract",
                wraps=original,
            ) as mocked:
                hidden = load_alert_context(alert, roots)
                visible = load_alert_context(alert, roots, include_content=True)
                self.assertEqual(mocked.call_count, 1)
                self.assertTrue(all(item["detail"] == "" for item in hidden["events"]))
                self.assertIn(
                    "cat big.log",
                    [item["detail"] for item in visible["events"]],
                )
                self.assertEqual(hidden["activity_summary"], visible["activity_summary"])
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("\n")
                os.utime(path, (1_000_080.0, 1_000_080.0))
                again = load_alert_context(alert, roots)
                self.assertEqual(mocked.call_count, 2)
                self.assertTrue(again["found"])

    def test_explicit_empty_homes_do_not_use_local_defaults(self) -> None:
        from a_token_monitor.alert_context import configured_alert_context_roots

        roots = configured_alert_context_roots(
            homes={
                "opencode": (),
                "cursor": (),
                "gemini": (),
                "qwen": (),
                "aider": (),
            },
        )
        self.assertEqual(roots.opencode_dbs, ())
        self.assertEqual(roots.cursor_projects, ())
        self.assertEqual(roots.gemini_homes, ())
        self.assertEqual(roots.qwen_homes, ())
        self.assertEqual(roots.aider_homes, ())

    def test_aider_tools_use_file_mtime_and_hide_prose(self) -> None:
        moment = 1_780_000_000.0
        history = "\n".join(
            [
                "#### 机密正文不要出现",
                "> Applied edit to src/app.py  ",
                "> Did not apply edit to src/skip.py",
                "> Creating empty file notes.txt",
                "> Running git status",
                "> Tokens: 12 sent, 3 received.",
            ]
        ) + "\n"
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "repo"
            home.mkdir()
            path = home / ".aider.chat.history.md"
            path.write_text(history, encoding="utf-8")
            os.utime(path, (moment, moment))
            alert = _alert(
                product="aider",
                cwd=str(home),
                first=moment,
                last=moment,
            )
            roots = AlertContextRoots(aider_homes=(home,))
            hidden = load_alert_context(alert, roots)
            visible = load_alert_context(alert, roots, include_content=True)
            os.utime(path, (moment - 200, moment - 200))
            fallback = load_alert_context(alert, roots, include_content=True)
        self.assertTrue(visible["found"])
        self.assertFalse(visible["fallback"])
        self.assertEqual(
            visible["activity_summary"],
            ["修改代码", "修改文件", "操作本地代码仓库"],
        )
        dumped = json.dumps(visible["events"], ensure_ascii=False)
        self.assertNotIn("机密正文", dumped)
        self.assertNotIn("skip.py", dumped)
        self.assertNotIn("Tokens", dumped)
        self.assertTrue(all(item["detail"] == "" for item in hidden["events"]))
        details = [item["detail"] for item in visible["events"]]
        self.assertEqual(details, ["src/app.py", "notes.txt", "git status"])
        self.assertEqual(visible["session"]["session_id"], home.name)
        self.assertTrue(fallback["fallback"])
        self.assertEqual(
            fallback["activity_summary"],
            visible["activity_summary"],
        )


if __name__ == "__main__":
    unittest.main()
