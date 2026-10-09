"""归档竞态、真实 HTTP 热更新及告警隐私的回归验证。"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen

from a_token_monitor.alert_context import _extract_codex, _extract_kimi, _excerpt
from a_token_monitor.alerts import StoredAlert
from a_token_monitor.dashboard import DashboardConfig, DashboardServer
from a_token_monitor.housekeeping import AuditTarget, HousekeepingMonitor
from a_token_monitor.local_time import to_local
from a_token_monitor.multi_account import MultiAccountMonitor
from a_token_monitor.usage import UsageAggregator


def _housekeeper(root: Path, active: set[str]) -> tuple[HousekeepingMonitor, Path]:
    """构造纯临时会话，未来的评估时刻使其满足闲置时间条件。"""

    home = root / "home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    path = (
        sessions
        / "rollout-2026-06-01T01-00-00-019ed409-4ff8-7083-98a5-2502125735ce.jsonl"
    )
    path.write_bytes(b"original\n")
    monitor = HousekeepingMonitor(
        (AuditTarget("test", "codex", home, sessions),),
        archive_dir=root / "archives",
        active_paths=lambda: set(active),
    )
    return monitor, path


class ArchiveConsistencyTests(unittest.TestCase):
    """压缩时发生写入、恢复活动或 manifest 失败都必须保留源文件。"""

    def test_append_during_compression_preserves_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            monitor, path = _housekeeper(Path(directory), set())

            def append(progress: dict[str, object]) -> None:
                if progress["phase"] == "verify":
                    path.write_bytes(b"original\nnew data\n")

            result = monitor.archive(
                now=time.time() + 40 * 86400,
                confirm=True,
                progress=append,
            )
            self.assertEqual(result["deleted"], 0)
            self.assertEqual(len(result["failed"]), 1)
            self.assertIn(b"new data", path.read_bytes())

    def test_resumed_activity_without_write_preserves_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            active: set[str] = set()
            monitor, path = _housekeeper(Path(directory), active)

            def resume(progress: dict[str, object]) -> None:
                if progress["phase"] == "verify":
                    active.add(str(path))

            result = monitor.archive(
                now=time.time() + 40 * 86400,
                confirm=True,
                progress=resume,
            )
            self.assertEqual(result["deleted"], 0)
            self.assertTrue(path.exists())

    def test_manifest_failure_prevents_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            monitor, path = _housekeeper(Path(directory), set())
            with patch(
                "a_token_monitor.housekeeping._write_manifest",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaises(OSError):
                    monitor.archive(now=time.time() + 40 * 86400, confirm=True)
            self.assertTrue(path.exists())

    def test_archive_and_clean_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            monitor, path = _housekeeper(Path(directory), set())
            compressing, release, cleaning = (
                threading.Event(),
                threading.Event(),
                threading.Event(),
            )

            def wait(progress: dict[str, object]) -> None:
                if progress["phase"] == "compress":
                    compressing.set()
                    release.wait(3)

            def clean() -> None:
                monitor.clean(now=time.time() + 40 * 86400, confirm=True)
                cleaning.set()

            first = threading.Thread(
                target=monitor.archive,
                kwargs={
                    "now": time.time() + 40 * 86400,
                    "confirm": True,
                    "progress": wait,
                },
            )
            second = threading.Thread(target=clean)
            first.start()
            try:
                self.assertTrue(compressing.wait(2))
                second.start()
                self.assertFalse(cleaning.wait(0.1))
                self.assertTrue(path.exists())
            finally:
                release.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
            self.assertTrue(cleaning.is_set())


class DashboardReloadTests(unittest.TestCase):
    """通过真实请求确认配置变动传递到了已有 HTTP 服务。"""

    def test_non_codex_homes_reload_for_get_and_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = root / "old", root / "new"
            old.mkdir()
            new.mkdir()
            monitor = MultiAccountMonitor(
                (),
                state_dir=root / "state",
                grok_homes=(old,),
                kimi_homes=(),
                dsh_homes=(),
                commandcode_homes=(),
                claude_homes=(),
                opencode_homes=(),
                cursor_homes=(),
                gemini_homes=(),
                qwen_homes=(), aider_homes=(),
            )
            aggregator = UsageAggregator()
            server = DashboardServer(
                config=DashboardConfig(port=0),
                usage_aggregator=aggregator,
                grok_homes=(old,),
                kimi_homes=(),
                dsh_homes=(),
                commandcode_homes=(),
                claude_homes=(),
                opencode_homes=(),
                cursor_homes=(),
                gemini_homes=(),
                qwen_homes=(), aider_homes=(),
            )
            monitor._dashboard = server
            server.start()
            try:
                monitor.apply_scan_dirs(
                    {"grok": (new,), "kimi": (new,), "claude": (new,)}
                )
                with patch(
                    "a_token_monitor.dashboard.build_multi_dashboard_state",
                    return_value={"sessions": []},
                ) as build:
                    host, port = server.address
                    with urlopen(
                        f"http://{host}:{port}/api/state", timeout=3
                    ) as response:
                        response.read()
                    self.assertEqual(build.call_args.kwargs["grok_homes"], (new,))
                    self.assertEqual(build.call_args.kwargs["kimi_homes"], (new,))
                    self.assertEqual(build.call_args.kwargs["claude_homes"], (new,))
                    self.assertEqual(build.call_args.kwargs["commandcode_homes"], ())
                    with urlopen(
                        Request(f"http://{host}:{port}/api/state", method="HEAD"),
                        timeout=3,
                    ) as response:
                        self.assertEqual(response.status, 200)
                    self.assertEqual(build.call_count, 1)
                    # 中文注释：缓存有效期内再次改目录，HEAD 也必须立刻看到新配置。
                    server.update_homes(
                        grok_homes=(old,),
                        kimi_homes=(),
                        dsh_homes=(),
                        commandcode_homes=(new,),
                        claude_homes=(),
                        opencode_homes=(),
                        cursor_homes=(),
                        gemini_homes=(),
                        qwen_homes=(), aider_homes=(),
                    )
                    with urlopen(
                        Request(f"http://{host}:{port}/api/state", method="HEAD"),
                        timeout=3,
                    ) as response:
                        self.assertEqual(response.status, 200)
                    self.assertEqual(build.call_count, 2)
                    self.assertEqual(build.call_args.kwargs["grok_homes"], (old,))
                    self.assertEqual(
                        build.call_args.kwargs["commandcode_homes"], (new,)
                    )
            finally:
                monitor.close()

    def test_content_excerpts_require_loopback(self) -> None:
        self.assertFalse(DashboardConfig().alert_context_content)
        with self.assertRaises(ValueError):
            DashboardConfig(host="0.0.0.0", alert_context_content=True)

    def test_http_context_default_hidden_and_opt_in_redacted(self) -> None:
        """通过真实 HTTP 验证默认隐私开关与显式开启后的脱敏输出。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = time.time()
            stamp = to_local(now).strftime("%Y-%m-%dT%H-%M-%S")
            session = (
                root
                / "sessions"
                / f"rollout-{stamp}-019ed409-4ff8-7083-98a5-2502125735ce.jsonl"
            )
            session.parent.mkdir()
            session.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in [
                        {"type": "session_meta", "payload": {"cwd": "/workspace/demo"}},
                        {
                            "type": "event_msg",
                            "timestamp": datetime.fromtimestamp(
                                now, timezone.utc
                            ).isoformat(),
                            "payload": {
                                "type": "user_message",
                                "message": "inspect token=private-value",
                            },
                        },
                        {
                            "type": "response_item",
                            "timestamp": datetime.fromtimestamp(
                                now, timezone.utc
                            ).isoformat(),
                            "payload": {
                                "type": "custom_tool_call",
                                "name": "exec",
                                "call_id": "wrapped",
                                "input": 'await tools.exec_command({cmd: "cat /workspace/private-file.py; token=private-value"});',
                            },
                        },
                        {
                            "type": "response_item",
                            "timestamp": datetime.fromtimestamp(
                                now, timezone.utc
                            ).isoformat(),
                            "payload": {
                                "type": "custom_tool_call_output",
                                "call_id": "wrapped",
                                "output": "private-value",
                            },
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            alert = StoredAlert(
                id=1,
                level="danger",
                kind="burst",
                product="codex",
                pid=1,
                process_key="test",
                command="codex",
                cwd="/workspace/demo",
                remote=None,
                bytes=100,
                peak_bytes=100,
                window_seconds=15,
                message="test",
                first_seen_at=now,
                last_seen_at=now,
                count=1,
                acknowledged_at=None,
            )
            store = Mock()
            store.get.return_value = alert
            for enabled in (False, True):
                with DashboardServer(
                    config=DashboardConfig(port=0, alert_context_content=enabled),
                    account_metadata={"test": {"codex_home": str(root)}},
                    usage_aggregator=UsageAggregator(),
                    alert_store=store,
                    grok_homes=(),
                    kimi_homes=(),
                    dsh_homes=(),
                    commandcode_homes=(),
                    claude_homes=(),
                    opencode_homes=(),
                    cursor_homes=(),
                    gemini_homes=(),
                    qwen_homes=(), aider_homes=(),
                ) as server:
                    host, port = server.address
                    with urlopen(
                        f"http://{host}:{port}/api/alerts/context?id=1", timeout=3
                    ) as response:
                        context = json.load(response)["context"]
                self.assertTrue(context["found"])
                self.assertEqual(context["content_enabled"], enabled)
                self.assertNotIn("private-value", json.dumps(context))
                self.assertEqual(
                    [event["label"] for event in context["events"][1:]],
                    ["exec → exec_command", "exec → exec_command"],
                )
                activity = context["events"][1]["activities"][0]
                self.assertEqual(activity["summary"], "读取代码")
                self.assertEqual(
                    activity["target"], "private-file.py" if enabled else ""
                )
                self.assertEqual(
                    context["activity_summary"], ["向模型提供文字", "读取代码"]
                )
                detail = context["events"][0]["detail"]
                self.assertEqual(detail, "inspect token=[REDACTED]" if enabled else "")

    def test_background_snapshot_does_not_wait_for_index_lock(self) -> None:
        """索引锁被占用时，已有快照仍应能被 HTTP 查询读取。"""

        aggregator = UsageAggregator(background_indexing=True)
        read = threading.Event()
        worker: threading.Thread | None = None
        try:
            aggregator.snapshot({}, now=time.time())

            def query() -> None:
                aggregator.snapshot({})
                aggregator.cached_snapshot()
                read.set()

            with aggregator._lock:
                worker = threading.Thread(target=query)
                worker.start()
                self.assertTrue(read.wait(1))
        finally:
            if worker is not None:
                worker.join(2)
            aggregator.close()


class AlertContextConsistencyTests(unittest.TestCase):
    """字节口径、跨 agent 上限及常见凭据脱敏。"""

    def test_unicode_fallback_uses_utf8_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            now = time.time()
            message = "你好🙂"
            timestamp = datetime.fromtimestamp(now - 100, timezone.utc).isoformat()
            path.write_text(
                json.dumps(
                    {
                        "timestamp": timestamp,
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": message},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            result = _extract_codex(path, now - 10, now + 10)
            self.assertTrue(result.fallback)
            self.assertEqual(result.input_bytes, 10)
            self.assertEqual(result.events[0]["size"], 10)

    def test_kimi_combined_events_have_global_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = time.time()
            record = {
                "time": now * 1000,
                "type": "context.append_message",
                "message": {
                    "role": "user",
                    "content": "你好🙂",
                    "origin": {"kind": "user"},
                },
            }
            for name in ("main", "subagent"):
                path = root / "agents" / name / "wire.jsonl"
                path.parent.mkdir(parents=True)
                path.write_text((json.dumps(record) + "\n") * 45, encoding="utf-8")
            result = _extract_kimi(root, now - 10, now + 10)
            self.assertEqual(len(result.events), 60)
            self.assertTrue(result.truncated)
            self.assertEqual(result.input_bytes, 900)

    def test_redacts_before_truncating(self) -> None:
        text = 'token=private-value api_key="second-value" --password third-value Bearer fourth-value https://user:fifth-value@example.com sk-abcdefghijk'
        result = _excerpt(text, 500)
        for secret in (
            "private-value",
            "second-value",
            "third-value",
            "fourth-value",
            "fifth-value",
            "sk-abcdefghijk",
        ):
            self.assertNotIn(secret, result)
        self.assertIn("[REDACTED]", result)
