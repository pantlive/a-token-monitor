"""Kimi Code 本地身份、wire 日志用量和配额读取测试。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from codex_reset_monitor import kimi as kimi_module
from codex_reset_monitor.kimi import (
    _clear_quota_cache,
    kimi_wire_session_id,
    list_kimi_active_sessions,
    load_kimi_session_index,
    parse_kimi_wire_chunk,
    read_kimi_account,
    read_kimi_quota,
    resolve_kimi_homes,
)
from codex_reset_monitor.registry import MultiSessionRegistry
from codex_reset_monitor.usage import TokenUsage, UsageAggregator, _estimate_usage


class KimiUsageTests(unittest.TestCase):
    """验证 Kimi wire.jsonl 请求用量和账号登录状态读取。"""

    def test_parses_usage_record_and_maps_session_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            kimi_home = _make_kimi_home(root)
            registry = MultiSessionRegistry(root / "state")
            aggregator = UsageAggregator(
                discovery_interval=0.01,
                kimi_homes=(kimi_home,),
            )

            state = aggregator.snapshot({"codex": registry}, now=1789708700.0)

        accounts = _period(state, "today")["accounts"]
        kimi_accounts = [
            item for item in accounts if item["account"] == "kimi"
        ]
        self.assertEqual(len(kimi_accounts), 1)
        kimi_account = kimi_accounts[0]
        # inputOther 800 + inputCacheRead 200 + inputCacheCreation 40
        self.assertEqual(kimi_account["input_tokens"], 1040)
        self.assertEqual(kimi_account["cached_input_tokens"], 200)
        self.assertEqual(kimi_account["cache_write_input_tokens"], 40)
        self.assertEqual(kimi_account["output_tokens"], 60)
        self.assertEqual(kimi_account["total_tokens"], 1100)
        self.assertEqual(kimi_account["models"][0]["model"], "kimi-code/k3-256k")
        self.assertEqual(
            kimi_account["projects"][0]["project"],
            "/workspace/demo",
        )
        self.assertEqual(
            kimi_account["estimated_cost_usd"],
            _estimate_usage(
                TokenUsage(
                    input_tokens=1040,
                    cached_input_tokens=200,
                    cache_write_input_tokens=40,
                    output_tokens=60,
                    total_tokens=1100,
                ),
                "kimi-code/k3-256k",
            )["estimated_cost_usd"],
        )
        dumped = json.dumps(state)
        self.assertNotIn("SECRET-TOKEN", dumped)
        self.assertNotIn("SECRET-REFRESH", dumped)

    def test_skips_non_turn_scope_and_noise_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            wire = (
                Path(temporary_directory)
                / "sessions"
                / "wd_demo"
                / "session_1"
                / "agents"
                / "main"
                / "wire.jsonl"
            )
            wire.parent.mkdir(parents=True)
            wire.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in (
                        {
                            "type": "usage.record",
                            "model": "kimi-code/k3-256k",
                            "usage": {"inputOther": 10, "output": 2},
                            "usageScope": "session",
                            "time": 1789708620000,
                        },
                        {
                            "type": "llm.request",
                            "model": "k3-256k",
                            "time": 1789708620001,
                        },
                        {
                            "type": "usage.record",
                            "model": "kimi-code/k3-256k",
                            "usage": {"inputOther": 10, "output": 2},
                            "usageScope": "turn",
                            "time": 1789708620002,
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            parsed = parse_kimi_wire_chunk(
                wire,
                offset=0,
                session_index={},
                default_model="kimi-code/k3-256k",
            )

        self.assertEqual(len(parsed.events), 1)
        self.assertEqual(parsed.events[0].input_tokens, 10)
        self.assertEqual(parsed.events[0].output_tokens, 2)
        self.assertAlmostEqual(parsed.events[0].timestamp, 1789708620.002)
        self.assertTrue(parsed.reached_eof)

    def test_wire_session_id_requires_standard_layout(self) -> None:
        standard = Path(
            "/home/u/.kimi-code/sessions/wd_x/session_1/agents/main/wire.jsonl"
        )
        self.assertEqual(kimi_wire_session_id(standard), "session_1")
        self.assertIsNone(
            kimi_wire_session_id(Path("/tmp/sessions/wd_x/session_1/state.json"))
        )

    def test_load_session_index_reads_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            kimi_home = _make_kimi_home(Path(temporary_directory))

            index = load_kimi_session_index(kimi_home)

        self.assertEqual(index["session_1"].cwd, "/workspace/demo")

    def test_read_account_detects_login_without_reading_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            kimi_home = root / ".kimi-code"
            credentials = kimi_home / "credentials"
            credentials.mkdir(parents=True)
            (credentials / "kimi-code.json").write_text(
                json.dumps(
                    {
                        "access_token": "SECRET-TOKEN",
                        "refresh_token": "SECRET-REFRESH",
                    }
                ),
                encoding="utf-8",
            )

            account = read_kimi_account(kimi_home)

            self.assertTrue(account.logged_in)
            self.assertEqual(account.account_key, "profile:kimi")
            self.assertNotIn("SECRET", repr(account))
            (credentials / "kimi-code.json").unlink()
            self.assertFalse(read_kimi_account(kimi_home).logged_in)

    def test_kimi_pricing_has_no_long_context_multiplier(self) -> None:
        estimate = _estimate_usage(
            TokenUsage(
                input_tokens=300_000,
                cached_input_tokens=100_000,
                output_tokens=1_000,
                total_tokens=301_000,
            ),
            "kimi-code/k3-256k",
        )

        # 200000*3 + 100000*0.3 + 1000*15，每 1M token；K3 不按上下文分段。
        self.assertAlmostEqual(estimate["estimated_cost_usd"], 0.645)

    def test_cache_savings_usd_priced_and_unpriced(self) -> None:
        """缓存节省金额 = 缓存 token 数 ×（输入价 − 缓存价）。"""

        priced = _estimate_usage(
            TokenUsage(
                input_tokens=100_000,
                cached_input_tokens=100_000,
                total_tokens=100_000,
            ),
            "kimi-code/k3-256k",
        )
        self.assertAlmostEqual(priced["cache_savings_usd"], 0.27)

        unpriced = _estimate_usage(
            TokenUsage(
                input_tokens=1_000,
                cached_input_tokens=1_000,
                total_tokens=2_000,
            ),
            "kimi-code/kimi-for-coding",
        )
        self.assertIsNone(unpriced["cache_savings_usd"])

    def test_snapshot_includes_daily_trend_and_cache_savings(self) -> None:
        """用量快照应带 30 天按天趋势和账号级缓存节省金额。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            kimi_home = _make_kimi_home(root)
            registry = MultiSessionRegistry(root / "state")
            aggregator = UsageAggregator(
                discovery_interval=0.01,
                kimi_homes=(kimi_home,),
            )

            state = aggregator.snapshot({"codex": registry}, now=1789708700.0)

        daily = state["daily"]
        self.assertEqual(len(daily), 30)
        expected_today = (
            datetime.fromtimestamp(1789708700.0).astimezone().strftime("%Y-%m-%d")
        )
        self.assertEqual(daily[-1]["date"], expected_today)
        self.assertEqual(daily[-1]["total_tokens"], 1100)
        self.assertFalse(daily[-1]["has_unpriced"])
        self.assertEqual(sum(day["total_tokens"] for day in daily), 1100)
        # 800*3 + 200*0.3 + 40*3*1.25（缓存写入）+ 60*15，每 1M token。
        self.assertAlmostEqual(daily[-1]["estimated_cost_usd"], 0.00351)
        kimi_accounts = [
            item
            for item in _period(state, "today")["accounts"]
            if item["account"] == "kimi"
        ]
        # 200 个缓存 token × (3 - 0.3) USD/1M。
        self.assertAlmostEqual(kimi_accounts[0]["cache_savings_usd"], 0.00054)

    def test_kimi_for_coding_stays_unpriced(self) -> None:
        estimate = _estimate_usage(
            TokenUsage(input_tokens=1000, output_tokens=100, total_tokens=1100),
            "kimi-code/kimi-for-coding",
        )

        self.assertIsNone(estimate["estimated_cost_usd"])
        self.assertFalse(estimate["api_pricing_known"])

    def test_resolve_homes_deduplicates_and_empty_disables_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "nope"
            self.assertEqual(
                resolve_kimi_homes((missing, missing)),
                (missing.resolve(),),
            )
        self.assertEqual(resolve_kimi_homes(()), ())

    def test_lists_process_backed_active_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            kimi_home = _make_kimi_home(root)
            state = (
                kimi_home
                / "sessions"
                / "wd_demo"
                / "session_1"
                / "state.json"
            )
            proc_root = root / "proc"
            process = proc_root / "88"
            (process / "fd").mkdir(parents=True)
            (process / "comm").write_text("kimi\n", encoding="utf-8")
            (process / "cmdline").write_bytes(b"kimi\0")
            (process / "stat").write_text(
                "88 (kimi) " + " ".join(["S", "1"] + ["0"] * 17 + ["9"]),
                encoding="utf-8",
            )
            (process / "fd" / "3").symlink_to(state)

            sessions = list_kimi_active_sessions(
                kimi_home,
                proc_root=proc_root,
                now=1_789_708_700.0,
            )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_id, "session_1")
        self.assertEqual(sessions[0].cwd, "/workspace/demo")
        self.assertEqual(sessions[0].pids, (88,))
        self.assertEqual(sessions[0].source, "kimi-cli")


