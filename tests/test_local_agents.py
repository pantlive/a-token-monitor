"""OpenCode、Cursor、Gemini CLI、Qwen Code 与 Aider 的用量、清理和扫描目录。"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from _platform_support import requires_symlinks
from a_token_monitor.housekeeping import (
    AuditTarget,
    default_sessions_root,
    scan_session_files,
)
from a_token_monitor.local_agents import (
    _path_slugs,
    list_aider_active_sessions,
    list_cursor_active_sessions,
    list_gemini_active_sessions,
    list_qwen_active_sessions,
    parse_aider_chunk,
    parse_chat_chunk,
    read_opencode_usage,
)
from a_token_monitor.multi_account import external_active_session_paths
from a_token_monitor.scan_dirs import validate_directory
from a_token_monitor.usage import UsageAggregator


_MOMENT = 1_780_000_000.0


def _today_account(snapshot: dict) -> dict:
    today = next(item for item in snapshot["periods"] if item["key"] == "today")
    return today["accounts"][0]


def _gemini_record(
    kind: str,
    message_id: str,
    tokens: dict[str, int],
) -> dict[str, object]:
    return {
        "type": kind,
        "id": message_id,
        "timestamp": _MOMENT,
        "model": "gemini-test",
        "tokens": tokens,
    }


class OpenCodeUsageTests(unittest.TestCase):
    def test_counts_assistant_tokens_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "opencode"
            home.mkdir()
            database = home / "opencode.db"
            secret = "SUPER-SECRET-TOKEN"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE session (
                    id TEXT PRIMARY KEY,
                    directory TEXT,
                    time_created INTEGER,
                    time_updated INTEGER
                );
                CREATE TABLE message (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    time_created INTEGER,
                    time_updated INTEGER,
                    data TEXT
                );
                CREATE TABLE credential (
                    id TEXT PRIMARY KEY,
                    secret TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?)",
                ("s1", "/work/app", 1, 1),
            )
            connection.execute(
                "INSERT INTO credential VALUES (?, ?)",
                ("c1", secret),
            )
            connection.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                (
                    "user-1",
                    "s1",
                    int(_MOMENT * 1000),
                    int(_MOMENT * 1000),
                    json.dumps(
                        {"role": "user", "content": f"tokens {secret}"},
                    ),
                ),
            )
            connection.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                (
                    "assistant-1",
                    "s1",
                    int(_MOMENT * 1000),
                    int(_MOMENT * 1000),
                    json.dumps(
                        {
                            "role": "assistant",
                            "modelID": "muse-spark",
                            "tokens": {
                                "input": 10,
                                "output": 4,
                                "reasoning": 1,
                                "cache": {"read": 2, "write": 3},
                            },
                        }
                    ),
                ),
            )
            connection.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                (
                    "assistant-zero",
                    "s1",
                    int(_MOMENT * 1000) + 1,
                    int(_MOMENT * 1000) + 1,
                    json.dumps(
                        {
                            "role": "assistant",
                            "modelID": "muse-spark",
                            "tokens": {
                                "input": 0,
                                "output": 0,
                                "reasoning": 0,
                                "cache": {"read": 0, "write": 0},
                            },
                        }
                    ),
                ),
            )
            connection.commit()
            connection.close()

            events = read_opencode_usage(database)
            self.assertIsNotNone(events)
            assert events is not None
            rendered = repr(events)
            self.assertNotIn(secret, rendered)
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event.input_tokens, 15)
            self.assertEqual(event.cached_input_tokens, 2)
            self.assertEqual(event.cache_write_input_tokens, 3)
            self.assertEqual(event.output_tokens, 5)
            self.assertEqual(event.reasoning_output_tokens, 1)
            self.assertEqual(event.total_tokens, 20)
            self.assertEqual(event.project, "/work/app")
            self.assertEqual(event.timestamp, _MOMENT)

            aggregator = UsageAggregator(opencode_homes=(home,))
            snapshot = aggregator.snapshot({}, now=_MOMENT)
            account = _today_account(snapshot)
            self.assertEqual(account["total_tokens"], 20)
            self.assertEqual(account["input_tokens"], 15)
            self.assertEqual(account["output_tokens"], 5)
            self.assertEqual(account["projects"][0]["project"], "/work/app")
            self.assertIsNone(read_opencode_usage(home / "missing.db"))


class CursorUsageTests(unittest.TestCase):
    def test_counts_only_real_usage_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "cursor"
            path = (
                home
                / "projects"
                / "my-slug"
                / "agent-transcripts"
                / "sess"
                / "turn.jsonl"
            )
            path.parent.mkdir(parents=True)
            lines = [
                {
                    "role": "assistant",
                    "message": {"content": ("x" * 400) + " tokens"},
                },
                {
                    "id": "a",
                    "usage": {
                        "input_tokens": 5,
                        "output_tokens": 3,
                        "reasoning_tokens": 9,
                    },
                },
                {
                    "id": "a",
                    "usage": {"input_tokens": 100, "output_tokens": 100},
                },
                {"usage": {"prompt_tokens": 7, "completion_tokens": 1}},
                {
                    "id": "zero",
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                    },
                },
            ]
            path.write_text(
                "".join(json.dumps(line) + "\n" for line in lines),
                encoding="utf-8",
            )
            os.utime(path, (_MOMENT, _MOMENT))
            aggregator = UsageAggregator(cursor_homes=(home,))
            snapshot = aggregator.snapshot({}, now=_MOMENT)
            account = _today_account(snapshot)
            self.assertEqual(account["input_tokens"], 12)
            self.assertEqual(account["output_tokens"], 4)
            self.assertEqual(account["total_tokens"], 16)
            self.assertEqual(account["reasoning_output_tokens"], 9)
            self.assertEqual(account["projects"][0]["project"], "my-slug")


class GeminiQwenUsageTests(unittest.TestCase):
    def test_recorded_total_sets_input_without_double_counting_thoughts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "chat.jsonl"
            user = {
                "type": "user",
                "content": "please mention tokens but do not count them",
            }
            record = _gemini_record(
                "gemini",
                "m1",
                {
                    "input": 100,
                    "output": 20,
                    "cached": 40,
                    "thoughts": 5,
                    "tool": 0,
                    "total": 125,
                },
            )
            path.write_text(
                json.dumps(user) + "\n" + json.dumps(record) + "\n",
                encoding="utf-8",
            )
            parsed = parse_chat_chunk(
                path,
                0,
                product="gemini",
                project="/work/gemini",
                fallback_timestamp=_MOMENT,
                maximum_bytes=1024 * 1024,
            )
            self.assertEqual(len(parsed.events), 1)
            event = parsed.events[0]
            self.assertEqual(event.input_tokens, 100)
            self.assertEqual(event.cached_input_tokens, 40)
            self.assertEqual(event.output_tokens, 25)
            self.assertEqual(event.reasoning_output_tokens, 5)
            self.assertEqual(event.total_tokens, 125)

    def test_later_message_replaces_earlier_and_partial_index_stays_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".gemini"
            chat = home / "tmp" / "proj" / "chats" / "session.jsonl"
            chat.parent.mkdir(parents=True)
            (home / "projects.json").write_text(
                json.dumps({"proj": "/work/gemini"}),
                encoding="utf-8",
            )
            first = _gemini_record(
                "gemini",
                "m1",
                {
                    "input": 10,
                    "output": 1,
                    "cached": 0,
                    "thoughts": 0,
                    "tool": 0,
                    "total": 11,
                },
            )
            padding = {
                "type": "user",
                "content": "tokens " + ("x" * 70_000),
            }
            update = _gemini_record(
                "message_update",
                "m1",
                {
                    "input": 100,
                    "output": 20,
                    "cached": 40,
                    "thoughts": 5,
                    "tool": 0,
                    "total": 125,
                },
            )
            chat.write_text(
                "\n".join(
                    json.dumps(item) for item in (first, padding, update)
                )
                + "\n",
                encoding="utf-8",
            )
            aggregator = UsageAggregator(
                gemini_homes=(home,),
                read_budget_bytes=4096,
            )
            snapshot = aggregator.snapshot({}, now=_MOMENT)
            self.assertEqual(snapshot["periods"], [])
            self.assertFalse(snapshot["indexing"]["complete"])
            for step in range(1, 5):
                if snapshot["indexing"]["complete"]:
                    break
                snapshot = aggregator.snapshot({}, now=_MOMENT + step)
            self.assertTrue(snapshot["indexing"]["complete"])
            account = _today_account(snapshot)
            self.assertEqual(account["input_tokens"], 100)
            self.assertEqual(account["cached_input_tokens"], 40)
            self.assertEqual(account["output_tokens"], 25)
            self.assertEqual(account["total_tokens"], 125)
            self.assertEqual(account["projects"][0]["project"], "/work/gemini")

    def test_qwen_uses_the_same_token_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / ".qwen"
            chat = home / "projects" / "slug" / "chats" / "session.jsonl"
            chat.parent.mkdir(parents=True)
            (home / "projects.json").write_text(
                json.dumps({"slug": "/work/qwen-app"}),
                encoding="utf-8",
            )
            record = _gemini_record(
                "gemini",
                "q1",
                {
                    "input": 100,
                    "output": 20,
                    "cached": 40,
                    "thoughts": 5,
                    "tool": 0,
                    "total": 125,
                },
            )
            chat.write_text(json.dumps(record) + "\n", encoding="utf-8")
            aggregator = UsageAggregator(qwen_homes=(home,))
            snapshot = aggregator.snapshot({}, now=_MOMENT)
            account = _today_account(snapshot)
            self.assertEqual(account["total_tokens"], 125)
            self.assertEqual(account["output_tokens"], 25)
            self.assertEqual(account["projects"][0]["project"], "/work/qwen-app")


class LocalAgentHousekeepingTests(unittest.TestCase):
    def test_lists_chat_files_and_skips_the_opencode_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gemini = root / "gemini"
            chat = gemini / "tmp" / "proj" / "chats" / "a.jsonl"
            chat.parent.mkdir(parents=True)
            chat.write_text("{}\n", encoding="utf-8")
            (chat.parent / "note.txt").write_text("skip", encoding="utf-8")
            (gemini / "projects.json").write_text(
                json.dumps({"proj": "/work/gem"}),
                encoding="utf-8",
            )
            gemini_target = AuditTarget(
                "Gemini CLI",
                "gemini",
                gemini,
                sessions_root=default_sessions_root("gemini", gemini),
            )
            gemini_files = scan_session_files(gemini_target)
            self.assertEqual([item.path.name for item in gemini_files], ["a.jsonl"])
            self.assertEqual(gemini_files[0].session_id, "a")
            self.assertEqual(gemini_files[0].project, "/work/gem")

            qwen = root / "qwen"
            qwen_chat = qwen / "projects" / "slug" / "chats" / "b.json"
            qwen_chat.parent.mkdir(parents=True)
            qwen_chat.write_text("{}", encoding="utf-8")
            (qwen_chat.parent / "note.txt").write_text("skip", encoding="utf-8")
            qwen_target = AuditTarget(
                "Qwen Code",
                "qwen",
                qwen,
                sessions_root=default_sessions_root("qwen", qwen),
            )
            qwen_files = scan_session_files(qwen_target)
            self.assertEqual([item.path.name for item in qwen_files], ["b.json"])

            cursor = root / "cursor"
            transcript = (
                cursor
                / "projects"
                / "my-slug"
                / "agent-transcripts"
                / "sess"
                / "turn.jsonl"
            )
            transcript.parent.mkdir(parents=True)
            transcript.write_text("{}\n", encoding="utf-8")
            (cursor / "projects" / "my-slug" / "store.db").write_bytes(b"nope")
            cursor_target = AuditTarget(
                "Cursor",
                "cursor",
                cursor,
                sessions_root=default_sessions_root("cursor", cursor),
            )
            cursor_files = scan_session_files(cursor_target)
            self.assertEqual(len(cursor_files), 1)
            self.assertEqual(cursor_files[0].session_id, "sess")
            self.assertEqual(cursor_files[0].project, "my-slug")

            opencode = root / "opencode"
            opencode.mkdir()
            (opencode / "opencode.db").write_bytes(b"db")
            opencode_target = AuditTarget("OpenCode", "opencode", opencode)
            self.assertIsNone(opencode_target.sessions_root)
            self.assertFalse(opencode_target.cleanable)
            self.assertEqual(scan_session_files(opencode_target), ())

            aider = root / "aider"
            aider.mkdir()
            history = aider / ".aider.chat.history.md"
            history.write_text("history", encoding="utf-8")
            (aider / "analytics.json").write_text("{}", encoding="utf-8")
            nested = aider / "nested" / ".aider.chat.history.md"
            nested.parent.mkdir()
            nested.write_text("nope", encoding="utf-8")
            aider_target = AuditTarget(
                "Aider",
                "aider",
                aider,
                sessions_root=default_sessions_root("aider", aider),
            )
            aider_files = scan_session_files(aider_target)
            self.assertEqual([item.path for item in aider_files], [history])
            self.assertEqual(aider_files[0].project, str(aider))
            self.assertEqual(aider_files[0].session_id, aider.name)


class LocalAgentScanDirTests(unittest.TestCase):
    def test_markers_accept_the_new_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            user_home = Path(temporary) / "home"
            homes = {
                "opencode": user_home / "opencode",
                "cursor": user_home / "cursor",
                "gemini": user_home / "gemini",
                "qwen": user_home / "qwen",
                "aider": user_home / "aider",
            }
            for path in homes.values():
                path.mkdir(parents=True)
            (homes["opencode"] / "opencode.db").write_bytes(b"")
            (homes["cursor"] / "projects").mkdir()
            (homes["gemini"] / "projects.json").write_text("{}", encoding="utf-8")
            (homes["qwen"] / "tmp").mkdir()
            (homes["aider"] / ".aider.chat.history.md").write_text(
                "",
                encoding="utf-8",
            )
            for provider, path in homes.items():
                result = validate_directory(
                    provider,
                    path,
                    home_dir=user_home,
                )
                self.assertTrue(result.ok, result.errors)
                self.assertTrue(result.structure_ok, provider)


def _local_stamp(moment: float) -> str:
    return datetime.fromtimestamp(moment).strftime("%Y-%m-%d %H:%M:%S")


def _agent_process(
    proc_root: Path,
    pid: int,
    comm: str,
    *,
    open_path: Path | None = None,
    cwd: Path | None = None,
) -> None:
    directory = proc_root / str(pid)
    (directory / "fd").mkdir(parents=True, exist_ok=True)
    (directory / "comm").write_text(f"{comm}\n", encoding="utf-8")
    (directory / "cmdline").write_bytes(comm.encode() + b"\0")
    fields = " ".join(["S", "1"] + ["0"] * 17 + ["9"])
    (directory / "stat").write_text(
        f"{pid} ({comm}) {fields}",
        encoding="utf-8",
    )
    if open_path is not None:
        (directory / "fd" / "3").symlink_to(open_path)
    if cwd is not None:
        (directory / "cwd").symlink_to(cwd)


def _cursor_transcript(home: Path, project: Path, name: str) -> Path:
    slug = sorted(_path_slugs(str(project.resolve())))[0]
    path = home / "projects" / slug / "agent-transcripts" / "sess" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    return path


class AiderUsageTests(unittest.TestCase):
    def test_parses_rounded_display_without_counting_prose(self) -> None:
        stamp = _local_stamp(_MOMENT)
        history = "\n".join(
            [
                f"# aider chat started at {stamp}",
                "> Main model: openai/gpt-4o with diff edit format",
                "> Editor model: should-not-win with diff",
                "#### 机密正文",
                "> Tokens: 12 sent, 3 received. Cost: $0.01 message, $0.01 session.",
                "> Tokens: 1.5k sent, 2k cache write, 4k cache hit, 10 received.",
                "> Model: gpt-4o-mini with diff edit format",
                "> Tokens: 10k sent, 4k cache hit, 2k received.",
            ]
        ) + "\n"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".aider.chat.history.md"
            path.write_text(history, encoding="utf-8")
            parsed = parse_aider_chunk(
                path,
                0,
                project=str(path.parent),
                fallback_timestamp=1.0,
                maximum_bytes=1024 * 1024,
            )
        self.assertTrue(parsed.reached_eof)
        self.assertEqual(len(parsed.events), 3)
        plain, anthropic, subset = parsed.events
        self.assertEqual(plain.timestamp, _MOMENT)
        self.assertEqual(plain.model, "openai/gpt-4o")
        self.assertEqual(plain.input_tokens, 12)
        self.assertEqual(plain.output_tokens, 3)
        self.assertEqual(plain.cached_input_tokens, 0)
        self.assertEqual(plain.total_tokens, 15)
        self.assertEqual(anthropic.model, "openai/gpt-4o")
        self.assertEqual(anthropic.input_tokens, 5500)
        self.assertEqual(anthropic.cached_input_tokens, 4000)
        self.assertEqual(anthropic.cache_write_input_tokens, 1500)
        self.assertEqual(anthropic.output_tokens, 10)
        self.assertEqual(anthropic.total_tokens, 5510)
        self.assertEqual(subset.model, "gpt-4o-mini")
        self.assertEqual(subset.input_tokens, 10_000)
        self.assertEqual(subset.cached_input_tokens, 4_000)
        self.assertEqual(subset.cache_write_input_tokens, 0)
        self.assertEqual(subset.output_tokens, 2_000)
        self.assertEqual(subset.total_tokens, 12_000)
        self.assertNotIn("机密正文", plain.model)

    def test_small_budget_finishes_without_stalling(self) -> None:
        stamp = _local_stamp(_MOMENT)
        padding = "> " + ("x" * 4000)
        history = "\n".join(
            [
                f"# aider chat started at {stamp}",
                "> Main model: openai/gpt-4o with diff edit format",
                "#### 机密正文",
                padding,
                "> Tokens: 12 sent, 3 received.",
            ]
        ) + "\n"
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "repo"
            home.mkdir()
            path = home / ".aider.chat.history.md"
            path.write_text(history, encoding="utf-8")
            aggregator = UsageAggregator(
                aider_homes=(home,),
                read_budget_bytes=1024,
                discovery_interval=30,
            )
            snapshot = aggregator.snapshot({}, now=_MOMENT)
            for step in range(1, 8):
                if snapshot["indexing"]["complete"]:
                    break
                snapshot = aggregator.snapshot({}, now=_MOMENT + step)
        self.assertTrue(snapshot["indexing"]["complete"])
        account = _today_account(snapshot)
        self.assertEqual(account["input_tokens"], 12)
        self.assertEqual(account["output_tokens"], 3)
        self.assertEqual(account["total_tokens"], 15)
        self.assertEqual(account["projects"][0]["project"], str(home))


class ActiveChatSessionTests(unittest.TestCase):
    @requires_symlinks
    def test_open_cursor_file_is_protected_and_foreign_files_are_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".cursor"
            project = root / "work" / "app"
            project.mkdir(parents=True)
            transcript = _cursor_transcript(home, project, "turn.jsonl")
            foreign = root / "notes.jsonl"
            foreign.write_text("{}\n", encoding="utf-8")
            proc = root / "proc"
            _agent_process(proc, 7, "cursor-agent", open_path=transcript)
            _agent_process(proc, 8, "cursor-agent", open_path=foreign)
            sessions = list_cursor_active_sessions(home, proc_root=proc)
            self.assertEqual(
                [item.jsonl_path for item in sessions],
                [str(transcript.resolve())],
            )
            with mock.patch(
                "a_token_monitor.multi_account.list_cursor_active_sessions",
                return_value=sessions,
            ):
                protected = external_active_session_paths(cursor_homes=(home,))
            self.assertEqual(protected, {str(transcript.resolve())})

    def test_no_process_means_no_cursor_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".cursor"
            project = root / "work" / "app"
            project.mkdir(parents=True)
            _cursor_transcript(home, project, "turn.jsonl")
            proc = root / "proc"
            proc.mkdir()
            self.assertEqual(
                list_cursor_active_sessions(home, proc_root=proc),
                (),
            )

    @requires_symlinks
    def test_cwd_fallback_keeps_the_newest_file_after_ten_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".cursor"
            project = root / "work" / "app"
            project.mkdir(parents=True)
            older = _cursor_transcript(home, project, "older.jsonl")
            newer = _cursor_transcript(home, project, "newer.jsonl")
            os.utime(older, (1_000_000.0, 1_000_000.0))
            os.utime(newer, (1_000_100.0, 1_000_100.0))
            proc = root / "proc"
            _agent_process(proc, 9, "cursor-agent", cwd=project)
            sessions = list_cursor_active_sessions(
                home,
                proc_root=proc,
                now=1_780_000_000.0,
            )
        self.assertEqual(
            [item.jsonl_path for item in sessions],
            [str(newer.resolve())],
        )
        self.assertEqual(sessions[0].confidence.value, "recent_file")

    @requires_symlinks
    def test_gemini_and_qwen_open_files_match_their_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gemini = root / ".gemini"
            gemini_chat = gemini / "tmp" / "proj" / "chats" / "a.jsonl"
            gemini_chat.parent.mkdir(parents=True)
            gemini_chat.write_text("{}\n", encoding="utf-8")
            qwen = root / ".qwen"
            qwen_chat = qwen / "projects" / "slug" / "chats" / "b.json"
            qwen_chat.parent.mkdir(parents=True)
            qwen_chat.write_text("{}\n", encoding="utf-8")
            proc = root / "proc"
            _agent_process(proc, 1, "gemini", open_path=gemini_chat)
            _agent_process(proc, 2, "qwen", open_path=qwen_chat)
            gemini_sessions = list_gemini_active_sessions(
                gemini, proc_root=proc
            )
            qwen_sessions = list_qwen_active_sessions(qwen, proc_root=proc)
        self.assertEqual(
            [item.jsonl_path for item in gemini_sessions],
            [str(gemini_chat.resolve())],
        )
        self.assertEqual(gemini_sessions[0].confidence.value, "open_file")
        self.assertEqual(
            [item.jsonl_path for item in qwen_sessions],
            [str(qwen_chat.resolve())],
        )

    @requires_symlinks
    def test_aider_open_file_and_old_cwd_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            history = repo / ".aider.chat.history.md"
            history.write_text("# aider\n", encoding="utf-8")
            os.utime(history, (1_000_000.0, 1_000_000.0))
            foreign = root / "notes.md"
            foreign.write_text("nope", encoding="utf-8")
            home = root / ".aider"
            home.mkdir()
            ignored = root / "proc-foreign"
            _agent_process(ignored, 3, "aider", open_path=foreign)
            self.assertEqual(
                list_aider_active_sessions(home, proc_root=ignored),
                (),
            )
            opened = root / "proc-open"
            _agent_process(opened, 4, "aider", open_path=history)
            open_sessions = list_aider_active_sessions(
                home, proc_root=opened
            )
            self.assertEqual(open_sessions[0].confidence.value, "open_file")
            self.assertEqual(
                Path(open_sessions[0].jsonl_path or "").resolve(),
                history.resolve(),
            )
            fallback = root / "proc-cwd"
            _agent_process(fallback, 5, "aider", cwd=repo)
            cwd_sessions = list_aider_active_sessions(
                home,
                proc_root=fallback,
                now=1_780_000_000.0,
            )
        self.assertEqual(len(cwd_sessions), 1)
        self.assertEqual(
            Path(cwd_sessions[0].jsonl_path or "").resolve(),
            history.resolve(),
        )
        self.assertEqual(cwd_sessions[0].confidence.value, "recent_file")
