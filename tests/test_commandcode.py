"""Command Code 账号身份、订阅额度和活动会话读取测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from token_monitor.commandcode import (
    _clear_quota_cache,
    list_commandcode_active_sessions,
    read_commandcode_account,
    read_commandcode_quota,
    read_commandcode_session_info,
    resolve_commandcode_homes,
)
from token_monitor.quota import QuotaSnapshot, QuotaWindow


ACCOUNT_ID = "a8f7ddce-358a-4441-9d10-de053e64c79f"
API_KEY = "user_SECRET_KEY_MUST_NOT_LEAK"
PERIOD_START = "2026-09-09T03:25:57.000Z"
PERIOD_END = "2026-10-09T03:25:57.000Z"
FIVE_HOUR_RESET_MS = 1790073581793
WEEKLY_RESET_MS = 1790217789196


def _make_commandcode_home(root: Path, api_key: str | None = API_KEY) -> Path:
    """创建一个最小 Command Code 数据目录。"""

    home = root / ".commandcode"
    home.mkdir(parents=True, exist_ok=True)
    if api_key is not None:
        (home / "auth.json").write_text(
            json.dumps(
                {
                    "apiKey": api_key,
                    "userId": ACCOUNT_ID,
                    "userName": "ch1711585919mck1",
                    "keyName": "cli-2026-09-09T05-45-30",
                    "authenticatedAt": "2026-09-09T05:45:32.060Z",
                }
            ),
            encoding="utf-8",
        )
    return home


def _write_session(
    home: Path,
    session_id: str,
    cwd: str = "/workspace/demo",
    model: str | None = "deepseek/deepseek-v4.1-flash",
    started_at: str = "2026-09-10T03:05:06.737Z",
) -> Path:
    """写入一份最小会话 JSONL 和 meta.json。"""

    directory = home / "projects" / "workspace-demo"
    directory.mkdir(parents=True, exist_ok=True)
    messages = directory / f"{session_id}.jsonl"
    messages.write_text(
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": session_id,
                "timestamp": started_at,
                "cwd": cwd,
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "edaa658f",
                "parentId": None,
                "timestamp": started_at,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "SECRET-PROMPT"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if model is not None:
        (directory / f"{session_id}.meta.json").write_text(
            json.dumps({"model": model}),
            encoding="utf-8",
        )
    return messages


def _write_process(
    proc_root: Path,
    pid: int,
    comm: str,
    command: tuple[str, ...],
    open_files: tuple[Path, ...] = (),
    ppid: int = 1,
    start: str = "1000",
) -> None:
    """写入一个假的 /proc 进程目录。"""

    directory = proc_root / str(pid)
    (directory / "fd").mkdir(parents=True)
    (directory / "comm").write_text(f"{comm}\n", encoding="utf-8")
    (directory / "cmdline").write_bytes(
        b"\0".join(item.encode("utf-8") for item in command) + b"\0"
    )
    fields = ["S", str(ppid)] + ["0"] * 17 + [start]
    (directory / "stat").write_text(
        f"{pid} ({comm}) " + " ".join(fields),
        encoding="utf-8",
    )
    for index, path in enumerate(open_files, start=3):
        (directory / "fd" / str(index)).symlink_to(path)


class CommandCodeHomesTests(unittest.TestCase):
    """验证数据目录解析和默认值。"""

    def tearDown(self) -> None:
        _clear_quota_cache()

    def test_resolve_keeps_order_and_removes_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = _make_commandcode_home(root, api_key=None)
            second = root / ".commandcode-work"
            second.mkdir()

            homes = resolve_commandcode_homes((first, second, first))

        self.assertEqual(homes, (first.resolve(), second.resolve()))

    def test_resolve_falls_back_to_default_home_when_it_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = _make_commandcode_home(root, api_key=None)
            with mock.patch.dict(
                "os.environ",
                {"COMMANDCODE_HOME": str(home)},
            ):
                homes = resolve_commandcode_homes()

        self.assertEqual(homes, (home.resolve(),))


class CommandCodeAccountTests(unittest.TestCase):
    """验证本地身份读取不会泄露 API Key。"""

    def test_reads_identity_without_exposing_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_commandcode_home(Path(temporary_directory))

            account = read_commandcode_account(home)

        self.assertEqual(account.account_id, ACCOUNT_ID)
        self.assertEqual(account.display_name, "ch1711585919mck1")
        self.assertEqual(account.user_name, "ch1711585919mck1")
        self.assertEqual(account.profile_name, "command-code")
        self.assertEqual(account.key_name, "cli-2026-09-09T05-45-30")
        self.assertTrue(account.logged_in)
        self.assertEqual(account.account_key, ACCOUNT_ID)
        self.assertNotIn(API_KEY, repr(account))

    def test_missing_auth_file_reports_logged_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_commandcode_home(
                Path(temporary_directory),
                api_key=None,
            )

            account = read_commandcode_account(home)

        self.assertFalse(account.logged_in)
        self.assertIsNone(account.account_id)
        self.assertEqual(account.display_name, "command-code")
        self.assertEqual(account.account_key, "profile:command-code")


class CommandCodeQuotaTests(unittest.TestCase):
    """验证官方后台额度接口的请求契约和解析结果。"""

    def setUp(self) -> None:
        _clear_quota_cache()

    def tearDown(self) -> None:
        _clear_quota_cache()

    def test_reads_windows_metadata_and_sends_cli_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_commandcode_home(Path(temporary_directory))
            calls: list[tuple[str, dict[str, str], float]] = []

            def fake_get(
                url: str,
                headers: dict[str, str],
                timeout: float,
            ) -> tuple[int, object]:
                calls.append((url, dict(headers), timeout))
                if url.startswith("https://api.commandcode.ai/alpha/whoami"):
                    return 200, {"success": True, "user": {"userName": "tester"}}
                if url.endswith("/alpha/billing/credits"):
                    return 200, {
                        "credits": {
                            "belowThreshold": False,
                            "creditThreshold": 0,
                            "monthlyCredits": 41.6279108343,
                            "purchasedCredits": 0,
                            "freeCredits": 0,
                        },
                        "windowLimits": {
                            "limited": True,
                            "exceeded": None,
                            "fiveHour": {
                                "used": 0.53906363,
                                "cap": 14,
                                "exceeded": False,
                                "resetAt": FIVE_HOUR_RESET_MS,
                            },
                            "weekly": {
                                "used": 5.603253285,
                                "cap": 35,
                                "exceeded": False,
                                "resetAt": WEEKLY_RESET_MS,
                            },
                        },
                    }
                if url.endswith("/alpha/billing/subscriptions"):
                    return 200, {
                        "success": True,
                        "data": {
                            "id": "sub_1",
                            "status": "active",
                            "planId": "individual-goat",
                            "currentPeriodStart": PERIOD_START,
                            "currentPeriodEnd": PERIOD_END,
                        },
                    }
                if url.startswith(
                    "https://api.commandcode.ai/alpha/usage/summary"
                ):
                    return 200, {
                        "totalCount": 5095,
                        "totalCost": 28.686106232699995,
                        "totalCredits": 28.686106232699995,
                        "totalTokensIn": 1296530110,
                        "totalTokensOut": 3569937,
                    }
                raise AssertionError(f"未预期的请求: {url}")

            snapshot = read_commandcode_quota(
                home,
                now=1_789_490_000.0,
                http_get_json=fake_get,
            )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.source, "command-code-api")
        self.assertEqual(snapshot.raw_limit_ids, ("command-code",))
        self.assertEqual(snapshot.plan_type, "GOAT")
        self.assertEqual(
            [window.name for window in snapshot.windows],
            ["5-hour", "Weekly", "monthly"],
        )
        five_hour = snapshot.windows[0]
        self.assertAlmostEqual(five_hour.used_percent or 0, 0.53906363 / 14 * 100)
        self.assertEqual(five_hour.window_minutes, 300.0)
        self.assertEqual(
            five_hour.resets_at,
            FIVE_HOUR_RESET_MS / 1000,
        )
        self.assertFalse(five_hour.is_exhausted)
        weekly = snapshot.windows[1]
        self.assertEqual(weekly.window_minutes, 10_080.0)
        self.assertEqual(weekly.resets_at, WEEKLY_RESET_MS / 1000)
        # 本月窗口：已用 28.6861，剩余 41.6279，合计 70.314 名额。
        monthly = snapshot.windows[2]
        self.assertAlmostEqual(
            monthly.used_percent or 0,
            28.686106232699995 / (41.6279108343 + 28.686106232699995) * 100,
        )
        self.assertIsNone(monthly.window_minutes)
        self.assertEqual(
            monthly.resets_at,
            datetime(2026, 10, 9, 3, 25, 57, tzinfo=timezone.utc).timestamp(),
        )
        self.assertEqual(
            snapshot.metadata["monthly_credits_remaining"],
            "41.63",
        )
        self.assertEqual(snapshot.metadata["period_credits_spent"], "28.69")
        self.assertEqual(snapshot.metadata["period_requests"], "5095")
        self.assertEqual(snapshot.metadata["plan_id"], "individual-goat")
        self.assertEqual(snapshot.metadata["subscription_status"], "active")
        self.assertEqual(snapshot.metadata["period_end"], PERIOD_END)
        self.assertEqual(snapshot.metadata["user_name"], "tester")
        self.assertNotIn("apiKey", snapshot.metadata)

        # 官方接口要求 CLI 版本头，否则返回 403；查询参数带账期起点。
        self.assertEqual(len(calls), 4)
        self.assertEqual(
            calls[0][0],
            "https://api.commandcode.ai/alpha/whoami?limits=1",
        )
        self.assertEqual(
            calls[3][0],
            "https://api.commandcode.ai/alpha/usage/summary"
            "?since=2026-09-09T03%3A25%3A57.000Z",
        )
        for _, headers, timeout in calls:
            self.assertEqual(headers["Authorization"], f"Bearer {API_KEY}")
            self.assertEqual(headers["User-Agent"], "cli")
            self.assertEqual(headers["x-command-code-version"], "1.53.1")
            self.assertEqual(timeout, 8.0)

    def test_sends_org_id_when_account_belongs_to_an_org(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_commandcode_home(Path(temporary_directory))
            urls: list[str] = []

            def fake_get(
                url: str,
                headers: dict[str, str],
                timeout: float,
            ) -> tuple[int, object]:
                urls.append(url)
                if "whoami" in url:
                    return 200, {
                        "success": True,
                        "user": {"userName": "tester"},
                        "org": {"id": "org_123"},
                    }
                # 带 orgId 时路径后面还有查询串，因此按子串匹配。
                if "/alpha/billing/credits" in url:
                    return 200, {"credits": {"monthlyCredits": 10}}
                if "/alpha/billing/subscriptions" in url:
                    return 200, {
                        "data": {
                            "status": "active",
                            "planId": "teams-pro",
                            "currentPeriodStart": PERIOD_START,
                            "currentPeriodEnd": PERIOD_END,
                        }
                    }
                return 200, {"totalCredits": 5, "totalCount": 2}

            snapshot = read_commandcode_quota(
                home,
                now=1_789_490_000.0,
                http_get_json=fake_get,
            )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.plan_type, "Teams Pro")
        self.assertIn("orgId=org_123", urls[1])
        self.assertIn("orgId=org_123&since=", urls[3])

    def test_returns_none_on_auth_failure_or_missing_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = _make_commandcode_home(root)
            calls: list[str] = []

            def forbidden(
                url: str,
                headers: dict[str, str],
                timeout: float,
            ) -> tuple[int, object]:
                calls.append(url)
                return 403, None

            forbidden_snapshot = read_commandcode_quota(
                home,
                now=1.0,
                http_get_json=forbidden,
            )
            logged_out_home = _make_commandcode_home(root / "other", api_key=None)
            missing_snapshot = read_commandcode_quota(
                logged_out_home,
                now=1.0,
                http_get_json=forbidden,
            )

        self.assertIsNone(forbidden_snapshot)
        self.assertIsNone(missing_snapshot)
        # 没登录时不应发起任何请求。
        self.assertEqual(len(calls), 1)

    def test_uses_cached_result_for_repeated_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_commandcode_home(Path(temporary_directory))
            calls: list[str] = []

            def fake_get(
                url: str,
                headers: dict[str, str],
                timeout: float,
            ) -> tuple[int, object]:
                calls.append(url)
                if "whoami" in url:
                    return 200, {"user": {"userName": "tester"}}
                if url.endswith("/alpha/billing/credits"):
                    return 200, {"credits": {"monthlyCredits": 10}}
                if url.endswith("/alpha/billing/subscriptions"):
                    return 200, {"data": {"status": "active", "planId": "individual-go"}}
                return 200, {"totalCredits": 1, "totalCount": 1}

            # 注入 HTTP 调用时不做缓存（测试需要真实请求次数）。
            first = read_commandcode_quota(home, now=100.0, http_get_json=fake_get)
            second = read_commandcode_quota(home, now=101.0, http_get_json=fake_get)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(len(calls), 8)

    def test_quota_windows_helper_matches_snapshot_shape(self) -> None:
        """额度窗口字段与 Dashboard 依赖的 QuotaSnapshot 保持一致。"""

        snapshot = QuotaSnapshot(
            observed_at=1.0,
            windows=(
                QuotaWindow(
                    limit_id="command-code",
                    name="Weekly",
                    used_percent=16.0,
                    window_minutes=10_080.0,
                    resets_at=2.0,
                ),
            ),
            plan_type="GOAT",
            source="command-code-api",
            raw_limit_ids=("command-code",),
            metadata={"period_credits_spent": "1.00"},
        )

        self.assertEqual(snapshot.exhausted_windows, ())
        self.assertIsNone(snapshot.latest_exhausted_reset_at)
        self.assertIsNotNone(snapshot.window("command-code", "Weekly"))


class CommandCodeSessionTests(unittest.TestCase):
    """验证活动会话识别只读取元数据。"""

    def test_reads_session_header_and_meta_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_commandcode_home(Path(temporary_directory))
            session_id = "a07902bd-2981-4581-a7d7-47d69da9f01c"
            path = _write_session(home, session_id)

            info = read_commandcode_session_info(path)

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.session_id, session_id)
        self.assertEqual(info.cwd, "/workspace/demo")
        self.assertEqual(info.model, "deepseek/deepseek-v4.1-flash")
        self.assertEqual(
            info.started_at,
            datetime(2026, 9, 10, 3, 5, 6, 737000, tzinfo=timezone.utc).timestamp(),
        )

    def test_session_info_accepts_meta_sidecar_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_commandcode_home(Path(temporary_directory))
            session_id = "6f584a23-ed99-4c31-9d5b-e05817ad7a35"
            _write_session(home, session_id)
            meta_path = home / "projects" / "workspace-demo" / f"{session_id}.meta.json"

            info = read_commandcode_session_info(meta_path)

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.session_id, session_id)
        self.assertEqual(info.cwd, "/workspace/demo")

    def test_lists_only_sessions_with_open_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = _make_commandcode_home(root)
            session_id = "2af45f83-d018-485a-8746-d575df5b2141"
            messages = _write_session(home, session_id)
            checkpoints = messages.with_name(f"{session_id}.checkpoints.jsonl")
            checkpoints.write_text("{}\n", encoding="utf-8")
            proc_root = root / "proc"
            _write_process(
                proc_root,
                pid=70,
                comm="MainThread",
                command=("node", "/usr/bin/command-code"),
                open_files=(messages, checkpoints),
            )

            sessions = list_commandcode_active_sessions(
                home,
                proc_root=proc_root,
                now=2_000.0,
            )

        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session.thread_id, f"command-code:{session_id}")
        self.assertEqual(session.session_id, session_id)
        self.assertEqual(session.cwd, "/workspace/demo")
        self.assertEqual(session.pids, (70,))
        self.assertEqual(session.source, "command-code-cli")
        self.assertEqual(session.last_event_type, "deepseek/deepseek-v4.1-flash")
        self.assertNotIn("SECRET-PROMPT", json.dumps(session.to_record()))

    def test_ignores_unrelated_processes_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = _make_commandcode_home(root)
            session_id = "3a6c6f56-2b8f-4e20-aa9b-590014d78869"
            messages = _write_session(home, session_id)
            unrelated = root / "notes.jsonl"
            unrelated.write_text("{}\n", encoding="utf-8")
            proc_root = root / "proc"
            _write_process(
                proc_root,
                pid=71,
                comm="node",
                command=("node", "/usr/bin/other-agent"),
                open_files=(messages,),
            )
            _write_process(
                proc_root,
                pid=72,
                comm="MainThread",
                command=("node", "/usr/bin/command-code"),
                open_files=(unrelated,),
            )

            sessions = list_commandcode_active_sessions(
                home,
                proc_root=proc_root,
                now=2_000.0,
            )

        self.assertEqual(sessions, ())


if __name__ == "__main__":
    unittest.main()