class KimiQuotaTests(unittest.TestCase):
    """验证 Kimi 官方 /usages 配额读取、token 刷新与跨进程目录锁。"""

    def setUp(self) -> None:
        _clear_quota_cache()
        # 隔离真实环境里的 Kimi 端点覆盖变量，保证测试确定性。
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("KIMI_CODE_BASE_URL", "KIMI_CODE_OAUTH_HOST", "KIMI_OAUTH_HOST"):
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        _clear_quota_cache()

    def test_parses_windows_and_booster_wallet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_quota_home(Path(temporary_directory))
            get_calls: list[tuple[str, dict[str, str], float]] = []
            post_calls: list[tuple[str, dict[str, str], float]] = []

            def fake_get(url: str, headers: dict[str, str], timeout: float):
                get_calls.append((url, dict(headers), timeout))
                return 200, _usage_payload()

            def fake_post(url: str, params: dict[str, str], timeout: float):
                post_calls.append((url, dict(params), timeout))
                return 500, {}

            snapshot = read_kimi_quota(
                home,
                now=1_789_700_000.0,
                http_get_json=fake_get,
                http_post_form=fake_post,
            )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.source, "kimi-api")
        self.assertEqual(snapshot.raw_limit_ids, ("kimi",))
        self.assertEqual(
            [window.name for window in snapshot.windows],
            ["limit_5h", "limit_7d", "limit_month_total"],
        )
        limit_5h = snapshot.windows[0]
        self.assertAlmostEqual(limit_5h.used_percent or 0, 25.0)
        self.assertEqual(limit_5h.window_minutes, 300.0)
        self.assertEqual(
            limit_5h.resets_at,
            datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc).timestamp(),
        )
        self.assertFalse(limit_5h.is_exhausted)
        limit_7d = snapshot.windows[1]
        self.assertAlmostEqual(limit_7d.used_percent or 0, 50.0)
        self.assertEqual(limit_7d.window_minutes, 10_080.0)
        self.assertIsNone(limit_7d.resets_at)
        month_total = snapshot.windows[2]
        self.assertAlmostEqual(month_total.used_percent or 0, 10.0)
        self.assertIsNone(month_total.window_minutes)
        self.assertEqual(
            snapshot.metadata,
            {
                "booster_balance_cents": "150",
                "booster_total_cents": "200",
                "booster_monthly_charge_limit_cents": "5000",
                "booster_monthly_used_cents": "1200",
                "booster_monthly_charge_limit_enabled": "true",
                "booster_currency": "CNY",
            },
        )
        # token 新鲜时不应触发刷新；GET 必须带 Bearer 头和 8 秒超时。
        self.assertEqual(post_calls, [])
        self.assertEqual(len(get_calls), 1)
        url, headers, timeout = get_calls[0]
        self.assertEqual(url, "https://api.kimi.com/coding/v1/usages")
        self.assertEqual(headers["Authorization"], "Bearer SECRET-TOKEN")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(timeout, 8.0)
        self.assertNotIn("SECRET", repr(snapshot))

    def test_refreshes_expired_token_and_writes_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_quota_home(Path(temporary_directory), fresh=False)
            post_calls: list[tuple[str, dict[str, str], float]] = []
            get_headers: list[dict[str, str]] = []

            def fake_post(url: str, params: dict[str, str], timeout: float):
                post_calls.append((url, dict(params), timeout))
                return 200, {
                    "access_token": "NEW-ACCESS",
                    "refresh_token": "NEW-REFRESH",
                    "expires_in": 900,
                    "scope": "kimi-code",
                    "token_type": "Bearer",
                }

            def fake_get(url: str, headers: dict[str, str], timeout: float):
                get_headers.append(dict(headers))
                return 200, _usage_payload()

            before = time.time()
            snapshot = read_kimi_quota(
                home,
                now=1_789_700_000.0,
                http_get_json=fake_get,
                http_post_form=fake_post,
                sleep=lambda _: None,
            )

            self.assertIsNotNone(snapshot)
            self.assertEqual(len(post_calls), 1)
            url, params, _ = post_calls[0]
            self.assertEqual(url, "https://auth.kimi.com/api/oauth/token")
            self.assertEqual(params["grant_type"], "refresh_token")
            self.assertEqual(params["refresh_token"], "SECRET-REFRESH")
            self.assertEqual(
                params["client_id"], "17e5f671-d194-4dfb-9706-5516cb48c098"
            )
            self.assertEqual(
                get_headers[0]["Authorization"], "Bearer NEW-ACCESS"
            )
            written = json.loads(
                (home / "credentials" / "kimi-code.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(written["access_token"], "NEW-ACCESS")
            self.assertEqual(written["refresh_token"], "NEW-REFRESH")
            self.assertEqual(written["expires_in"], 900)
            self.assertGreaterEqual(written["expires_at"], int(before) + 900)
            self.assertNotIn("SECRET-REFRESH", json.dumps(written))
            mode = stat.S_IMODE(
                (home / "credentials" / "kimi-code.json").stat().st_mode
            )
            self.assertEqual(mode, 0o600)
            # 锁目录必须释放，锁目标文件保留（与官方一致）。
            self.assertFalse((home / "oauth" / "kimi-code.lock").exists())
            self.assertTrue((home / "oauth" / "kimi-code").exists())

    def test_401_and_network_error_return_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_quota_home(Path(temporary_directory))
            responses = iter([(401, {"error": "unauthorized"}), (0, None)])
            post_calls: list[tuple[str, dict[str, str], float]] = []

            def fake_get(url: str, headers: dict[str, str], timeout: float):
                return next(responses)

            def fake_post(url: str, params: dict[str, str], timeout: float):
                post_calls.append((url, dict(params), timeout))
                return 200, {}

            first = read_kimi_quota(
                home,
                now=1_789_700_000.0,
                http_get_json=fake_get,
                http_post_form=fake_post,
            )
            second = read_kimi_quota(
                home,
                now=1_789_700_000.0,
                http_get_json=fake_get,
                http_post_form=fake_post,
            )

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(post_calls, [])

    def test_refresh_unauthorized_keeps_credentials_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_quota_home(Path(temporary_directory), fresh=False)
            original = (home / "credentials" / "kimi-code.json").read_text(
                encoding="utf-8"
            )

            def fake_post(url: str, params: dict[str, str], timeout: float):
                return 401, {"error": "invalid_grant"}

            snapshot = read_kimi_quota(
                home,
                now=1_789_700_000.0,
                http_get_json=lambda url, headers, timeout: (200, _usage_payload()),
                http_post_form=fake_post,
                sleep=lambda _: None,
            )

            self.assertIsNone(snapshot)
            self.assertEqual(
                (home / "credentials" / "kimi-code.json").read_text(
                    encoding="utf-8"
                ),
                original,
            )
            self.assertFalse((home / "oauth" / "kimi-code.lock").exists())

    def test_refresh_retries_retryable_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_quota_home(Path(temporary_directory), fresh=False)
            post_calls: list[str] = []
            sleeps: list[float] = []

            def fake_post(url: str, params: dict[str, str], timeout: float):
                post_calls.append(url)
                if len(post_calls) == 1:
                    return 500, {"error": "server"}
                return 200, {
                    "access_token": "NEW-ACCESS",
                    "refresh_token": "NEW-REFRESH",
                    "expires_in": 900,
                }

            snapshot = read_kimi_quota(
                home,
                now=1_789_700_000.0,
                http_get_json=lambda url, headers, timeout: (200, _usage_payload()),
                http_post_form=fake_post,
                sleep=sleeps.append,
            )

            self.assertIsNotNone(snapshot)
            self.assertEqual(len(post_calls), 2)
            self.assertEqual(sleeps, [1.0])

    def test_lock_contention_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_quota_home(Path(temporary_directory), fresh=False)
            lock_dir = home / "oauth" / "kimi-code.lock"
            lock_dir.mkdir(parents=True)
            post_calls: list[str] = []

            def fake_post(url: str, params: dict[str, str], timeout: float):
                post_calls.append(url)
                return 200, {}

            snapshot = read_kimi_quota(
                home,
                now=1_789_700_000.0,
                http_get_json=lambda url, headers, timeout: (200, _usage_payload()),
                http_post_form=fake_post,
                sleep=lambda _: None,
            )

            self.assertIsNone(snapshot)
            self.assertEqual(post_calls, [])
            # 别人持有的锁不能被我们释放。
            self.assertTrue(lock_dir.exists())

    def test_breaks_stale_lock_and_refreshes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_quota_home(Path(temporary_directory), fresh=False)
            lock_dir = home / "oauth" / "kimi-code.lock"
            lock_dir.mkdir(parents=True)
            stale = time.time() - 30
            os.utime(lock_dir, (stale, stale))
            post_calls: list[str] = []

            def fake_post(url: str, params: dict[str, str], timeout: float):
                post_calls.append(url)
                return 200, {
                    "access_token": "NEW-ACCESS",
                    "refresh_token": "NEW-REFRESH",
                    "expires_in": 900,
                }

            snapshot = read_kimi_quota(
                home,
                now=1_789_700_000.0,
                http_get_json=lambda url, headers, timeout: (200, _usage_payload()),
                http_post_form=fake_post,
                sleep=lambda _: None,
            )

            self.assertIsNotNone(snapshot)
            self.assertEqual(len(post_calls), 1)
            self.assertFalse(lock_dir.exists())

    def test_skips_refresh_when_lock_peer_already_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_quota_home(Path(temporary_directory), fresh=False)
            lock_dir = home / "oauth" / "kimi-code.lock"
            lock_dir.mkdir(parents=True)
            post_calls: list[str] = []
            get_headers: list[dict[str, str]] = []

            def on_sleep(_: float) -> None:
                # 模拟等待锁期间官方 CLI 已完成刷新并释放锁。
                _write_credentials(
                    home,
                    {
                        "access_token": "PEER-ACCESS",
                        "refresh_token": "PEER-REFRESH",
                        "expires_at": time.time() + 900,
                        "expires_in": 900,
                    },
                )
                lock_dir.rmdir()

            def fake_post(url: str, params: dict[str, str], timeout: float):
                post_calls.append(url)
                return 200, {}

            def fake_get(url: str, headers: dict[str, str], timeout: float):
                get_headers.append(dict(headers))
                return 200, _usage_payload()

            snapshot = read_kimi_quota(
                home,
                now=time.time(),
                http_get_json=fake_get,
                http_post_form=fake_post,
                sleep=on_sleep,
            )

            self.assertIsNotNone(snapshot)
            self.assertEqual(post_calls, [])
            self.assertEqual(
                get_headers[0]["Authorization"], "Bearer PEER-ACCESS"
            )

    def test_region_file_and_env_override_select_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_quota_home(Path(temporary_directory))
            (home / "region").write_text("global\n", encoding="utf-8")
            get_urls: list[str] = []

            def fake_get(url: str, headers: dict[str, str], timeout: float):
                get_urls.append(url)
                return 200, _usage_payload()

            snapshot = read_kimi_quota(
                home,
                now=1_789_700_000.0,
                http_get_json=fake_get,
                http_post_form=lambda url, params, timeout: (500, {}),
            )
            self.assertIsNotNone(snapshot)
            self.assertEqual(
                get_urls[0], "https://api.kimi.ai/coding/v1/usages"
            )

            get_urls.clear()
            with mock.patch.dict(
                os.environ,
                {"KIMI_CODE_BASE_URL": "https://example.test/coding/v1/"},
            ):
                snapshot = read_kimi_quota(
                    home,
                    now=1_789_700_000.0,
                    http_get_json=fake_get,
                    http_post_form=lambda url, params, timeout: (500, {}),
                )
            self.assertIsNotNone(snapshot)
            self.assertEqual(
                get_urls[0], "https://example.test/coding/v1/usages"
            )

    def test_refresh_uses_env_oauth_host_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_quota_home(Path(temporary_directory), fresh=False)
            post_urls: list[str] = []

            def fake_post(url: str, params: dict[str, str], timeout: float):
                post_urls.append(url)
                return 401, {"error": "invalid_grant"}

            with mock.patch.dict(
                os.environ,
                {"KIMI_CODE_OAUTH_HOST": "https://auth.example.test/"},
            ):
                snapshot = read_kimi_quota(
                    home,
                    now=1_789_700_000.0,
                    http_get_json=lambda url, headers, timeout: (
                        200,
                        _usage_payload(),
                    ),
                    http_post_form=fake_post,
                    sleep=lambda _: None,
                )

            self.assertIsNone(snapshot)
            self.assertEqual(
                post_urls, ["https://auth.example.test/api/oauth/token"]
            )

    def test_drops_entries_without_used_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_quota_home(Path(temporary_directory))

            snapshot = read_kimi_quota(
                home,
                now=1_789_700_000.0,
                http_get_json=lambda url, headers, timeout: (
                    200,
                    {
                        "usages": {
                            "limit_5h": {"reset_time": "not-a-date"},
                            "limit_7d": None,
                        }
                    },
                ),
                http_post_form=lambda url, params, timeout: (500, {}),
            )

            self.assertIsNone(snapshot)
            payload_snapshot = read_kimi_quota(
                home,
                now=1_789_700_000.0,
                http_get_json=lambda url, headers, timeout: (200, "not-json-object"),
                http_post_form=lambda url, params, timeout: (500, {}),
            )
            self.assertIsNone(payload_snapshot)

    def test_missing_credentials_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / ".kimi-code"
            home.mkdir()

            snapshot = read_kimi_quota(
                home,
                now=1_789_700_000.0,
                http_get_json=lambda url, headers, timeout: (200, _usage_payload()),
                http_post_form=lambda url, params, timeout: (500, {}),
            )

            self.assertIsNone(snapshot)

    def test_real_http_layer_results_are_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_quota_home(Path(temporary_directory))
            get_calls: list[str] = []

            def fake_get(url: str, headers: dict[str, str], timeout: float):
                get_calls.append(url)
                return 200, _usage_payload()

            def fake_post(url: str, params: dict[str, str], timeout: float):
                raise AssertionError("token 新鲜时不应触发刷新")

            with (
                mock.patch.object(kimi_module, "_http_get_json", fake_get),
                mock.patch.object(kimi_module, "_http_post_form", fake_post),
            ):
                first = read_kimi_quota(home, now=1_789_700_000.0)
                second = read_kimi_quota(home, now=1_789_700_030.0)
                third = read_kimi_quota(home, now=1_789_700_061.0)

            self.assertIsNotNone(first)
            self.assertIs(second, first)
            self.assertIsNotNone(third)
            self.assertEqual(len(get_calls), 2)

    def test_real_http_layer_failures_cached_briefly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = _make_quota_home(Path(temporary_directory))
            get_calls: list[str] = []

            def fake_get(url: str, headers: dict[str, str], timeout: float):
                get_calls.append(url)
                return 0, None

            def fake_post(url: str, params: dict[str, str], timeout: float):
                raise AssertionError("token 新鲜时不应触发刷新")

            with (
                mock.patch.object(kimi_module, "_http_get_json", fake_get),
                mock.patch.object(kimi_module, "_http_post_form", fake_post),
            ):
                self.assertIsNone(read_kimi_quota(home, now=1_800_000_000.0))
                self.assertIsNone(read_kimi_quota(home, now=1_800_000_010.0))
                self.assertIsNone(read_kimi_quota(home, now=1_800_000_016.0))

            self.assertEqual(len(get_calls), 2)


def _usage_payload() -> dict[str, object]:
    """构造一份官方 /usages 响应。"""

    return {
        "usages": {
            "limit_5h": {
                "used_ratio": 0.25,
                "reset_time": "2026-09-18T20:00:00Z",
            },
            "limit_7d": {"used_ratio": 0.5},
            "limit_month_total": {"used_ratio": "0.10"},
            "limit_month_code": {"reset_time": "2026-10-01T00:00:00Z"},
        },
        "boosterWallet": {
            "balance": {
                "type": "BOOSTER",
                "amount": 200_000_000,
                "amountLeft": 150_000_000,
            },
            "monthlyChargeLimit": {"priceInCents": 5000, "currency": "CNY"},
            "monthlyUsed": {"priceInCents": 1200, "currency": "CNY"},
            "monthlyChargeLimitEnabled": True,
        },
    }


def _write_credentials(home: Path, credentials: dict[str, object]) -> None:
    credentials_dir = home / "credentials"
    credentials_dir.mkdir(parents=True, exist_ok=True)
    (credentials_dir / "kimi-code.json").write_text(
        json.dumps(credentials),
        encoding="utf-8",
    )


def _make_quota_home(root: Path, fresh: bool = True) -> Path:
    """构造带凭据的 KIMI_CODE_HOME；fresh=False 时凭据已过期。"""

    home = root / ".kimi-code"
    home.mkdir(parents=True)
    expires_at = 1_900_000_000.0 if fresh else 1_000_000_000.0
    _write_credentials(
        home,
        {
            "access_token": "SECRET-TOKEN",
            "refresh_token": "SECRET-REFRESH",
            "expires_at": expires_at,
            "expires_in": 900,
            "scope": "kimi-code",
            "token_type": "Bearer",
        },
    )
    return home


def _make_kimi_home(root: Path) -> Path:
    """构造包含一个会话、一条用量记录和假凭据的 KIMI_CODE_HOME。"""

    kimi_home = root / ".kimi-code"
    session_root = kimi_home / "sessions" / "wd_demo" / "session_1"
    agents_dir = session_root / "agents" / "main"
    agents_dir.mkdir(parents=True)
    (session_root / "state.json").write_text(
        json.dumps({"id": "session_1", "cwd": "/workspace/demo"}),
        encoding="utf-8",
    )
    (agents_dir / "wire.jsonl").write_text(
        "\n".join(
            json.dumps(item)
            for item in (
                {"type": "metadata", "time": 1},
                {
                    "type": "usage.record",
                    "agentId": "main",
                    "model": "kimi-code/k3-256k",
                    "usage": {
                        "inputOther": 800,
                        "inputCacheRead": 200,
                        "inputCacheCreation": 40,
                        "output": 60,
                    },
                    "usageScope": "turn",
                    "time": 1789708620583,
                },
                {
                    "type": "context.append_loop_event",
                    "event": {"type": "step.end", "usage": {"output": 999}},
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    credentials = kimi_home / "credentials"
    credentials.mkdir()
    (credentials / "kimi-code.json").write_text(
        json.dumps(
            {
                "access_token": "SECRET-TOKEN",
                "refresh_token": "SECRET-REFRESH",
            }
        ),
        encoding="utf-8",
    )
    return kimi_home


def _period(state: dict[str, object], key: str) -> dict[str, object]:
    """取出一个时间窗口。"""

    periods = state["periods"]
    assert isinstance(periods, list)
    for period in periods:
        assert isinstance(period, dict)
        if period["key"] == key:
            return period
    raise AssertionError(f"缺少时间窗口: {key}")


if __name__ == "__main__":
    unittest.main()
