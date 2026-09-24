"""Claude Code 用量索引测试（使用脱敏合成样本）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from _platform_support import requires_symlinks
from token_monitor.claude import (
    claude_home_for,
    claude_has_inline_sidechains,
    claude_session_id,
    claude_session_id_from_open_path,
    list_claude_active_sessions,
    list_claude_transcripts,
    main_transcripts,
    parse_claude_chunk,
    read_claude_account,
    resolve_claude_homes,
    resolve_sidechain_policy,
    subagent_transcripts,
)
from token_monitor.registry import MultiSessionRegistry
from token_monitor.usage import UsageAggregator, _lookup_pricing


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

    def test_no_process_means_no_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = self._home(root)
            proc_root = root / "proc"
            proc_root.mkdir()

            sessions = list_claude_active_sessions(
                home,
                proc_root=proc_root,
                now=1_789_708_700.0,
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
            grok_homes=(),
            kimi_homes=(),
            dsh_homes=(),
            claude_homes=(home,),
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
                grok_homes=(),
                kimi_homes=(),
                dsh_homes=(),
                claude_homes=(root / ".claude",),
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
                grok_homes=(),
                kimi_homes=(),
                dsh_homes=(),
                claude_homes=(home,),
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


def _timestamp(value: str) -> float:
    """把 ISO8601 时间转成 Unix 秒。"""

    from datetime import datetime, timezone

    return (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


if __name__ == "__main__":
    unittest.main()
