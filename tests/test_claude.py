"""Claude Code 用量索引测试（使用脱敏合成样本）。"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from unittest import mock

from _platform_support import requires_symlinks
import a_token_monitor.claude as claude_module
from a_token_monitor.claude import (
    claude_home_for,
    claude_has_inline_sidechains,
    claude_session_id,
    claude_session_id_from_open_path,
    list_claude_active_sessions,
    list_claude_transcripts,
    main_transcripts,
    parse_claude_chunk,
    read_claude_account,
    read_claude_quota,
    resolve_claude_homes,
    resolve_sidechain_policy,
    subagent_transcripts,
)
from a_token_monitor.registry import MultiSessionRegistry
from a_token_monitor.usage import TokenUsage, UsageAggregator, _estimate_usage, _lookup_pricing


def _assistant_line(
    *,
    timestamp: str,
    model: str = "claude-sonnet-4-5-20250929",
    message_id: str = "msg_01",
    cwd: str | None = "/home/dev/project-alpha",
    input_tokens: int = 1_000,
    output_tokens: int = 200,
    cache_read: int = 0,
    cache_write: int = 0,
    sidechain: bool = False,
) -> str:
    """构造一行脱敏的 assistant 记录。"""

    usage: dict[str, object] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if cache_read:
        usage["cache_read_input_tokens"] = cache_read
    if cache_write:
        usage["cache_creation_input_tokens"] = cache_write
    payload = {
        "type": "assistant",
        "timestamp": timestamp,
        "sessionId": "session-alpha",
        "cwd": cwd,
        "isSidechain": sidechain,
        "message": {
            "id": message_id,
            "model": model,
            "usage": usage,
            "content": [{"type": "text", "text": "（脱敏样本）"}],
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def _write_session(
    home: Path,
    project: str,
    session_id: str,
    lines: list[str],
) -> Path:
    """写入一个主会话 JSONL。"""

    path = home / "projects" / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class ClaudeParserTests(unittest.TestCase):
    """验证 Claude Code JSONL 的解析与去重。"""

    def test_parses_usage_into_project_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / ".claude"
            path = _write_session(
                home,
                "-home-dev-project-alpha",
                "11111111-1111-4111-8111-111111111111",
                [
                    _assistant_line(
                        timestamp="2026-05-01T10:00:00Z",
                        input_tokens=100,
                        output_tokens=20,
                    ),
                    _assistant_line(
                        timestamp="2026-05-01T10:05:00Z",
                        message_id="msg_02",
                        model="claude-opus-4-1-20250805",
                        input_tokens=900,
                        output_tokens=300,
                        cache_read=400,
                        cache_write=100,
                        cwd="/home/dev/project-beta",
                    ),
                ],
            )

            result = parse_claude_chunk(path, 0)

        self.assertEqual(len(result.events), 2)
        first, second = result.events
        self.assertEqual(first.model, "claude-sonnet-4-5-20250929")
        # 中文注释：input_tokens 换算成「总输入」口径
        self.assertEqual(first.input_tokens, 100)
        self.assertEqual(first.total_tokens, 120)
        self.assertEqual(first.project, "/home/dev/project-alpha")
        self.assertEqual(second.cached_input_tokens, 400)
        self.assertEqual(second.cache_write_input_tokens, 100)
        self.assertEqual(second.input_tokens, 1_400)
        self.assertEqual(second.total_tokens, 1_700)
        self.assertEqual(result.project, "/home/dev/project-alpha")
        self.assertTrue(result.reached_eof)

    def test_duplicate_message_ids_are_counted_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / ".claude"
            path = _write_session(
                home,
                "-home-dev-project-alpha",
                "22222222-2222-4222-8222-222222222222",
                [
                    _assistant_line(timestamp="2026-05-01T10:00:00Z"),
                    _assistant_line(timestamp="2026-05-01T10:00:01Z"),
                    _assistant_line(
                        timestamp="2026-05-01T10:01:00Z",
                        message_id="msg_02",
                    ),
                ],
            )

            first_pass = parse_claude_chunk(path, 0)
            resumed = parse_claude_chunk(
                path,
                0,
                seen_ids=first_pass.seen_ids,
            )

        self.assertEqual(len(first_pass.events), 2)
        self.assertIn("msg_01", first_pass.seen_ids)
        self.assertEqual(resumed.events, ())

    def test_synthetic_and_zero_usage_lines_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / ".claude"
            path = _write_session(
                home,
                "-home-dev-project-alpha",
                "33333333-3333-4333-8333-333333333333",
                [
                    _assistant_line(
                        timestamp="2026-05-01T10:00:00Z",
                        model="<synthetic>",
                    ),
                    _assistant_line(
                        timestamp="2026-05-01T10:01:00Z",
                        message_id="msg_zero",
                        input_tokens=0,
                        output_tokens=0,
                    ),
                    json.dumps({"type": "user", "timestamp": "2026-05-01T10:02:00Z"}),
                ],
            )

            result = parse_claude_chunk(path, 0)

        self.assertEqual(result.events, ())

    def test_incremental_resume_and_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / ".claude"
            path = _write_session(
                home,
                "-home-dev-project-alpha",
                "44444444-4444-4444-8444-444444444444",
                [_assistant_line(timestamp="2026-05-01T10:00:00Z")],
            )
            first = parse_claude_chunk(path, 0)

            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    _assistant_line(
                        timestamp="2026-05-01T10:10:00Z",
                        message_id="msg_02",
                    )
                    + "\n"
                )
            second = parse_claude_chunk(
                path,
                first.next_offset,
                seen_ids=first.seen_ids,
            )

            # 中文注释：日志轮转：文件被替换后从 0 重新解析
            path.write_text(
                _assistant_line(
                    timestamp="2026-05-01T11:00:00Z",
                    message_id="msg_03",
                )
                + "\n",
                encoding="utf-8",
            )
            rotated = parse_claude_chunk(path, 0)

        self.assertEqual(len(first.events), 1)
        self.assertEqual(len(second.events), 1)
        self.assertEqual(second.events[0].message_id, "msg_02")
        self.assertEqual(len(rotated.events), 1)
        self.assertEqual(rotated.events[0].message_id, "msg_03")

    def test_incomplete_trailing_line_waits_for_next_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / ".claude"
            path = _write_session(
                home,
                "-home-dev-project-alpha",
                "55555555-5555-4555-8555-555555555555",
                [_assistant_line(timestamp="2026-05-01T10:00:00Z")],
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"type": "assistant", "message": {"id": "msg_02"')

            result = parse_claude_chunk(path, 0)

            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    ', "model": "claude-haiku-4-5", "usage": {"input_tokens": 5,'
                    ' "output_tokens": 5}}}\n'
                )
            resumed = parse_claude_chunk(path, result.next_offset, seen_ids=result.seen_ids)

        self.assertEqual(len(result.events), 1)
        # 未写完的行退回重读，但本轮仍然算读完（否则索引永远不 complete）
        self.assertTrue(result.reached_eof)
        self.assertEqual(len(resumed.events), 1)
        self.assertEqual(resumed.events[0].model, "claude-haiku-4-5")


class ClaudeLayoutTests(unittest.TestCase):
    """验证目录发现、身份读取与子代理策略。"""

    def test_resolve_homes_prefers_argument_then_env(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            explicit = Path(temporary_directory) / "claude-a"
            explicit.mkdir()
            self.assertEqual(resolve_claude_homes([explicit]), (explicit,))

            other = Path(temporary_directory) / "claude-b"
            other.mkdir()
            previous = os.environ.get("CLAUDE_CONFIG_DIR")
            os.environ["CLAUDE_CONFIG_DIR"] = str(other)
            try:
                self.assertEqual(resolve_claude_homes(None), (other,))
            finally:
                if previous is None:
                    os.environ.pop("CLAUDE_CONFIG_DIR", None)
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = previous

    def test_lists_main_and_subagent_transcripts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / ".claude"
            main = _write_session(
                home,
                "-home-dev-project-alpha",
                "66666666-6666-4666-8666-666666666666",
                [_assistant_line(timestamp="2026-05-01T10:00:00Z")],
            )
            agent = (
                home
                / "projects"
                / "-home-dev-project-alpha"
                / "66666666-6666-4666-8666-666666666666"
                / "subagents"
                / "workflows"
                / "wf_1"
                / "agent-abc.jsonl"
            )
            agent.parent.mkdir(parents=True, exist_ok=True)
            agent.write_text(
                _assistant_line(
                    timestamp="2026-05-01T10:01:00Z",
                    message_id="msg_agent",
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertEqual(main_transcripts(home), (main,))
            self.assertEqual(subagent_transcripts(home), (agent,))
            self.assertEqual(len(list_claude_transcripts(home)), 2)
            self.assertEqual(
                list_claude_transcripts(home, include_subagents=False),
                (main,),
            )
            self.assertEqual(
                claude_session_id(agent),
                "66666666-6666-4666-8666-666666666666",
            )
            self.assertEqual(
                claude_session_id(main),
                "66666666-6666-4666-8666-666666666666",
            )

    def test_inline_sidechains_switch_off_subagent_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / ".claude"
            _write_session(
                home,
                "-home-dev-project-alpha",
                "77777777-7777-4777-8777-777777777777",
                [
                    _assistant_line(
                        timestamp="2026-05-01T10:00:00Z",
                        sidechain=True,
                    )
                ],
            )
            policy = resolve_sidechain_policy([home], now=1_000.0)

        self.assertFalse(policy[home])

    def test_no_inline_sidechains_keeps_subagent_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / ".claude"
            _write_session(
                home,
                "-home-dev-project-alpha",
                "88888888-8888-4888-8888-888888888888",
                [_assistant_line(timestamp="2026-05-01T10:00:00Z")],
            )

            detected = claude_has_inline_sidechains(home)
            policy = resolve_sidechain_policy([home], now=1_000.0)

        self.assertFalse(detected)
        self.assertTrue(policy[home])

    def test_reads_account_identity_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".claude"
            home.mkdir()
            (root / ".claude.json").write_text(
                json.dumps(
                    {
                        "userID": "user-abc",
                        "oauthAccount": {"emailAddress": "dev@example.com"},
                        "accessToken": "must-not-be-read",
                    }
                ),
                encoding="utf-8",
            )

            account = read_claude_account(home)

        self.assertEqual(account.account_id, "user-abc")
        self.assertEqual(account.display_name, "dev@example.com")
        self.assertEqual(account.account_key, "user-abc")
        self.assertNotIn("must-not-be-read", repr(account))

    def test_config_dir_home_reads_its_own_identity(self) -> None:
        """CLAUDE_CONFIG_DIR 目录自带 .claude.json，不能读到默认账号的身份。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            default_home = root / ".claude"
            work_home = root / ".claude-work"
            default_home.mkdir()
            work_home.mkdir()
            (root / ".claude.json").write_text(
                json.dumps(
                    {
                        "userID": "user-personal",
                        "oauthAccount": {"emailAddress": "me@example.com"},
                    }
                ),
                encoding="utf-8",
            )
            (work_home / ".claude.json").write_text(
                json.dumps(
                    {
                        "userID": "user-work",
                        "oauthAccount": {"emailAddress": "work@example.com"},
                    }
                ),
                encoding="utf-8",
            )

            personal = read_claude_account(default_home)
            work = read_claude_account(work_home)

        self.assertEqual(personal.account_id, "user-personal")
        self.assertEqual(personal.display_name, "me@example.com")
        self.assertEqual(work.account_id, "user-work")
        self.assertEqual(work.display_name, "work@example.com")

    def test_home_matching(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / ".claude"
            path = home / "projects" / "-home-dev-x" / "session.jsonl"
            other = Path(temporary_directory) / "elsewhere.jsonl"

            self.assertEqual(claude_home_for(path, [home]), home)
            self.assertIsNone(claude_home_for(other, [home]))


class ClaudeActiveSessionTests(unittest.TestCase):
    """验证按进程打开文件识别 Claude Code 活动会话。"""

    def _home(self, root: Path) -> Path:
        home = root / ".claude"
        transcript = (
            home
            / "projects"
            / "-home-dev-project-alpha"
            / "11111111-1111-4111-8111-111111111111.jsonl"
        )
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "user",
                            "cwd": "/home/dev/project-alpha",
                            "timestamp": "2026-05-01T10:00:00Z",
                        }
                    ),
                    _assistant_line(
                        timestamp="2026-05-01T10:01:00Z",
                        model="claude-sonnet-4-5",
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return home

    def _process(self, proc_root: Path, pid: int, open_path: Path | None) -> None:
        directory = proc_root / str(pid)
        (directory / "fd").mkdir(parents=True, exist_ok=True)
        (directory / "comm").write_text("claude\n", encoding="utf-8")
        (directory / "cmdline").write_bytes(b"claude\0")
        (directory / "stat").write_text(
            f"{pid} (claude) " + " ".join(["S", "1"] + ["0"] * 17 + ["9"]),
            encoding="utf-8",
        )
        if open_path is not None:
            (directory / "fd" / "3").symlink_to(open_path)

    @requires_symlinks
    def test_detects_session_from_open_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = self._home(root)
            transcript = (
                home
                / "projects"
                / "-home-dev-project-alpha"
                / "11111111-1111-4111-8111-111111111111.jsonl"
            )
            proc_root = root / "proc"
            self._process(proc_root, 4242, transcript)

            sessions = list_claude_active_sessions(
                home,
                proc_root=proc_root,
                now=1_789_708_700.0,
            )

        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session.thread_id, "claude:11111111-1111-4111-8111-111111111111")
        self.assertEqual(session.session_id, "11111111-1111-4111-8111-111111111111")
        self.assertEqual(session.product, "claude")
        self.assertEqual(session.source, "claude-cli")
        self.assertEqual(session.project, "/home/dev/project-alpha")
        self.assertEqual(session.cwd, "/home/dev/project-alpha")
        self.assertEqual(session.model, "claude-sonnet-4-5")
        self.assertEqual(session.pids, (4242,))
        self.assertEqual(session.status.value, "running")
        self.assertEqual(session.jsonl_path, str(transcript))
        self.assertIsNotNone(session.started_at)
        self.assertIsNotNone(session.last_activity_at)

    @requires_symlinks
    def test_merges_processes_and_ignores_foreign_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = self._home(root)
            transcript = (
                home
                / "projects"
                / "-home-dev-project-alpha"
                / "11111111-1111-4111-8111-111111111111.jsonl"
            )
            outside = root / "other" / "notes.jsonl"
            outside.parent.mkdir(parents=True, exist_ok=True)
            outside.write_text("", encoding="utf-8")
            proc_root = root / "proc"
            self._process(proc_root, 100, transcript)
            self._process(proc_root, 101, transcript)
            self._process(proc_root, 102, outside)

            sessions = list_claude_active_sessions(
                home,
                proc_root=proc_root,
                now=1_789_708_700.0,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].pids, (100, 101))

    def test_no_process_falls_back_to_recent_file(self) -> None:
        """进程证据缺失时，刚被写过的 transcript 按 RECENT_FILE 兜底。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = self._home(root)
            proc_root = root / "proc"
            proc_root.mkdir()

            sessions = list_claude_active_sessions(
                home,
                proc_root=proc_root,
                now=time.time(),
            )

        self.assertEqual([item.confidence.value for item in sessions], ["recent_file"])
        self.assertEqual(sessions[0].product, "claude")

    def test_old_transcript_without_process_is_not_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = self._home(root)
            proc_root = root / "proc"
            proc_root.mkdir()
            for transcript in home.glob("projects/**/*.jsonl"):
                os.utime(transcript, (time.time() - 1800, time.time() - 1800))

            sessions = list_claude_active_sessions(
                home,
                proc_root=proc_root,
                now=time.time(),
            )

        self.assertEqual(sessions, ())

    @requires_symlinks
    def test_header_without_cwd_still_reports_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".claude"
            transcript = (
                home
                / "projects"
                / "-home-dev-unknown"
                / "22222222-2222-4222-8222-222222222222.jsonl"
            )
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(
                json.dumps({"type": "user", "message": "…"}) + "\n",
                encoding="utf-8",
            )
            proc_root = root / "proc"
            self._process(proc_root, 7, transcript)

            sessions = list_claude_active_sessions(
                home,
                proc_root=proc_root,
                now=1_789_708_700.0,
            )

        self.assertEqual(len(sessions), 1)
        self.assertIsNone(sessions[0].project)
        self.assertIsNone(sessions[0].model)

    def test_claude_session_id_from_open_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".claude"
            self._home(home)
            inside = (
                home / "projects" / "-home-dev-project-alpha" / "11111111-1111-4111-8111-11111111.jsonl"
            )
            agent = (
                home
                / "projects"
                / "-home-dev-project-alpha"
                / "11111111-1111-4111-8111-11111111"
                / "subagents"
                / "agent-abc.jsonl"
            )
            agent.parent.mkdir(parents=True, exist_ok=True)
            agent.write_text("", encoding="utf-8")
            outside = root / "elsewhere.jsonl"
            outside.write_text("", encoding="utf-8")

            root_projects = home / "projects"

        self.assertEqual(
            claude_session_id_from_open_path(inside, root_projects),
            "11111111-1111-4111-8111-11111111",
        )
        self.assertEqual(
            claude_session_id_from_open_path(agent, root_projects),
            "11111111-1111-4111-8111-11111111",
        )
        self.assertIsNone(claude_session_id_from_open_path(outside, root_projects))


class ClaudeUsageAggregatorTests(unittest.TestCase):
    """验证 Claude 用量进入按日/模型/项目/会话统计与成本估算。"""

    def _aggregator(self, root: Path) -> tuple[UsageAggregator, Path]:
        home = root / ".claude"
        session = _write_session(
            home,
            "-home-dev-project-alpha",
            "99999999-9999-4999-8999-999999999999",
            [
                _assistant_line(
                    timestamp="2026-05-01T10:00:00Z",
                    message_id="msg_01",
                    model="claude-sonnet-4-5",
                    input_tokens=100_000,
                    output_tokens=10_000,
                    cache_read=50_000,
                ),
                _assistant_line(
                    timestamp="2026-05-02T10:00:00Z",
                    message_id="msg_02",
                    model="claude-sonnet-4-5",
                    input_tokens=200_000,
                    output_tokens=20_000,
                ),
            ],
        )
        aggregator = UsageAggregator(
            discovery_interval=0.01,
            refresh_interval=0.01,
            cache_path=root / "state" / "usage-index.sqlite3",
            homes={
                "grok": (),
                "kimi": (),
                "dsh": (),
                "claude": (home,),
                "opencode": (),
                "cursor": (),
                "gemini": (),
                "qwen": (),
                "aider": (),
            },
        )
        aggregator.snapshot(
            {"codex": MultiSessionRegistry(root / "state")},
            account_metadata={},
            now=_timestamp("2026-05-02T12:00:00Z"),
        )
        return aggregator, session

    def test_snapshot_includes_claude_usage_and_cost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            aggregator, _ = self._aggregator(Path(temporary_directory))

            snapshot = aggregator.snapshot(
                {"codex": MultiSessionRegistry(Path(temporary_directory) / "state")},
                account_metadata={},
                now=_timestamp("2026-05-02T12:00:00Z"),
            )
            search = aggregator.search(since=0.0, group="session", limit=50)

        claude_accounts = [
            account
            for period in snapshot["periods"]
            for account in period["accounts"]
            if "claude" in json.dumps(account, ensure_ascii=False)
        ]
        self.assertTrue(claude_accounts)
        # 默认按「日期 + 会话 + 模型」分组，跨天的会话会分成两行
        session_rows = [
            row
            for row in search["rows"]
            if row["session_id"] == "99999999-9999-4999-8999-999999999999"
        ]
        self.assertEqual(len(session_rows), 2)
        self.assertEqual({row["project"] for row in session_rows}, {"/home/dev/project-alpha"})
        self.assertEqual(sum(row["total_tokens"] for row in session_rows), 380_000)
        self.assertEqual(sum(row["records"] for row in session_rows), 2)
        # 100k 非缓存输入 + 50k 缓存读 + 10k 输出（Sonnet: 3 / 0.3 / 15）
        expected = (100_000 * 3 + 50_000 * 0.3 + 10_000 * 15) / 1_000_000
        expected += (200_000 * 3 + 20_000 * 15) / 1_000_000
        actual = sum(row["estimated_cost_usd"] for row in session_rows)
        self.assertAlmostEqual(actual, round(expected, 6), places=5)

    def test_index_checkpoint_avoids_replaying_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            aggregator, session = self._aggregator(root)
            second = UsageAggregator(
                discovery_interval=0.01,
                refresh_interval=0.01,
                cache_path=root / "state" / "usage-index.sqlite3",
                homes={
                    "grok": (),
                    "kimi": (),
                    "dsh": (),
                    "claude": (root / ".claude",),
                    "opencode": (),
                    "cursor": (),
                    "gemini": (),
                    "qwen": (),
                    "aider": (),
                },
            )
            snapshot = second.snapshot(
                {"codex": MultiSessionRegistry(root / "state")},
                account_metadata={},
                now=_timestamp("2026-05-02T13:00:00Z"),
            )
            loaded = second._persistent.load(session) if second._persistent else None

        self.assertTrue(snapshot["indexing"]["complete"])
        self.assertEqual(snapshot["indexing"]["pending_files"], 0)
        # 中文注释：检查点命中时不再重读文件内容
        self.assertEqual(snapshot["indexing"]["read_bytes_this_refresh"], 0)
        self.assertIsNotNone(loaded)
        self.assertIsNotNone(loaded.next_offset)
        self.assertEqual(len(loaded.total_deltas), 2)

    def test_file_without_trailing_newline_still_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".claude"
            session = _write_session(
                home,
                "-home-dev-project-alpha",
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                [_assistant_line(timestamp="2026-05-01T10:00:00Z")],
            )
            session.write_text(
                session.read_text(encoding="utf-8").rstrip("\n"),
                encoding="utf-8",
            )
            aggregator = UsageAggregator(
                discovery_interval=0.01,
                refresh_interval=0.01,
                cache_path=root / "state" / "usage-index.sqlite3",
                homes={
                    "grok": (),
                    "kimi": (),
                    "dsh": (),
                    "claude": (home,),
                    "opencode": (),
                    "cursor": (),
                    "gemini": (),
                    "qwen": (),
                    "aider": (),
                },
            )

            snapshot = aggregator.snapshot(
                {"codex": MultiSessionRegistry(root / "state")},
                account_metadata={},
                now=_timestamp("2026-05-02T12:00:00Z"),
            )

        self.assertTrue(snapshot["indexing"]["complete"])
        self.assertTrue(snapshot["periods"])

    def test_anthropic_models_are_priced(self) -> None:
        for model in (
            "claude-opus-4-1-20250805",
            "claude-sonnet-4-5-20250929",
            "claude-haiku-4-5",
            "claude-3-5-haiku-20241022",
        ):
            self.assertIsNotNone(_lookup_pricing(model), model)

    def test_claude_models_use_official_api_prices(self) -> None:
        # 中文注释：(输入, 缓存读取, 输出) 美元 / 百万 token，取自 Anthropic 官方价格页。
        official = {
            "claude-fable-5-1": (10, 0.25, 50),
            "claude-fable-5": (10, 1, 50),
            "claude-opus-5-5": (4, 0.2, 20),
            "claude-opus-5": (5, 0.5, 25),
            "claude-opus-4-8": (5, 0.5, 25),
            "claude-opus-4-7": (5, 0.5, 25),
            "claude-opus-4-6": (5, 0.5, 25),
            "claude-opus-4-5-20251101": (5, 0.5, 25),
            "claude-opus-4-1-20250805": (15, 1.5, 75),
            "claude-sonnet-5-5": (2, 0.1, 10),
            "claude-sonnet-5": (2, 0.2, 10),
            "claude-sonnet-4-6": (3, 0.3, 15),
            "claude-haiku-5-5": (0.1, 0.01, 0.5),
            "claude-haiku-4-5": (1, 0.1, 5),
        }
        for model, prices in official.items():
            with self.subTest(model=model):
                pricing = _lookup_pricing(model)
                self.assertIsNotNone(pricing)
                self.assertEqual(
                    (pricing.input_usd, pricing.cached_input_usd, pricing.output_usd),
                    prices,
                )

    def test_claude_long_context_rules(self) -> None:
        def cost(model: str, input_tokens: int, output_tokens: int) -> float:
            usage = TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            )
            return _estimate_usage(usage, model)["estimated_cost_usd"]

        # 中文注释：4.6 及之后在 1M 上下文内不加价（超过全局 272K 阈值也一样）。
        self.assertEqual(cost("claude-opus-5-5", 1_000_000, 1_000_000), 24.0)
        self.assertEqual(cost("claude-opus-4-8", 1_000_000, 0), 5.0)
        # 中文注释：Haiku 5.5 提示超过 100K 时全部单价 ×5。
        self.assertEqual(cost("claude-haiku-5-5", 100_000, 0), 0.01)
        self.assertEqual(cost("claude-haiku-5-5", 150_000, 10_000), 0.1)
        # 中文注释：Sonnet 4.5 超过 200K 输入时输入 2×、输出 1.5×。
        self.assertEqual(cost("claude-sonnet-4-5", 300_000, 10_000), 2.025)


def _timestamp(value: str) -> float:
    """把 ISO8601 时间转成 Unix 秒。"""

    from datetime import datetime, timezone

    return (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


class ClaudeLongLineTests(unittest.TestCase):
    """长于单轮读取预算、但未超过单行上限的记录不能让增量读取停在原地。"""

    def test_line_longer_than_budget_does_not_stall_the_reader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            transcript = Path(temporary_directory) / "session.jsonl"
            image_line = json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-08-27T01:00:00Z",
                    "message": {"role": "user", "content": "x" * 1_500_000},
                }
            )
            transcript.write_text(
                image_line
                + "\n"
                + _assistant_line(timestamp="2026-08-27T01:00:05Z", message_id="msg_after")
                + "\n",
                encoding="utf-8",
            )
            offset = 0
            discarding = False
            events = []
            for _ in range(5):
                result = parse_claude_chunk(
                    transcript,
                    offset,
                    discarding_oversized_line=discarding,
                    maximum_bytes=1024 * 1024,
                )
                events.extend(result.events)
                self.assertGreater(result.next_offset, offset, "偏移量必须前进")
                offset, discarding = result.next_offset, result.discarding_oversized_line
                if offset >= transcript.stat().st_size:
                    break
            size = transcript.stat().st_size

        self.assertEqual(offset, size)
        self.assertEqual([event.message_id for event in events], ["msg_after"])


if __name__ == "__main__":
    unittest.main()


class ClaudeQuotaTest(unittest.TestCase):
    """Claude Code OAuth usage 接口的额度读取（网络与 Keychain 全部注入/打桩）。"""

    def setUp(self) -> None:
        claude_module._clear_quota_cache()
        claude_module._clear_quota_backoff()
        # 中文注释：任何遗留测试也不能触发真实 OAuth 续期请求。
        post_patch = mock.patch.object(
            claude_module, "_http_post_json", return_value=(0, None)
        )
        self.default_post = post_patch.start()
        self.addCleanup(post_patch.stop)

    def tearDown(self) -> None:
        claude_module._clear_quota_cache()
        claude_module._clear_quota_backoff()

    def _home_with_credentials(self, root: Path) -> Path:
        home = root / ".claude"
        home.mkdir()
        (home / ".credentials.json").write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "test-token-abc",
                        "refreshToken": "test-refresh",
                        "expiresAt": 1_900_000_000_000,
                        "subscriptionType": "max",
                    }
                }
            ),
            encoding="utf-8",
        )
        return home

    def test_parses_all_windows_and_plan(self) -> None:
        """完整返回应解析出 5 小时、周与 Claude Design 三个窗口及套餐名。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            home = self._home_with_credentials(Path(temporary_directory))
            calls: list[tuple[str, dict[str, str]]] = []

            def fake_get(
                url: str,
                headers: Mapping[str, str],
                timeout: float,
            ) -> tuple[int, object]:
                calls.append((url, dict(headers)))
                return 200, {
                    "five_hour": {
                        "utilization": 21.0,
                        "resets_at": "2026-09-24T15:00:00Z",
                    },
                    "seven_day": {
                        "utilization": 74,
                        "resets_at": "2026-09-28T00:00:00Z",
                    },
                    "seven_day_omelette": {"utilization": 7.0},
                }

            snapshot = read_claude_quota(home, now=1000.0, http_get_json=fake_get)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.plan_type, "max")
        self.assertEqual(snapshot.source, "claude-oauth")
        self.assertEqual(len(snapshot.windows), 3)
        five_hour, weekly, design = snapshot.windows
        self.assertEqual((five_hour.name, five_hour.window_minutes), ("5-hour", 300.0))
        self.assertEqual(five_hour.used_percent, 21.0)
        self.assertEqual(
            five_hour.resets_at,
            datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc).timestamp(),
        )
        self.assertEqual((weekly.name, weekly.window_minutes), ("Weekly", 10080.0))
        self.assertEqual(weekly.used_percent, 74.0)
        self.assertEqual(design.limit_id, "claude-design")
        self.assertEqual(design.used_percent, 7.0)
        # token 只能出现在 Authorization 头里，且带了正确的 beta 标记。
        self.assertEqual(len(calls), 1)
        url, headers = calls[0]
        self.assertIn("/api/oauth/usage", url)
        self.assertEqual(headers["Authorization"], "Bearer test-token-abc")
        self.assertEqual(headers["anthropic-beta"], "oauth-2025-04-20")
        self.assertNotIn("test-token-abc", repr(snapshot))

    def test_partial_payload_returns_available_windows(self) -> None:
        """只有 five_hour 时也能返回单窗口快照。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            home = self._home_with_credentials(Path(temporary_directory))

            def fake_get(
                url: str,
                headers: Mapping[str, str],
                timeout: float,
            ) -> tuple[int, object]:
                return 200, {"five_hour": {"utilization": 55.5}}

            snapshot = read_claude_quota(home, now=1000.0, http_get_json=fake_get)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(len(snapshot.windows), 1)
        self.assertEqual(snapshot.windows[0].used_percent, 55.5)

    def test_missing_credentials_returns_none_without_http(self) -> None:
        """没有凭据文件时直接返回 None，不发起请求。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / ".claude"
            home.mkdir()
            called = False

            def fake_get(
                url: str,
                headers: Mapping[str, str],
                timeout: float,
            ) -> tuple[int, object]:
                nonlocal called
                called = True
                return 200, {}

            snapshot = read_claude_quota(
                home,
                now=1000.0,
                http_get_json=fake_get,
                keychain_reader=lambda: None,
            )

        self.assertIsNone(snapshot)
        self.assertFalse(called)

    def test_http_error_returns_none(self) -> None:
        """没有历史成功值时，请求或续期失败返回 None。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            home = self._home_with_credentials(Path(temporary_directory))
            for status in (401, 403, 429, 500):
                with self.subTest(status=status):
                    claude_module._clear_quota_backoff()
                    snapshot = read_claude_quota(
                        home,
                        now=1000.0,
                        http_get_json=lambda url, headers, timeout: (status, {}),
                    )
                    self.assertIsNone(snapshot)

    def test_keychain_fallback_when_credentials_file_missing(self) -> None:
        """macOS 上没有凭据文件时用注入的 Keychain 读取器拿 token。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / ".claude"
            home.mkdir()
            keychain_calls = 0

            def fake_keychain() -> Mapping[str, object]:
                nonlocal keychain_calls
                keychain_calls += 1
                return {
                    "claudeAiOauth": {
                        "accessToken": "keychain-token",
                        "subscriptionType": "pro",
                    }
                }

            def fake_get(
                url: str,
                headers: Mapping[str, str],
                timeout: float,
            ) -> tuple[int, object]:
                assert headers["Authorization"] == "Bearer keychain-token"
                return 200, {"five_hour": {"utilization": 10.0}}

            snapshot = read_claude_quota(
                home,
                now=1000.0,
                http_get_json=fake_get,
                keychain_reader=fake_keychain,
            )

        self.assertEqual(keychain_calls, 1)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.plan_type, "pro")

    def test_real_path_caches_success_and_failure(self) -> None:
        """真实 HTTP 路径走缓存：成功 5 分钟、失败 1 分钟内不重复请求。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            home = self._home_with_credentials(Path(temporary_directory))
            responses = iter(
                [
                    (200, {"five_hour": {"utilization": 30.0}}),
                    (200, {"five_hour": {"utilization": 99.0}}),
                    (500, {}),
                    (500, {}),
                ]
            )
            http_calls = 0

            def fake_http(
                url: str,
                headers: Mapping[str, str],
                timeout: float,
            ) -> tuple[int, object]:
                nonlocal http_calls
                http_calls += 1
                return next(responses)

            with (
                mock.patch.object(
                    claude_module, "_http_get_json", side_effect=fake_http
                ),
                mock.patch.object(
                    claude_module, "_read_macos_keychain", return_value=None
                ),
            ):
                first = read_claude_quota(home, now=1000.0)
                second = read_claude_quota(home, now=1000.0 + 299.0)
                third = read_claude_quota(home, now=1000.0 + 301.0)

                self.assertEqual(http_calls, 2)
                assert first is not None and second is not None and third is not None
                self.assertEqual(first.windows[0].used_percent, 30.0)
                self.assertEqual(second.windows[0].used_percent, 30.0)
                self.assertEqual(third.windows[0].used_percent, 99.0)

                # 第三次成功缓存到 1301+300=1601 才过期，之后才会重新请求并失败。
                still_cached = read_claude_quota(home, now=1000.0 + 400.0)
                assert still_cached is not None
                self.assertEqual(still_cached.windows[0].used_percent, 99.0)
                self.assertEqual(http_calls, 2)

                failed = read_claude_quota(home, now=1000.0 + 602.0)
                assert failed is not None
                self.assertEqual(failed.observed_at, third.observed_at)
                self.assertEqual(failed.metadata["stale_reason"], "unavailable")
                self.assertEqual(http_calls, 3)
                cached_failure = read_claude_quota(home, now=1000.0 + 661.0)
                self.assertEqual(cached_failure, failed)
                self.assertEqual(http_calls, 3)
                retried = read_claude_quota(home, now=1000.0 + 663.0)
                self.assertEqual(retried, failed)
                self.assertEqual(http_calls, 4)

    def _set_oauth(self, home: Path, **changes: object) -> dict[str, object]:
        """修改合成凭据，返回完整 JSON 以验证未知字段保留。"""

        path = home / ".credentials.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["claudeAiOauth"].update(changes)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def test_valid_token_does_not_refresh(self) -> None:
        """有效 token 或缺失到期时间时，不能发起续期。"""

        with tempfile.TemporaryDirectory() as temporary:
            home = self._home_with_credentials(Path(temporary))
            post = mock.Mock()
            get = mock.Mock(return_value=(200, {"five_hour": {"utilization": 20}}))
            for expiry in (1_900_000_000_000, None):
                self._set_oauth(home, expiresAt=expiry)
                result = read_claude_quota(
                    home, now=1000, http_get_json=get, http_post_json=post
                )
                self.assertIsNotNone(result)
            post.assert_not_called()

    def test_refresh_preserves_payload_and_uses_new_token(self) -> None:
        """提前续期，轮换凭据后原子写回，保留所有非凭据字段。"""

        with tempfile.TemporaryDirectory() as temporary:
            home = self._home_with_credentials(Path(temporary))
            original = self._set_oauth(
                home, expiresAt=1_200_000, scopes=["user:profile"]
            )
            original["other"] = {"retained": True}
            path = home / ".credentials.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            post = mock.Mock(
                return_value=(
                    200,
                    {
                        "access_token": "example-new-access",
                        "refresh_token": "example-new-refresh",
                        "expires_in": 3600,
                    },
                )
            )
            get = mock.Mock(return_value=(200, {"five_hour": {"utilization": 20}}))
            with self.assertLogs(claude_module.__name__, level="INFO") as captured:
                result = read_claude_quota(
                    home, now=1000, http_get_json=get, http_post_json=post
                )
            self.assertIsNotNone(result)
            post.assert_called_once_with(
                claude_module._CLAUDE_TOKEN_URL,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": "test-refresh",
                    "client_id": claude_module._CLAUDE_CLIENT_ID,
                },
                10.0,
            )
            self.assertEqual(
                get.call_args.args[1]["Authorization"], "Bearer example-new-access"
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
            expected = json.loads(json.dumps(original))
            expected["claudeAiOauth"].update(
                accessToken="example-new-access",
                refreshToken="example-new-refresh",
                expiresAt=4_600_000,
            )
            self.assertEqual(saved, expected)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(home.glob("*.tmp")), [])
            self.assertFalse(home.with_name(home.name + ".lock").exists())
            for token in (
                "test-token-abc",
                "test-refresh",
                "example-new-access",
                "example-new-refresh",
            ):
                self.assertNotIn(token, repr(result))
                self.assertNotIn(token, "\n".join(captured.output))

    def test_refresh_without_rotated_refresh_token_keeps_previous(self) -> None:
        """接口省略 refresh_token 时继续保留旧值。"""

        with tempfile.TemporaryDirectory() as temporary:
            home = self._home_with_credentials(Path(temporary))
            self._set_oauth(home, expiresAt=1_000_000)
            result = read_claude_quota(
                home,
                now=1000,
                http_get_json=mock.Mock(
                    return_value=(200, {"five_hour": {"utilization": 10}})
                ),
                http_post_json=mock.Mock(
                    return_value=(
                        200,
                        {
                            "access_token": "example-new-access",
                            "expires_in": 3600,
                        },
                    )
                ),
            )
            self.assertIsNotNone(result)
            saved = json.loads((home / ".credentials.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["claudeAiOauth"]["refreshToken"], "test-refresh")

    def test_rechecks_credentials_after_acquiring_lock(self) -> None:
        """锁等待期间另一进程已刷新时，直接使用磁盘上的新 token。"""

        with tempfile.TemporaryDirectory() as temporary:
            home = self._home_with_credentials(Path(temporary))
            self._set_oauth(home, expiresAt=1_000_000)
            acquire = claude_module._ClaudeRefreshLock.acquire

            def acquire_and_update(lock: claude_module._ClaudeRefreshLock) -> bool:
                held = acquire(lock)
                self._set_oauth(
                    home, accessToken="example-concurrent-access", expiresAt=9_000_000
                )
                return held

            post = mock.Mock()
            get = mock.Mock(return_value=(200, {"five_hour": {"utilization": 10}}))
            with mock.patch.object(
                claude_module._ClaudeRefreshLock, "acquire", acquire_and_update
            ):
                result = read_claude_quota(
                    home, now=1000, http_get_json=get, http_post_json=post
                )
            self.assertIsNotNone(result)
            post.assert_not_called()
            self.assertEqual(
                get.call_args.args[1]["Authorization"],
                "Bearer example-concurrent-access",
            )

    def test_failed_refresh_keeps_file_and_returns_stale_quota(self) -> None:
        """明确失败和异常响应均不重试、不改文件，返回带凭据原因的旧额度。"""

        responses = [
            (400, {}),
            (401, {}),
            (429, {}),
            (500, {}),
            (200, {}),
            (200, {"access_token": "example-new-access", "expires_in": -1}),
        ]
        for response in responses:
            with (
                self.subTest(response=response),
                tempfile.TemporaryDirectory() as temporary,
            ):
                home = self._home_with_credentials(Path(temporary))
                get = mock.Mock(return_value=(200, {"five_hour": {"utilization": 30}}))
                good = read_claude_quota(home, now=1000, http_get_json=get)
                self._set_oauth(home, expiresAt=1_200_000)
                path = home / ".credentials.json"
                original = path.read_bytes()
                post = mock.Mock(return_value=response)
                stale = read_claude_quota(
                    home, now=1100, http_get_json=get, http_post_json=post
                )
                assert good is not None and stale is not None
                self.assertEqual(stale.observed_at, good.observed_at)
                self.assertEqual(
                    stale.metadata,
                    {"stale": "1", "stale_reason": "credentials_expired"},
                )
                self.assertEqual(good.metadata, {})
                self.assertEqual(path.read_bytes(), original)
                post.assert_called_once()
                self.assertEqual(get.call_count, 1)

    def test_unavailable_lock_does_not_refresh(self) -> None:
        """别的进程持有新锁时，放弃本轮且不删除别人的锁。"""

        with tempfile.TemporaryDirectory() as temporary:
            home = self._home_with_credentials(Path(temporary))
            self._set_oauth(home, expiresAt=1_000_000)
            lock_dir = home.with_name(home.name + ".lock")
            lock_dir.mkdir()
            post, get = mock.Mock(), mock.Mock()
            result = read_claude_quota(
                home,
                now=1000,
                http_get_json=get,
                http_post_json=post,
                sleep=lambda _: None,
            )
            self.assertIsNone(result)
            post.assert_not_called()
            get.assert_not_called()
            self.assertTrue(lock_dir.is_dir())

    def test_keychain_and_disabled_refresh_are_read_only(self) -> None:
        """Keychain 过期凭据以及显式关闭续期均不触发写入或 POST。"""

        with tempfile.TemporaryDirectory() as temporary:
            home = self._home_with_credentials(Path(temporary))
            payload = self._set_oauth(home, expiresAt=1_000_000)
            post = mock.Mock()
            get = mock.Mock(return_value=(200, {"five_hour": {"utilization": 10}}))
            result = read_claude_quota(
                home,
                now=1000,
                http_get_json=get,
                http_post_json=post,
                allow_refresh=False,
            )
            self.assertIsNotNone(result)
            path = home / ".credentials.json"
            path.unlink()
            result = read_claude_quota(
                home,
                now=1100,
                http_get_json=get,
                http_post_json=post,
                keychain_reader=lambda: payload,
            )
            self.assertIsNotNone(result)
            self.assertFalse(path.exists())
            post.assert_not_called()

    def test_401_forces_only_one_refresh_and_retries_usage_once(self) -> None:
        """未到期 token 被撤销时仅强制续期一次，重发后仍失败则返回旧额度。"""

        for final_status in (200, 401, 403):
            with (
                self.subTest(status=final_status),
                tempfile.TemporaryDirectory() as temporary,
            ):
                home = self._home_with_credentials(Path(temporary))
                get = mock.Mock(
                    side_effect=[
                        (401, {}),
                        (final_status, {"five_hour": {"utilization": 10}}),
                    ]
                )
                post = mock.Mock(
                    return_value=(
                        200,
                        {
                            "access_token": "example-new-access",
                            "expires_in": 3600,
                        },
                    )
                )
                result = read_claude_quota(
                    home, now=1000, http_get_json=get, http_post_json=post
                )
                self.assertEqual(get.call_count, 2)
                post.assert_called_once()
                self.assertEqual(
                    get.call_args.args[1]["Authorization"], "Bearer example-new-access"
                )
                self.assertEqual(result is not None, final_status == 200)

    def test_preemptive_refresh_is_not_repeated_after_401(self) -> None:
        """主动续期后收到 401 也不能对轮换凭据再续期。"""

        with tempfile.TemporaryDirectory() as temporary:
            home = self._home_with_credentials(Path(temporary))
            self._set_oauth(home, expiresAt=1_000_000)
            post = mock.Mock(
                return_value=(
                    200,
                    {
                        "access_token": "example-new-access",
                        "expires_in": 3600,
                    },
                )
            )
            get = mock.Mock(return_value=(401, {}))
            self.assertIsNone(
                read_claude_quota(
                    home, now=1000, http_get_json=get, http_post_json=post
                )
            )
            get.assert_called_once()
            post.assert_called_once()

    def test_stale_quota_expires_and_drops_reset_windows(self) -> None:
        """旧快照保留原时间，逐个丢弃过期窗口，超过 6 小时不再显示。"""

        with tempfile.TemporaryDirectory() as temporary:
            home = self._home_with_credentials(Path(temporary))
            get = mock.Mock(
                return_value=(
                    200,
                    {
                        "five_hour": {"utilization": 30, "resets_at": 1100},
                        "seven_day": {"utilization": 40, "resets_at": 50_000},
                    },
                )
            )
            good = read_claude_quota(home, now=1000, http_get_json=get)
            get.return_value = (500, {})
            stale = read_claude_quota(home, now=1100, http_get_json=get)
            assert good is not None and stale is not None
            self.assertEqual(stale.observed_at, 1000)
            self.assertEqual([w.name for w in stale.windows], ["Weekly"])
            self.assertEqual(len(good.windows), 2)
            self.assertIsNotNone(read_claude_quota(home, now=22_600, http_get_json=get))
            self.assertIsNone(read_claude_quota(home, now=22_601, http_get_json=get))
            self.assertIsNone(
                claude_module._stale_claude_quota(
                    home.resolve(),
                    50_000,
                    claude_module._QuotaFailure.UNAVAILABLE,
                )
            )

    def test_stale_with_all_windows_reset_returns_none(self) -> None:
        """最后一个窗口已重置时，即使快照未满 6 小时也应移除。"""

        with tempfile.TemporaryDirectory() as temporary:
            home = self._home_with_credentials(Path(temporary))
            get = mock.Mock(
                return_value=(
                    200,
                    {"five_hour": {"utilization": 30, "resets_at": 1100}},
                )
            )
            self.assertIsNotNone(read_claude_quota(home, now=1000, http_get_json=get))
            get.return_value = (500, {})
            self.assertIsNone(read_claude_quota(home, now=1100, http_get_json=get))

    def test_429_backoff_survives_cache_clear_and_is_per_home(self) -> None:
        """429 按 10、20、30 分钟退避；手动刷新无效，账号间互不影响。"""

        with tempfile.TemporaryDirectory() as temporary:
            home = self._home_with_credentials(Path(temporary))
            get = mock.Mock(return_value=(200, {"five_hour": {"utilization": 30}}))
            good = read_claude_quota(home, now=900, http_get_json=get)
            get.return_value = (429, {})
            for failed_at, delay in (
                (1000, 600),
                (1600, 1200),
                (2800, 1800),
                (4600, 1800),
            ):
                stale = read_claude_quota(home, now=failed_at, http_get_json=get)
                assert stale is not None and good is not None
                self.assertEqual(stale.observed_at, good.observed_at)
                self.assertEqual(stale.metadata["stale_reason"], "rate_limited")
                calls = get.call_count
                claude_module._clear_quota_cache()
                during = read_claude_quota(
                    home, now=failed_at + delay - 1, http_get_json=get
                )
                self.assertEqual(during, stale)
                self.assertEqual(get.call_count, calls)
            other = Path(temporary) / "other"
            other.mkdir()
            other_home = self._home_with_credentials(other)
            get.return_value = (200, {"five_hour": {"utilization": 40}})
            self.assertIsNotNone(
                read_claude_quota(other_home, now=5000, http_get_json=get)
            )
            recovered = read_claude_quota(home, now=6400, http_get_json=get)
            assert recovered is not None
            self.assertEqual(recovered.metadata, {})
            self.assertNotIn(home.resolve(), claude_module._backoff_until)

    def test_errors_do_not_leak_tokens_and_log_only_once(self) -> None:
        """异常正文可含 token，日志只能保留类型且相同失败只记一次。"""

        with tempfile.TemporaryDirectory() as temporary:
            home = self._home_with_credentials(Path(temporary))
            self._set_oauth(home, expiresAt=1_000_000)
            post = mock.Mock(side_effect=OSError("test-token-abc test-refresh"))
            with self.assertLogs(claude_module.__name__, level="INFO") as captured:
                for now in (1000, 1100):
                    self.assertIsNone(
                        read_claude_quota(
                            home,
                            now=now,
                            http_get_json=mock.Mock(),
                            http_post_json=post,
                        )
                    )
            logs = "\n".join(captured.output)
            self.assertEqual(logs.count("refresh_failed"), 1)
            self.assertEqual(logs.count("credentials_expired"), 1)
            self.assertNotIn("test-token-abc", logs)
            self.assertNotIn("test-refresh", logs)


class ClaudeRefreshLockTest(unittest.TestCase):
    """合成目录验证 proper-lockfile 路径、锁保活和丢失后的失败关闭。"""

    def test_stale_lock_is_reclaimed(self) -> None:
        """陈旧空目录锁可以清理，新锁释放后不应留下目录。"""

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".claude"
            home.mkdir()
            lock_dir = home.with_name(home.name + ".lock")
            lock_dir.mkdir()
            os.utime(lock_dir, (1, 1))
            lock = claude_module._ClaudeRefreshLock(home, lambda _: None, retries=2)
            try:
                self.assertTrue(lock.acquire())
                self.assertTrue(lock.touch())
            finally:
                lock.release()
            self.assertFalse(lock_dir.exists())

    def test_peer_with_default_heartbeat_is_not_stale(self) -> None:
        """proper-lockfile 默认 5 秒心跳有轻微延迟时不能按 Kimi 的阈值抢锁。"""

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".claude"
            home.mkdir()
            lock_dir = home.with_name(home.name + ".lock")
            lock_dir.mkdir()
            now = time.time()
            os.utime(lock_dir, (now - 6, now - 6))
            lock = claude_module._ClaudeRefreshLock(home, lambda _: None, retries=2)
            self.assertFalse(lock.acquire())
            lock.release()
            self.assertTrue(lock_dir.is_dir())

    def test_lost_lock_does_not_delete_replacement(self) -> None:
        """锁被替换后原持有者不能续期或删除新持有者的目录。"""

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".claude"
            home.mkdir()
            lock = claude_module._ClaudeRefreshLock(home, lambda _: None, retries=1)
            self.assertTrue(lock.acquire())
            lock_dir = home.with_name(home.name + ".lock")
            lock_dir.rename(lock_dir.with_name("old.lock"))
            lock_dir.mkdir()
            self.assertFalse(lock.touch())
            lock.release()
            self.assertTrue(lock_dir.is_dir())

    def test_heartbeat_renews_until_ownership_is_lost(self) -> None:
        """保活循环更新锁，发现锁丢失就退出。"""

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".claude"
            lock = claude_module._ClaudeRefreshLock(home, lambda _: None)
            with (
                mock.patch.object(lock, "touch", side_effect=[True, False]) as touch,
                mock.patch.object(
                    lock._stop,
                    "wait",
                    return_value=False,
                ),
            ):
                lock._renew()
            self.assertEqual(touch.call_count, 2)


class ClaudeHttpPostTest(unittest.TestCase):
    """验证 JSON POST、超时和响应大小上限，不访问真实服务。"""

    def test_json_post_and_response_limit(self) -> None:
        """续期使用 JSON POST，超大响应不交给解析器。"""

        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"expires_in":3600}'
        with mock.patch.object(
            claude_module.urllib.request, "urlopen", return_value=response
        ) as opened:
            self.assertEqual(
                claude_module._http_post_json(
                    "https://example.com/oauth/token",
                    {"refresh_token": "example-refresh"},
                    10,
                ),
                (200, {"expires_in": 3600}),
            )
            request = opened.call_args.args[0]
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(
                json.loads(request.data), {"refresh_token": "example-refresh"}
            )
            self.assertEqual(request.get_header("Content-type"), "application/json")
            self.assertEqual(opened.call_args.kwargs["timeout"], 10)
            response.read.return_value = b"x" * (
                claude_module._CLAUDE_MAX_RESPONSE_BYTES + 1
            )
            self.assertEqual(
                claude_module._http_post_json(
                    "https://example.com/oauth/token", {}, 10
                ),
                (200, None),
            )

    def test_atomic_replace_failure_keeps_original_and_cleans_tempfile(self) -> None:
        """替换失败时保留原凭据字节，临时凭据不能遗留在目录中。"""

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = home / ".credentials.json"
            original = b'{"claudeAiOauth":{"accessToken":"example-original"}}'
            path.write_bytes(original)
            with mock.patch.object(
                Path, "replace", side_effect=OSError("example-error")
            ):
                with self.assertRaises(OSError):
                    claude_module._write_claude_credentials(home, {"example": "value"})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(home.iterdir()), [path])

    def test_network_error_is_unavailable(self) -> None:
        """网络错误收敛为无响应，不向调用者泄漏异常正文。"""

        with mock.patch.object(
            claude_module.urllib.request,
            "urlopen",
            side_effect=OSError("example-refresh"),
        ):
            self.assertEqual(
                claude_module._http_post_json(
                    "https://example.com/oauth/token", {}, 10
                ),
                (0, None),
            )
