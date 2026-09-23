"""本地 Dashboard 的 HTTP 接口测试。"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from token_monitor.alerts import TrafficAlertStore
from token_monitor.housekeeping import (
    AuditTarget,
    DiskThresholds,
    HousekeepingMonitor,
)
from token_monitor.dashboard import (
    _DASHBOARD_HTML,
    _SETTINGS_HTML,
    DashboardConfig,
    DashboardServer,
    build_multi_dashboard_state,
)
from token_monitor.health import HealthTracker
from token_monitor.retention import RetentionController, RetentionError
from token_monitor.multi_models import (
    DetectionConfidence,
    SessionStatus,
    TrackedSession,
)
from token_monitor.quota import QuotaSnapshot, QuotaWindow
from token_monitor.registry import MultiSessionRegistry
from token_monitor.scan_dirs import ScanDirsController
from token_monitor.traffic import TrafficAlert
from token_monitor.usage import UsageAggregator


class DashboardTests(unittest.TestCase):
    """验证网页只展示额度、会话和用量状态。"""

    def test_aggregates_quotas_without_mixing_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            personal = MultiSessionRegistry(root / "personal-state")
            company = MultiSessionRegistry(root / "company-state")
            personal.save_quota(
                QuotaSnapshot(
                    observed_at=100,
                    plan_type="plus",
                    source="personal-app-server",
                    windows=(
                        QuotaWindow(
                            limit_id="codex",
                            name="primary",
                            used_percent=20,
                            window_minutes=300,
                            resets_at=200,
                        ),
                    ),
                )
            )
            company.save_quota(
                QuotaSnapshot(
                    observed_at=100,
                    plan_type="plus",
                    source="company-app-server",
                    windows=(
                        QuotaWindow(
                            limit_id="codex",
                            name="primary",
                            used_percent=80,
                            window_minutes=300,
                            resets_at=300,
                        ),
                    ),
                )
            )
            personal.upsert_session(
                TrackedSession(
                    thread_id="personal-thread",
                    session_id="personal-session",
                    jsonl_path=None,
                    cwd=str(root),
                    source="cli",
                    status=SessionStatus.RUNNING,
                    confidence=DetectionConfidence.APP_SERVER,
                    first_seen_at=90,
                    last_seen_at=100,
                )
            )

            state = build_multi_dashboard_state(
                {"personal": personal, "company": company}
            )

        self.assertEqual(
            [(item["account"], item["source"]) for item in state["quotas"]],
            [
                ("personal", "personal-app-server"),
                ("company", "company-app-server"),
            ],
        )
        self.assertIsNone(state["quota"])
        self.assertEqual(state["sessions"][0]["account"], "personal")

    def test_groups_profiles_by_real_account_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            personal = MultiSessionRegistry(root / "personal-state")
            work = MultiSessionRegistry(root / "work-state")
            for registry, observed_at in ((personal, 100), (work, 110)):
                registry.save_quota(
                    QuotaSnapshot(
                        observed_at=observed_at,
                        plan_type="plus",
                        source="app-server",
                        windows=(
                            QuotaWindow(
                                limit_id="codex",
                                name="primary",
                                used_percent=20,
                                window_minutes=300,
                                resets_at=200,
                            ),
                        ),
                    )
                )
            for registry, thread_id in (
                (personal, "personal-thread"),
                (work, "work-thread"),
            ):
                registry.upsert_session(
                    TrackedSession(
                        thread_id=thread_id,
                        session_id=thread_id,
                        jsonl_path=None,
                        cwd=str(root),
                        source="cli",
                        status=SessionStatus.RUNNING,
                        confidence=DetectionConfidence.PERSISTED,
                        first_seen_at=90,
                        last_seen_at=100,
                    )
                )

            state = build_multi_dashboard_state(
                {"codex": personal, "codex-work": work},
                account_metadata={
                    "codex": {
                        "account_id": "same-account",
                        "profile_name": "codex",
                        "codex_home": str(root / ".codex"),
                    },
                    "codex-work": {
                        "account_id": "same-account",
                        "profile_name": "codex-work",
                        "codex_home": str(root / ".codex-work"),
                    },
                },
            )

        self.assertEqual([item["name"] for item in state["accounts"]], ["same-account"])
        self.assertEqual(
            {item["name"] for item in state["accounts"][0]["profiles"]},
            {"codex", "codex-work"},
        )
        self.assertEqual(len(state["quotas"]), 1)
        self.assertEqual(
            {item["profile_name"] for item in state["sessions"]},
            {"codex", "codex-work"},
        )

    def test_keeps_old_session_under_its_recorded_account_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = MultiSessionRegistry(root / "state")
            registry.save_quota(
                QuotaSnapshot(
                    observed_at=100,
                    plan_type="plus",
                    source="current-account",
                    windows=(
                        QuotaWindow(
                            limit_id="codex",
                            name="primary",
                            used_percent=20,
                            window_minutes=300,
                            resets_at=200,
                        ),
                    ),
                )
            )
            registry.upsert_session(
                TrackedSession(
                    thread_id="old-account-thread",
                    session_id="old-account-session",
                    jsonl_path=None,
                    cwd=str(root),
                    source="cli",
                    status=SessionStatus.RUNNING,
                    confidence=DetectionConfidence.PERSISTED,
                    first_seen_at=90,
                    last_seen_at=100,
                    account_id="account-personal",
                )
            )

            state = build_multi_dashboard_state(
                {"codex": registry},
                account_metadata={
                    "codex": {
                        "account_id": "account-work",
                        "profile_name": "codex",
                        "codex_home": str(root / ".codex"),
                    }
                },
            )

        self.assertEqual(state["sessions"][0]["account"], "account-personal")
        account_by_name = {account["name"]: account for account in state["accounts"]}
        self.assertIsNone(account_by_name["account-personal"]["quota"])
        self.assertIsNotNone(account_by_name["account-work"]["quota"])

    def test_serves_state_html_and_rejects_write_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = MultiSessionRegistry(root / "state")
            registry.save_quota(
                QuotaSnapshot(
                    observed_at=100,
                    plan_type="plus",
                    source="app-server",
                    windows=(
                        QuotaWindow(
                            limit_id="codex",
                            name="primary",
                            used_percent=42,
                            window_minutes=300,
                            resets_at=200,
                        ),
                    ),
                )
            )
            registry.upsert_session(
                TrackedSession(
                    thread_id="thread-dashboard",
                    session_id="session-dashboard",
                    jsonl_path=str(root / "session.jsonl"),
                    cwd=str(root),
                    source="cli",
                    status=SessionStatus.RUNNING,
                    confidence=DetectionConfidence.OPEN_FILE,
                    first_seen_at=90,
                    last_seen_at=100,
                    pids=(123,),
                )
            )
            server = DashboardServer(
                registry=registry,
                config=DashboardConfig(port=0),
                grok_homes=(),
                kimi_homes=(),
                dsh_homes=(),
                commandcode_homes=(),
            claude_homes=(),
            )
            server.start()
            host, port = server.address
            base_url = f"http://{host}:{port}"
            try:
                with urlopen(f"{base_url}/api/state", timeout=2) as response:
                    state = json.load(response)
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        response.headers["Cache-Control"],
                        "no-store",
                    )
                    self.assertEqual(state["usage"]["periods"], [])
                with urlopen(f"{base_url}/api/usage", timeout=2) as response:
                    usage_state = json.load(response)
                    self.assertEqual(response.status, 200)
                    self.assertIn("usage", usage_state)
                with urlopen(f"{base_url}/", timeout=2) as response:
                    html = response.read().decode("utf-8")
                    self.assertIn("Token Monitor", html)
                    self.assertNotIn("Codex Monitor", html)
                    self.assertIn(
                        '<div class="brand-name">Token <span>Monitor</span></div>',
                        html,
                    )
                    self.assertIn("<title>Token Monitor</title>", html)
                    self.assertIn("用量与成本估算", html)
                    self.assertIn("API 等价金额", html)
                    self.assertIn("Dashboard 导航", html)
                    self.assertIn('href="#accounts"', html)
                    self.assertIn('href="#usage"', html)
                    self.assertIn('href="#traffic"', html)
                    self.assertIn("异常流量监控", html)
                    self.assertLess(html.find('href="#overview"'), html.find('href="#traffic"'))
                    self.assertLess(html.find('id="traffic"'), html.find('id="accounts"'))
                    self.assertIn('id="account-list"', html)
                    self.assertIn('id="usage-content"', html)
                    self.assertIn('id="traffic-content"', html)
                    self.assertIn("traffic", state)
                    self.assertIn('id="usage-load-button"', html)
                    self.assertIn('id="refresh-button"', html)
                    self.assertIn('id="accounts-online"', html)
                    self.assertIn("usage-model-filter", html)
                    self.assertIn("usage-project-filter", html)
                    self.assertIn("项目 / 工作目录", html)
                    self.assertIn("data-session-toggle", html)
                    self.assertIn("个活动会话", html)
                    self.assertIn("收起会话列表", html)
                    self.assertIn("expandedSessionTables", html)
                    self.assertIn("session-collapsible", html)
                    self.assertNotIn("额度中断与恢复记录", html)
                    self.assertNotIn("自动恢复", html)
                    self.assertNotIn("/api/recovery/cancel", html)
                head_request = Request(base_url + "/", method="HEAD")
                with urlopen(head_request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertGreater(int(response.headers["Content-Length"]), 0)
                    self.assertEqual(response.read(), b"")
                request = Request(base_url + "/api/state", method="POST")
                with self.assertRaises(HTTPError) as context:
                    urlopen(request, timeout=2)
            finally:
                server.close()

        self.assertEqual(state["counts"]["active"], 1)
        self.assertEqual(state["counts"]["process_backed"], 1)
        self.assertEqual(state["quota"]["windows"][0]["used_percent"], 42)
        self.assertEqual(context.exception.code, 405)
        self.assertNotIn("prompt", json.dumps(state, ensure_ascii=False))

    def test_state_includes_kimi_account_without_quota(self) -> None:
        """Kimi 配额接口不可用时应只展示账号身份。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = MultiSessionRegistry(root / "state")
            kimi_home = root / ".kimi-code"
            credentials = kimi_home / "credentials"
            credentials.mkdir(parents=True)
            (credentials / "kimi-code.json").write_text(
                json.dumps({"access_token": "SECRET-TOKEN"}),
                encoding="utf-8",
            )
            server = DashboardServer(
                registry=registry,
                config=DashboardConfig(port=0),
                grok_homes=(),
                kimi_homes=(kimi_home,),
                dsh_homes=(),
                commandcode_homes=(),
            claude_homes=(),
            )
            server.start()
            host, port = server.address
            try:
                with mock.patch(
                    "token_monitor.dashboard.read_kimi_quota",
                    return_value=None,
                ):
                    with urlopen(
                        f"http://{host}:{port}/api/state", timeout=2
                    ) as response:
                        state = json.load(response)
            finally:
                server.close()

        kimi_accounts = [
            account
            for account in state["accounts"]
            if account.get("product") == "kimi"
        ]
        self.assertEqual(len(kimi_accounts), 1)
        self.assertEqual(kimi_accounts[0]["name"], "kimi")
        self.assertIsNone(kimi_accounts[0]["quota"])
        self.assertNotIn("SECRET-TOKEN", json.dumps(state, ensure_ascii=False))

    def test_state_includes_commandcode_account_and_quota(self) -> None:
        """Command Code 订阅额度应出现在账号卡片和额度区。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = MultiSessionRegistry(root / "state")
            home = root / ".commandcode"
            home.mkdir(parents=True)
            (home / "auth.json").write_text(
                json.dumps(
                    {
                        "apiKey": "SECRET-API-KEY",
                        "userId": "a8f7ddce-358a-4441-9d10-de053e64c79f",
                        "userName": "tester",
                    }
                ),
                encoding="utf-8",
            )
            quota = QuotaSnapshot(
                observed_at=1_789_490_000.0,
                windows=(
                    QuotaWindow(
                        limit_id="command-code",
                        name="Weekly",
                        used_percent=16.0,
                        window_minutes=10_080.0,
                        resets_at=1_790_217_789.0,
                    ),
                ),
                plan_type="GOAT",
                source="command-code-api",
                raw_limit_ids=("command-code",),
                metadata={
                    "period_credits_spent": "28.69",
                    "monthly_credits_remaining": "41.63",
                    "period_requests": "5095",
                    "days_left": "17",
                },
            )
            server = DashboardServer(
                registry=registry,
                config=DashboardConfig(port=0),
                grok_homes=(),
                kimi_homes=(),
                dsh_homes=(),
                commandcode_homes=(home,),
            )
            server.start()
            host, port = server.address
            try:
                with mock.patch(
                    "token_monitor.dashboard.read_commandcode_quota",
                    return_value=quota,
                ):
                    with urlopen(
                        f"http://{host}:{port}/api/state", timeout=2
                    ) as response:
                        state = json.load(response)
            finally:
                server.close()

        accounts = [
            account
            for account in state["accounts"]
            if account.get("product") == "command-code"
        ]
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["name"], "tester")
        self.assertEqual(
            accounts[0]["account_id"],
            "a8f7ddce-358a-4441-9d10-de053e64c79f",
        )
        quota_payload = accounts[0]["quota"]
        self.assertIsNotNone(quota_payload)
        assert quota_payload is not None
        self.assertEqual(quota_payload["plan_type"], "GOAT")
        self.assertEqual(quota_payload["product"], "command-code")
        self.assertEqual(quota_payload["windows"][0]["name"], "Weekly")
        self.assertEqual(
            quota_payload["metadata"]["monthly_credits_remaining"],
            "41.63",
        )
        self.assertNotIn("SECRET-API-KEY", json.dumps(state, ensure_ascii=False))
        # 账号卡片产品标签与用量区对账都依赖这两个前端标记。
        self.assertIn("Command Code", _DASHBOARD_HTML)
        self.assertIn("renderCommandCodeReconciliation", _DASHBOARD_HTML)

    def test_serves_insights_and_page_markers(self) -> None:
        """习惯分析端点应返回对话画像，页面应带分析区标记。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            sessions_dir = home / "sessions"
            sessions_dir.mkdir(parents=True)
            for index in range(3):
                (sessions_dir / f"session-{index}.jsonl").write_text(
                    json.dumps(
                        {
                            "timestamp": "2026-08-27T09:00:00Z",
                            "type": "event_msg",
                            "payload": {
                                "thread_settings": {"model": "gpt-5.6-luna"},
                                "info": {
                                    "total_token_usage": {
                                        "input_tokens": 100 + index,
                                        "cached_input_tokens": 0,
                                        "output_tokens": 10,
                                        "reasoning_output_tokens": 0,
                                        "total_tokens": 110 + index,
                                    },
                                },
                            },
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
            registry = MultiSessionRegistry(root / "state")
            aggregator = UsageAggregator(discovery_interval=0.01)
            metadata = {
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(home),
                },
            }
            server = DashboardServer(
                registry=registry,
                config=DashboardConfig(port=0),
                account_metadata=metadata,
                usage_aggregator=aggregator,
                grok_homes=(),
                kimi_homes=(),
                dsh_homes=(),
                commandcode_homes=(),
            claude_homes=(),
            )
            server.start()
            host, port = server.address
            try:
                with urlopen(
                    f"http://{host}:{port}/api/insights", timeout=2
                ) as response:
                    payload = json.load(response)
                    self.assertEqual(response.status, 200)
                # 夹具时间早于当前日期，days=1 的窗口应过滤掉全部对话。
                with urlopen(
                    f"http://{host}:{port}/api/insights?days=1", timeout=2
                ) as response:
                    window_payload = json.load(response)
                    self.assertEqual(response.status, 200)
                head_request = Request(
                    f"http://{host}:{port}/api/insights",
                    method="HEAD",
                )
                with urlopen(head_request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                with urlopen(f"http://{host}:{port}/", timeout=2) as response:
                    html = response.read().decode("utf-8")
            finally:
                server.close()

        insights = payload["insights"]
        self.assertTrue(insights["ready"])
        self.assertIsNone(insights["window_days"])
        self.assertEqual(insights["conversation_count"], 3)
        self.assertEqual(len(insights["hour_histogram"]), 24)
        self.assertIn("suggestions", insights)
        window_insights = window_payload["insights"]
        self.assertTrue(window_insights["ready"])
        self.assertEqual(window_insights["window_days"], 1)
        self.assertEqual(window_insights["conversation_count"], 0)
        for marker in (
            'id="insights"',
            'id="insights-load-button"',
            'id="insights-period-tabs"',
            'data-insights-days="7"',
            "renderInsights",
            "/api/insights",
            "习惯分析",
        ):
            self.assertIn(marker, html)

    def test_state_includes_budget_and_kimi_booster_metadata(self) -> None:
        """预算和 Kimi booster 真实扣费字段应出现在 Dashboard 状态里。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = MultiSessionRegistry(root / "state")
            kimi_home = root / ".kimi-code"
            credentials = kimi_home / "credentials"
            credentials.mkdir(parents=True)
            (credentials / "kimi-code.json").write_text(
                json.dumps({"access_token": "SECRET-TOKEN"}),
                encoding="utf-8",
            )
            snapshot = QuotaSnapshot(
                observed_at=1_789_700_000.0,
                source="kimi-api",
                metadata={
                    "booster_monthly_used_cents": "1234",
                    "booster_currency": "CNY",
                },
            )
            server = DashboardServer(
                registry=registry,
                config=DashboardConfig(port=0, budget_usd=50.0),
                grok_homes=(),
                kimi_homes=(kimi_home,),
                dsh_homes=(),
                commandcode_homes=(),
            claude_homes=(),
            )
            server.start()
            host, port = server.address
            try:
                with mock.patch(
                    "token_monitor.dashboard.read_kimi_quota",
                    return_value=snapshot,
                ):
                    with urlopen(
                        f"http://{host}:{port}/api/state", timeout=2
                    ) as response:
                        state = json.load(response)
                    with urlopen(f"http://{host}:{port}/", timeout=2) as response:
                        html = response.read().decode("utf-8")
            finally:
                server.close()

        self.assertEqual(state["budget_usd"], 50.0)
        kimi_quotas = [
            quota for quota in state["quotas"] if quota.get("product") == "kimi"
        ]
        self.assertEqual(len(kimi_quotas), 1)
        self.assertEqual(
            kimi_quotas[0]["metadata"]["booster_monthly_used_cents"],
            "1234",
        )
        for marker in (
            'id="alert-list"',
            "renderAlerts",
            "renderTrend",
            "usage-trend",
            "renderTopProjects",
            "renderKimiReconciliation",
            "budget-bar",
            "本月预算",
            "缓存节省",
        ):
            self.assertIn(marker, html)
        self.assertNotIn("SECRET-TOKEN", json.dumps(state, ensure_ascii=False))

    def test_state_includes_kimi_quota_when_available(self) -> None:
        """Kimi 配额读取成功时账号卡片应带窗口数据。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = MultiSessionRegistry(root / "state")
            kimi_home = root / ".kimi-code"
            credentials = kimi_home / "credentials"
            credentials.mkdir(parents=True)
            (credentials / "kimi-code.json").write_text(
                json.dumps({"access_token": "SECRET-TOKEN"}),
                encoding="utf-8",
            )
            snapshot = QuotaSnapshot(
                observed_at=1_789_700_000.0,
                windows=(
                    QuotaWindow(
                        limit_id="kimi",
                        name="limit_5h",
                        used_percent=25.0,
                        window_minutes=300.0,
                        resets_at=1_789_730_000.0,
                    ),
                ),
                source="kimi-api",
                raw_limit_ids=("kimi",),
            )
            server = DashboardServer(
                registry=registry,
                config=DashboardConfig(port=0),
                grok_homes=(),
                kimi_homes=(kimi_home,),
                dsh_homes=(),
                commandcode_homes=(),
            claude_homes=(),
            )
            server.start()
            host, port = server.address
            try:
                with mock.patch(
                    "token_monitor.dashboard.read_kimi_quota",
                    return_value=snapshot,
                ):
                    with urlopen(
                        f"http://{host}:{port}/api/state", timeout=2
                    ) as response:
                        state = json.load(response)
            finally:
                server.close()

        kimi_accounts = [
            account
            for account in state["accounts"]
            if account.get("product") == "kimi"
        ]
        self.assertEqual(len(kimi_accounts), 1)
        quota = kimi_accounts[0]["quota"]
        self.assertIsNotNone(quota)
        self.assertEqual(quota["product"], "kimi")
        self.assertEqual(quota["source"], "kimi-api")
        self.assertEqual(quota["windows"][0]["name"], "limit_5h")
        self.assertEqual(quota["windows"][0]["used_percent"], 25.0)
        self.assertNotIn("SECRET-TOKEN", json.dumps(state, ensure_ascii=False))

    def test_includes_kimi_and_dsh_live_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = MultiSessionRegistry(root / "state")
            kimi_home = root / ".kimi-code"
            session_dir = kimi_home / "sessions" / "wd_demo" / "session_1"
            session_dir.mkdir(parents=True)
            state_path = session_dir / "state.json"
            state_path.write_text(
                json.dumps({"id": "session_1", "cwd": "/workspace/demo"}),
                encoding="utf-8",
            )
            (kimi_home / "credentials").mkdir()
            (kimi_home / "credentials" / "kimi-code.json").write_text(
                "{}",
                encoding="utf-8",
            )
            dsh_home = root / ".dsh"
            dsh_home.mkdir()
            (dsh_home / ".anonymous-user-id").write_text("anon-dsh\n", encoding="utf-8")
            (dsh_home / ".credentials.yaml").write_text("apiKey: x\n", encoding="utf-8")
            lock = (
                dsh_home
                / "sessions"
                / "--workspace-demo--"
                / "session-9"
                / "session.lock"
            )
            lock.parent.mkdir(parents=True)
            lock.write_text("", encoding="utf-8")
            cache = (
                dsh_home
                / "storages"
                / "session_projcache"
                / "sessions"
                / "session-9.json"
            )
            cache.parent.mkdir(parents=True)
            cache.write_text(
                json.dumps(
                    {
                        "record": {
                            "identity": {"cwd": "/workspace/demo"},
                            "rows": {
                                "tokenUsage": {
                                    "val": {
                                        "totals": {
                                            "uncachedInputTokens": 1,
                                            "outputTokens": 1,
                                            "cacheReadTokens": 0,
                                            "cacheWriteTokens": 0,
                                        }
                                    }
                                },
                                "modelSelection": {
                                    "val": {
                                        "lastUsed": {
                                            "model": "deepseek/deepseek-v4.1-flash"
                                        }
                                    }
                                },
                                "sessionListMetadata": {"val": {}},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            kimi_session = TrackedSession(
                thread_id="kimi:session_1",
                session_id="session_1",
                jsonl_path=str(state_path),
                cwd="/workspace/demo",
                source="kimi-cli",
                status=SessionStatus.RUNNING,
                confidence=DetectionConfidence.OPEN_FILE,
                first_seen_at=90,
                last_seen_at=100,
                pids=(88,),
            )
            dsh_session = TrackedSession(
                thread_id="dsh:session-9",
                session_id="session-9",
                jsonl_path=str(lock),
                cwd="/workspace/demo",
                source="dsh",
                status=SessionStatus.RUNNING,
                confidence=DetectionConfidence.OPEN_FILE,
                first_seen_at=90,
                last_seen_at=100,
                pids=(70,),
            )
            with (
                mock.patch(
                    "token_monitor.dashboard.list_kimi_active_sessions",
                    return_value=(kimi_session,),
                ),
                mock.patch(
                    "token_monitor.dashboard.list_dsh_active_sessions",
                    return_value=(dsh_session,),
                ),
            ):
                state = build_multi_dashboard_state(
                    {"personal": registry},
                    kimi_homes=(kimi_home,),
                    dsh_homes=(dsh_home,),
                )

        products = {session.get("product") for session in state["sessions"]}
        self.assertIn("kimi", products)
        self.assertIn("dsh", products)
        dsh_accounts = [
            account
            for account in state["accounts"]
            if account.get("product") == "dsh"
        ]
        self.assertEqual(len(dsh_accounts), 1)
        self.assertEqual(dsh_accounts[0]["counts"].get("active"), 1)

    def test_web_rejects_recovery_write_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = MultiSessionRegistry(root / "state")
            server = DashboardServer(
                registry=registry,
                config=DashboardConfig(port=0),
                grok_homes=(),
                kimi_homes=(),
                dsh_homes=(),
                commandcode_homes=(),
            claude_homes=(),
            )
            server.start()
            host, port = server.address
            base_url = f"http://{host}:{port}"
            try:
                request = Request(
                    base_url + "/api/recovery/cancel",
                    method="POST",
                )
                with self.assertRaises(HTTPError) as context:
                    urlopen(request, timeout=2)
            finally:
                server.close()

        self.assertEqual(context.exception.code, 405)

    def test_does_not_expose_legacy_recovery_records(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = MultiSessionRegistry(root / "state")
            registry.upsert_session(
                TrackedSession(
                    thread_id="thread-recovered",
                    session_id="session-recovered",
                    jsonl_path=None,
                    cwd=str(root),
                    source="cli",
                    status=SessionStatus.COMPLETED,
                    confidence=DetectionConfidence.PERSISTED,
                    first_seen_at=90,
                    last_seen_at=140,
                    terminal=True,
                    quota_blocked_at=100,
                    last_resume_started_at=130,
                    last_resume_finished_at=140,
                    last_resume_result="success",
                )
            )

            state = build_multi_dashboard_state({"personal": registry})

        self.assertEqual(state["sessions"], [])
        self.assertNotIn("recoveries", state)

    def test_hides_legacy_recovery_text_from_session_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = MultiSessionRegistry(root / "state")
            registry.upsert_session(
                TrackedSession(
                    thread_id="thread-blocked",
                    session_id="session-blocked",
                    jsonl_path=None,
                    cwd=str(root),
                    source="cli",
                    status=SessionStatus.LIMIT_BLOCKED,
                    confidence=DetectionConfidence.PERSISTED,
                    first_seen_at=90,
                    last_seen_at=100,
                    last_error="已从 Dashboard 手动取消自动恢复；进程已退出，不自动恢复",
                )
            )

            state = build_multi_dashboard_state({"personal": registry})

        error = state["sessions"][0]["last_error"]
        self.assertEqual(error, "历史会话状态")
        self.assertNotIn("自动恢复", error)


class AlertHistoryDashboardTests(unittest.TestCase):
    """验证告警历史查询、已读和清理接口。"""

    def _store(self, root: Path) -> TrafficAlertStore:
        """写入两条历史告警，一条 danger 一条 warn。"""

        store = TrafficAlertStore(root / "state")
        observed_at = time.time()
        store.record(
            [
                TrafficAlert(
                    level="danger",
                    product="codex",
                    pid=11,
                    kind="burst",
                    bytes=40 * 1024 * 1024,
                    window_seconds=15.0,
                    message="codex pid 11 突发外发",
                    observed_at=observed_at,
                    remote="203.0.113.10:443",
                    process_key="codex:11:10",
                    command="codex",
                    cwd="/home/dev/project",
                ),
                TrafficAlert(
                    level="warn",
                    product="kimi",
                    pid=12,
                    kind="window",
                    bytes=80 * 1024 * 1024,
                    window_seconds=300.0,
                    message="kimi pid 12 累计外发",
                    observed_at=observed_at,
                    remote="198.51.100.7:8443",
                    process_key="kimi:12:20",
                    command="kimi",
                    cwd="/srv/app",
                ),
            ]
        )
        return store

    def _server(
        self,
        root: Path,
        alert_store: TrafficAlertStore | None,
    ) -> DashboardServer:
        """启动一个只绑定回环随机端口的 Dashboard。"""

        server = DashboardServer(
            registry=MultiSessionRegistry(root / "monitor-state"),
            config=DashboardConfig(port=0),
            grok_homes=(),
            kimi_homes=(),
            dsh_homes=(),
            commandcode_homes=(),
            claude_homes=(),
            alert_store=alert_store,
        )
        server.start()
        return server

    @staticmethod
    def _post(url: str, payload: object, content_type: str = "application/json"):
        """发送 JSON POST 请求。"""

        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": content_type},
            method="POST",
        )
        return urlopen(request, timeout=2)

    def test_api_returns_filtered_alert_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server = self._server(root, self._store(root))
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with urlopen(f"{base_url}/api/alerts", timeout=2) as response:
                    payload = json.load(response)
                with urlopen(
                    f"{base_url}/api/alerts?level=warn&ack=unread",
                    timeout=2,
                ) as response:
                    warn_only = json.load(response)
                with urlopen(
                    f"{base_url}/api/alerts?q=%2Fsrv%2Fapp",
                    timeout=2,
                ) as response:
                    keyword = json.load(response)
                with urlopen(
                    f"{base_url}/api/alerts?limit=1",
                    timeout=2,
                ) as response:
                    first_page = json.load(response)
                with urlopen(f"{base_url}/api/state", timeout=2) as response:
                    state = json.load(response)
            finally:
                server.close()

        self.assertTrue(payload["available"])
        self.assertEqual(payload["retention_days"], 30.0)
        self.assertEqual(payload["stats"]["total"], 2)
        self.assertEqual(payload["stats"]["unread"], 2)
        self.assertEqual(
            [item["product"] for item in payload["alerts"]],
            ["kimi", "codex"],
        )
        self.assertEqual(len(warn_only["alerts"]), 1)
        self.assertEqual(warn_only["alerts"][0]["pid"], 12)
        self.assertEqual(len(keyword["alerts"]), 1)
        self.assertEqual(keyword["alerts"][0]["cwd"], "/srv/app")
        self.assertEqual(len(first_page["alerts"]), 1)
        self.assertTrue(first_page["has_more"])
        self.assertEqual(state["alert_history"]["unread"], 2)

    def test_api_without_store_reports_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server = self._server(root, None)
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with urlopen(f"{base_url}/api/alerts", timeout=2) as response:
                    payload = json.load(response)
                with urlopen(f"{base_url}/api/state", timeout=2) as response:
                    state = json.load(response)
            finally:
                server.close()

        self.assertFalse(payload["available"])
        self.assertEqual(payload["alerts"], [])
        self.assertFalse(state["alert_history"]["available"])

    def test_acknowledge_unacknowledge_and_clear_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server = self._server(root, self._store(root))
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with urlopen(f"{base_url}/api/alerts", timeout=2) as response:
                    alert_id = json.load(response)["alerts"][0]["id"]
                with self._post(
                    f"{base_url}/api/alerts",
                    {"action": "ack", "all": True},
                ) as response:
                    acked = json.load(response)
                with self._post(
                    f"{base_url}/api/alerts",
                    {"action": "unack", "ids": [alert_id]},
                ) as response:
                    restored = json.load(response)
                with self._post(
                    f"{base_url}/api/alerts",
                    {"action": "clear", "ids": [alert_id]},
                ) as response:
                    cleared = json.load(response)
                with self._post(
                    f"{base_url}/api/alerts",
                    {"action": "clear", "all": True},
                ) as response:
                    cleared_all = json.load(response)
                with urlopen(f"{base_url}/api/alerts", timeout=2) as response:
                    final_state = json.load(response)
            finally:
                server.close()

        self.assertTrue(acked["ok"])
        self.assertEqual(acked["changed"], 2)
        self.assertEqual(acked["stats"]["unread"], 0)
        self.assertEqual(restored["changed"], 1)
        self.assertEqual(restored["stats"]["unread"], 1)
        self.assertEqual(cleared["changed"], 1)
        self.assertEqual(cleared_all["changed"], 1)
        self.assertEqual(final_state["stats"]["total"], 0)

    def test_invalid_alert_requests_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server = self._server(root, self._store(root))
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with self.assertRaises(HTTPError) as bad_action:
                    self._post(f"{base_url}/api/alerts", {"action": "drop"})
                with self.assertRaises(HTTPError) as bad_body:
                    self._post(
                        f"{base_url}/api/alerts",
                        {"action": "ack"},
                        content_type="text/plain",
                    )
                with self.assertRaises(HTTPError) as bad_level:
                    urlopen(f"{base_url}/api/alerts?level=critical", timeout=2)
                with self.assertRaises(HTTPError) as read_only:
                    self._post(f"{base_url}/api/state", {"action": "ack"})
            finally:
                server.close()

        self.assertEqual(bad_action.exception.code, 400)
        self.assertEqual(bad_body.exception.code, 400)
        self.assertEqual(bad_level.exception.code, 400)
        self.assertEqual(read_only.exception.code, 405)

    def test_page_contains_alert_history_section(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server = self._server(root, self._store(root))
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with urlopen(f"{base_url}/", timeout=2) as response:
                    html = response.read().decode("utf-8")
            finally:
                server.close()

        self.assertIn("告警历史", html)
        self.assertIn('id="alert-history"', html)
        self.assertIn('href="#alert-history"', html)
        self.assertIn('data-nav-target="alert-history"', html)
        self.assertIn('id="alert-history-content"', html)
        self.assertIn('id="alert-range-filter"', html)
        self.assertIn('id="alert-ack-all-button"', html)
        self.assertIn('id="alert-clear-button"', html)
        self.assertLess(html.find('id="traffic"'), html.find('id="alert-history"'))
        self.assertLess(html.find('id="insights"'), html.find('id="alert-history"'))
        self.assertIn(
            'class="panel section-block is-collapsed"',
            html,
        )
        self.assertIn('data-section-toggle="alert-history"', html)
        self.assertIn('id="alert-history-body"', html)
        self.assertIn("/api/alerts", html)


class GrokSessionDashboardTests(unittest.TestCase):
    """验证 Grok 活动会话以统一字段进入 Dashboard 状态。"""

    def test_grok_sessions_use_unified_fields(self) -> None:
        from token_monitor.multi_models import DetectionConfidence, SessionStatus
        from token_monitor.multi_models import TrackedSession as Model

        session = Model(
            thread_id="grok:session-1",
            session_id="session-1",
            jsonl_path="/home/dev/.grok/sessions/%2Fworkspace%2Fdemo/session-1/chat_history.jsonl",
            cwd="/workspace/demo",
            source="grok-cli",
            status=SessionStatus.RUNNING,
            confidence=DetectionConfidence.OPEN_FILE,
            first_seen_at=100.0,
            last_seen_at=200.0,
            pids=(4242,),
            last_event_at=190.0,
            last_event_type="updates.jsonl",
            product="grok",
            model="grok-4.6",
            project="/workspace/demo",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            grok_home = root / ".grok"
            grok_home.mkdir(parents=True)
            registry = MultiSessionRegistry(root / "state")
            with (
                mock.patch(
                    "token_monitor.dashboard.list_grok_active_sessions",
                    return_value=(session,),
                ),
                mock.patch(
                    "token_monitor.dashboard.read_grok_quota",
                    return_value=None,
                ),
            ):
                state = build_multi_dashboard_state(
                    {"personal": registry},
                    grok_homes=(grok_home,),
                    kimi_homes=(),
                    dsh_homes=(),
                    commandcode_homes=(),
                    claude_homes=(),
                )

        grok_entries = [
            item for item in state["sessions"] if item.get("product") == "grok"
        ]
        self.assertEqual(len(grok_entries), 1)
        entry = grok_entries[0]
        self.assertEqual(entry["session_id"], "session-1")
        self.assertEqual(entry["source"], "grok-cli")
        self.assertEqual(entry["cwd"], "/workspace/demo")
        self.assertEqual(entry["pids"], [4242])
        self.assertEqual(entry["status"], "running")
        self.assertEqual(entry["last_event_type"], "updates.jsonl")
        self.assertTrue(entry["process_backed"])
        self.assertEqual(entry["account"], "grok")
        # 统一会话模型字段
        self.assertEqual(entry["product"], "grok")
        self.assertEqual(entry["model"], "grok-4.6")
        self.assertEqual(entry["project"], "/workspace/demo")
        self.assertEqual(entry["started_at"], 100.0)
        self.assertEqual(entry["last_activity_at"], 190.0)
        self.assertIn("tokens", entry)
        self.assertIn("turns", entry)
        grok_accounts = [
            account for account in state["accounts"] if account.get("product") == "grok"
        ]
        self.assertTrue(grok_accounts)
        self.assertEqual(state["counts"]["active"], 1)


class NoCodexDashboardTests(unittest.TestCase):
    """验证没有 Codex 账号时 Dashboard 与 provider 隔离仍然可用。"""

    def _missing_homes(self, root: Path) -> dict[str, object]:
        missing = root / "missing"
        return {
            "grok_homes": (missing / "grok",),
            "kimi_homes": (missing / "kimi",),
            "dsh_homes": (missing / "dsh",),
            "commandcode_homes": (missing / "commandcode",),
            "claude_homes": (missing / "claude",),
        }

    def test_state_without_any_account_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state = build_multi_dashboard_state({}, **self._missing_homes(root))

        self.assertEqual(state["accounts"], [])
        self.assertEqual(state["sessions"], [])
        self.assertEqual(state["counts"], {})
        self.assertEqual(state["quotas"] if "quotas" in state else [], [])

    def test_server_serves_state_without_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server = DashboardServer(
                registries={},
                config=DashboardConfig(port=0),
                grok_homes=(root / "missing" / "grok",),
                kimi_homes=(root / "missing" / "kimi",),
                dsh_homes=(root / "missing" / "dsh",),
                commandcode_homes=(root / "missing" / "commandcode",),
                claude_homes=(root / "missing" / "claude",),
            )
            server.start()
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with urlopen(f"{base_url}/api/state", timeout=5) as response:
                    status = response.status
                    state = json.load(response)
                with urlopen(f"{base_url}/", timeout=5) as response:
                    html = response.read().decode("utf-8")
            finally:
                server.close()

        self.assertEqual(status, 200)
        self.assertEqual(state["accounts"], [])
        self.assertEqual(state["sessions"], [])
        self.assertIn("Token Monitor", html)

    def test_one_failing_provider_does_not_break_state(self) -> None:
        """单个 provider 读取失败只降级自己，不影响其他 provider。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            grok_home = root / ".grok"
            grok_home.mkdir()
            claude_home = root / ".claude"
            claude_home.mkdir()
            (root / ".claude.json").write_text(
                json.dumps({"userID": "user-abc"}),
                encoding="utf-8",
            )
            with (
                mock.patch(
                    "token_monitor.dashboard.read_grok_account",
                    side_effect=RuntimeError("模拟 Grok 目录损坏"),
                ),
                self.assertLogs("token_monitor.dashboard", level="ERROR") as captured,
            ):
                state = build_multi_dashboard_state(
                    {},
                    grok_homes=(grok_home,),
                    kimi_homes=(root / "missing" / "kimi",),
                    dsh_homes=(root / "missing" / "dsh",),
                    commandcode_homes=(root / "missing" / "commandcode",),
                    claude_homes=(claude_home,),
                )

        products = [item.get("product") for item in state["accounts"]]
        self.assertNotIn("grok", products)
        self.assertIn("claude", products)
        self.assertTrue(
            any("Grok 目录读取失败" in line for line in captured.output),
            captured.output,
        )


class ClaudeSessionDashboardTests(unittest.TestCase):
    """验证 Claude Code 活动会话通过统一适配器进入 Dashboard 状态。"""

    def test_claude_sessions_use_unified_fields(self) -> None:
        session = TrackedSession(
            thread_id="claude:session-1",
            session_id="session-1",
            jsonl_path="/home/dev/.claude/projects/-home-dev-proj/session-1.jsonl",
            cwd="/home/dev/proj",
            source="claude-cli",
            status=SessionStatus.RUNNING,
            confidence=DetectionConfidence.OPEN_FILE,
            first_seen_at=100.0,
            last_seen_at=200.0,
            pids=(9,),
            last_event_at=150.0,
            last_event_type="assistant",
            product="claude",
            model="claude-sonnet-4-5",
            project="/home/dev/proj",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            claude_home = root / ".claude"
            claude_home.mkdir(parents=True)
            registry = MultiSessionRegistry(root / "state")
            with (
                mock.patch(
                    "token_monitor.dashboard.list_claude_active_sessions",
                    return_value=(session,),
                ),
                mock.patch(
                    "token_monitor.dashboard.read_grok_quota",
                    return_value=None,
                ),
            ):
                state = build_multi_dashboard_state(
                    {"personal": registry},
                    grok_homes=(root / "missing" / "grok",),
                    kimi_homes=(root / "missing" / "kimi",),
                    dsh_homes=(root / "missing" / "dsh",),
                    commandcode_homes=(root / "missing" / "commandcode",),
                    claude_homes=(claude_home,),
                )

        entries = [
            item for item in state["sessions"] if item.get("product") == "claude"
        ]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["source"], "claude-cli")
        self.assertEqual(entry["model"], "claude-sonnet-4-5")
        self.assertEqual(entry["project"], "/home/dev/proj")
        self.assertEqual(entry["started_at"], 100.0)
        self.assertEqual(entry["last_activity_at"], 150.0)
        self.assertEqual(entry["pids"], [9])
        claude_accounts = [
            account
            for account in state["accounts"]
            if account.get("product") == "claude"
        ]
        self.assertTrue(claude_accounts)


class UsageSearchDashboardTests(unittest.TestCase):
    """验证用量检索接口和页面入口。"""

    def _aggregator(self, root: Path) -> UsageAggregator:
        """写入一个会话的合成用量索引。"""

        home = root / ".codex"
        session = (
            home
            / "sessions"
            / "2026"
            / "08"
            / "27"
            / "rollout-2026-08-27T01-00-00-33333333-3333-4333-8333-333333333333.jsonl"
        )
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text(
            "\n".join(
                json.dumps(event)
                for event in (
                    {
                        "timestamp": "2026-08-27T01:00:00Z",
                        "type": "session_meta",
                        "payload": {"cwd": "/home/dev/gamma"},
                    },
                    {
                        "timestamp": "2026-08-27T02:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "thread_settings": {"model": "gpt-5.6-luna"},
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 4_000,
                                    "total_tokens": 4_000,
                                }
                            },
                        },
                    },
                )
            )
            + "\n",
            encoding="utf-8",
        )
        aggregator = UsageAggregator(
            discovery_interval=0.01,
            refresh_interval=0.01,
            cache_path=root / "state" / "usage-index.sqlite3",
        )
        aggregator.snapshot(
            {"codex": MultiSessionRegistry(root / "monitor-state")},
            account_metadata={
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(home),
                }
            },
            now=time.time(),
        )
        return aggregator

    def _server(self, root: Path, aggregator: UsageAggregator) -> DashboardServer:
        server = DashboardServer(
            registry=MultiSessionRegistry(root / "monitor-state"),
            config=DashboardConfig(port=0),
            grok_homes=(),
            kimi_homes=(),
            dsh_homes=(),
            commandcode_homes=(),
            claude_homes=(),
            usage_aggregator=aggregator,
        )
        server.start()
        return server

    def test_api_returns_usage_search_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server = self._server(root, self._aggregator(root))
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with urlopen(
                    f"{base_url}/api/usage/search?days=0",
                    timeout=5,
                ) as response:
                    payload = json.load(response)
                with urlopen(
                    f"{base_url}/api/usage/search?days=0&group=model&sort=tokens",
                    timeout=5,
                ) as response:
                    by_model = json.load(response)
                with urlopen(
                    f"{base_url}/api/usage/search?days=0&model=nonexistent",
                    timeout=5,
                ) as response:
                    empty = json.load(response)
            finally:
                server.close()

        self.assertTrue(payload["search"]["available"])
        self.assertEqual(payload["search"]["totals"]["records"], 1)
        self.assertEqual(payload["search"]["totals"]["total_tokens"], 4_000)
        self.assertEqual(
            payload["search"]["rows"][0]["project"],
            "/home/dev/gamma",
        )
        self.assertEqual(payload["facets"]["models"], ["gpt-5.6-luna"])
        self.assertEqual(by_model["search"]["group"], "model")
        self.assertEqual(by_model["search"]["rows"][0]["models"], ["gpt-5.6-luna"])
        self.assertEqual(empty["search"]["matched_rows"], 0)

    def test_api_without_index_reports_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server = self._server(root, UsageAggregator())
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with urlopen(
                    f"{base_url}/api/usage/search",
                    timeout=5,
                ) as response:
                    payload = json.load(response)
                with self.assertRaises(HTTPError) as invalid:
                    urlopen(
                        f"{base_url}/api/usage/search?group=project",
                        timeout=5,
                    )
            finally:
                server.close()

        self.assertFalse(payload["search"]["available"])
        self.assertFalse(payload["facets"]["available"])
        self.assertEqual(invalid.exception.code, 400)

    def test_page_contains_usage_search_section(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server = self._server(root, self._aggregator(root))
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with urlopen(f"{base_url}/", timeout=5) as response:
                    html = response.read().decode("utf-8")
            finally:
                server.close()

        self.assertIn("用量检索", html)
        self.assertIn('id="usage-search"', html)
        self.assertIn('href="#usage-search"', html)
        self.assertIn('data-nav-target="usage-search"', html)
        self.assertIn('id="usage-search-content"', html)
        self.assertIn('data-usage-search-group="date"', html)
        self.assertIn('id="usage-search-model"', html)
        self.assertIn("/api/usage/search", html)
        self.assertLess(html.find('id="usage"'), html.find('id="usage-search"'))
        self.assertLess(html.find('id="insights"'), html.find('id="usage-search"'))
        self.assertIn('data-section-toggle="usage-search"', html)
        self.assertIn('id="usage-search-body"', html)


class HousekeepingDashboardTests(unittest.TestCase):
    """验证磁盘/会话管理接口和长会话提醒。"""

    _SESSION = "66666666-6666-4666-8666-666666666666"

    def _environment(
        self,
        root: Path,
    ) -> tuple[HousekeepingMonitor, UsageAggregator, MultiSessionRegistry]:
        """构造一个带旧会话、长会话和目录占用的临时环境。"""

        home = root / ".codex"
        old_session = (
            home
            / "sessions"
            / "2026"
            / "06"
            / "01"
            / "rollout-2026-06-01T01-00-00-77777777-7777-4777-8777-777777777777.jsonl"
        )
        long_session = (
            home
            / "sessions"
            / "2026"
            / "08"
            / "27"
            / f"rollout-2026-08-27T01-00-00-{self._SESSION}.jsonl"
        )
        long_session.parent.mkdir(parents=True, exist_ok=True)
        long_session.write_text(
            "\n".join(
                json.dumps(event)
                for event in (
                    {
                        "timestamp": "2026-08-27T01:00:00Z",
                        "type": "session_meta",
                        "payload": {"cwd": "/home/dev/iota"},
                    },
                    {
                        "timestamp": "2026-08-27T02:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "thread_settings": {"model": "gpt-5.6-luna"},
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 500_000,
                                    "total_tokens": 500_000,
                                }
                            },
                        },
                    },
                )
            )
            + "\n",
            encoding="utf-8",
        )
        old_session.parent.mkdir(parents=True, exist_ok=True)
        old_session.write_bytes(b"x" * 4096)
        stale = time.time() - 100 * 86_400
        os.utime(old_session, (stale, stale))
        aggregator = UsageAggregator(
            discovery_interval=0.01,
            refresh_interval=0.01,
            cache_path=root / "state" / "usage-index.sqlite3",
        )
        aggregator.snapshot(
            {"codex": MultiSessionRegistry(root / "monitor-state")},
            account_metadata={
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(home),
                }
            },
            now=time.time(),
        )
        monitor = HousekeepingMonitor(
            targets=(
                AuditTarget(
                    label="Codex (codex)",
                    product="codex",
                    path=home,
                    sessions_root=home / "sessions",
                ),
            ),
            thresholds=DiskThresholds.from_gb(
                single_warn_gb=0.000001,
                total_warn_gb=0.000001,
            ),
            archive_dir=root / "state" / "archives",
            # 中文注释：daemon 会把仍在运行的会话路径交进来，归档时必须跳过。
            active_paths=lambda: {str(long_session)},
        )
        registry = MultiSessionRegistry(root / "monitor-state")
        registry.upsert_session(
            TrackedSession(
                thread_id="thread-long",
                session_id=self._SESSION,
                jsonl_path=str(long_session),
                cwd="/home/dev/iota",
                source="cli",
                status=SessionStatus.RUNNING,
                confidence=DetectionConfidence.OPEN_FILE,
                first_seen_at=100,
                last_seen_at=200,
                pids=(4321,),
            )
        )
        return monitor, aggregator, registry

    def _server(
        self,
        root: Path,
        monitor: HousekeepingMonitor,
        aggregator: UsageAggregator,
        registry: MultiSessionRegistry,
    ) -> DashboardServer:
        server = DashboardServer(
            registry=registry,
            config=DashboardConfig(port=0),
            grok_homes=(),
            kimi_homes=(),
            dsh_homes=(),
            commandcode_homes=(),
            claude_homes=(),
            usage_aggregator=aggregator,
            housekeeping=monitor,
        )
        server.start()
        return server

    @staticmethod
    def _post(url: str, payload: object):
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urlopen(request, timeout=5)

    def test_state_reports_long_session_and_disk_pressure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            monitor, aggregator, registry = self._environment(root)
            server = self._server(root, monitor, aggregator, registry)
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with urlopen(f"{base_url}/api/state", timeout=5) as response:
                    state = json.load(response)
                with urlopen(
                    f"{base_url}/api/housekeeping?days=30",
                    timeout=5,
                ) as response:
                    housekeeping = json.load(response)
            finally:
                server.close()

        advice = state["session_advice"]
        self.assertEqual(advice["count"], 1)
        self.assertEqual(advice["sessions"][0]["session_id"], self._SESSION)
        self.assertEqual(advice["sessions"][0]["context_tokens"], 500_000)
        self.assertEqual(
            state["sessions"][0]["usage"]["turns"],
            1,
        )
        self.assertTrue(state["housekeeping"]["available"])
        self.assertTrue(state["housekeeping"]["reminders"])
        self.assertEqual(housekeeping["preview"]["count"], 1)
        self.assertEqual(housekeeping["archives"], [])

    def test_archive_and_restore_through_the_api(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            monitor, aggregator, registry = self._environment(root)
            server = self._server(root, monitor, aggregator, registry)
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with self.assertRaises(HTTPError) as unconfirmed:
                    self._post(
                        f"{base_url}/api/housekeeping",
                        {"action": "archive", "days": 30},
                    )
                with self._post(
                    f"{base_url}/api/housekeeping",
                    {"action": "archive", "days": 30, "confirm": True},
                ) as response:
                    archived = json.load(response)
                archive_name = Path(archived["result"]["archive"]).name
                with self._post(
                    f"{base_url}/api/housekeeping",
                    {"action": "restore", "archive": archive_name},
                ) as response:
                    restored = json.load(response)
                with self.assertRaises(HTTPError) as escaping:
                    self._post(
                        f"{base_url}/api/housekeeping",
                        {"action": "restore", "archive": "../etc/passwd"},
                    )
                with self.assertRaises(HTTPError) as unknown:
                    self._post(
                        f"{base_url}/api/housekeeping",
                        {"action": "rm-rf", "confirm": True},
                    )
            finally:
                server.close()

        self.assertEqual(unconfirmed.exception.code, 400)
        self.assertEqual(archived["result"]["count"], 1)
        self.assertEqual(archived["result"]["deleted"], 1)
        self.assertEqual(restored["result"]["restored"], 1)
        self.assertEqual(escaping.exception.code, 400)
        self.assertEqual(unknown.exception.code, 400)

    def test_async_archive_returns_task_and_progress(self) -> None:
        """归档接口支持后台任务，返回可轮询的任务 id 和进度。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            monitor, aggregator, registry = self._environment(root)
            server = self._server(root, monitor, aggregator, registry)
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with self._post(
                    f"{base_url}/api/housekeeping",
                    {
                        "action": "archive",
                        "days": 30,
                        "confirm": True,
                        "async": True,
                    },
                ) as response:
                    started = json.load(response)
                task_id = started["task"]["id"]
                deadline = time.time() + 30
                status = None
                while time.time() < deadline:
                    with urlopen(
                        f"{base_url}/api/housekeeping?task={task_id}",
                        timeout=5,
                    ) as response:
                        status = json.load(response)
                    if status["task"]["state"] != "running":
                        break
                    time.sleep(0.05)
                with urlopen(
                    f"{base_url}/api/housekeeping?days=30",
                    timeout=5,
                ) as response:
                    refreshed = json.load(response)
            finally:
                server.close()

        self.assertEqual(started["task"]["state"], "running")
        self.assertIsNotNone(status)
        self.assertEqual(status["task"]["state"], "done")
        self.assertEqual(status["task"]["result"]["count"], 1)
        self.assertTrue(status["tasks"])
        self.assertEqual(refreshed["preview"]["count"], 0)
        self.assertEqual(len(refreshed["archives"]), 1)

    def test_active_sessions_expose_archive_capability(self) -> None:
        """活动会话表要给出能否单独归档，并附上原因。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            monitor, aggregator, registry = self._environment(root)
            running_jsonl = None
            for session in registry.list_sessions():
                if session.pids:
                    running_jsonl = session.jsonl_path
            # 追加一个已结束、可归档的会话
            finished = (
                root
                / ".codex"
                / "sessions"
                / "2026"
                / "09"
                / "10"
                / "rollout-2026-09-10T01-00-00-99999999-8888-4777-8666-555544443333.jsonl"
            )
            finished.parent.mkdir(parents=True, exist_ok=True)
            finished.write_bytes(b"x" * 4096)
            stamp = time.time() - 12 * 86_400
            os.utime(finished, (stamp, stamp))
            registry.upsert_session(
                TrackedSession(
                    thread_id="thread-finished",
                    session_id="99999999-8888-4777-8666-555544443333",
                    jsonl_path=str(finished),
                    cwd="/home/dev/iota",
                    source="cli",
                    status=SessionStatus.COMPLETED,
                    confidence=DetectionConfidence.OPEN_FILE,
                    first_seen_at=100,
                    last_seen_at=time.time() - 60,
                )
            )
            server = self._server(root, monitor, aggregator, registry)
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with urlopen(f"{base_url}/api/state", timeout=5) as response:
                    state = json.load(response)
            finally:
                server.close()

        by_id = {item["session_id"]: item for item in state["sessions"]}
        self.assertIn("99999999-8888-4777-8666-555544443333", by_id)
        finished_entry = by_id["99999999-8888-4777-8666-555544443333"]
        self.assertTrue(finished_entry["archive"]["eligible"])
        self.assertFalse(finished_entry["active"])
        self.assertEqual(state["counts"]["recent"], 1)
        running = next(item for item in state["sessions"] if item["pids"])
        self.assertFalse(running["archive"]["eligible"])
        self.assertEqual(running["archive"]["reason"], "会话仍在运行")
        self.assertEqual(running["jsonl_path"], running_jsonl)

    def test_archive_single_session_through_the_api(self) -> None:
        """POST 指定 session 时只归档该会话，并拒绝不可归档的路径。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            monitor, aggregator, registry = self._environment(root)
            finished = (
                root
                / ".codex"
                / "sessions"
                / "2026"
                / "09"
                / "10"
                / "rollout-2026-09-10T01-00-00-77777777-8888-4777-8666-555544443333.jsonl"
            )
            finished.parent.mkdir(parents=True, exist_ok=True)
            finished.write_bytes(b"y" * 2048)
            stamp = time.time() - 20 * 86_400
            os.utime(finished, (stamp, stamp))
            server = self._server(root, monitor, aggregator, registry)
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with self._post(
                    f"{base_url}/api/housekeeping",
                    {
                        "action": "archive",
                        "session": str(finished),
                        "confirm": True,
                        "async": True,
                    },
                ) as response:
                    started = json.load(response)
                task_id = started["task"]["id"]
                deadline = time.time() + 30
                status = None
                while time.time() < deadline:
                    with urlopen(
                        f"{base_url}/api/housekeeping?task={task_id}",
                        timeout=5,
                    ) as response:
                        status = json.load(response)
                    if status["task"]["state"] != "running":
                        break
                    time.sleep(0.05)
                with self.assertRaises(HTTPError) as outside:
                    self._post(
                        f"{base_url}/api/housekeeping",
                        {
                            "action": "archive",
                            "session": str(root / "nope.jsonl"),
                            "confirm": True,
                        },
                    )
            finally:
                server.close()
            archived_exists = finished.exists()

        self.assertEqual(status["task"]["state"], "done")
        self.assertEqual(status["task"]["result"]["count"], 1)
        self.assertFalse(archived_exists)
        self.assertEqual(outside.exception.code, 400)

    def test_page_contains_housekeeping_section(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            monitor, aggregator, registry = self._environment(root)
            server = self._server(root, monitor, aggregator, registry)
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with urlopen(f"{base_url}/", timeout=5) as response:
                    html = response.read().decode("utf-8")
            finally:
                server.close()

        self.assertIn("磁盘与会话管理", html)
        self.assertIn('id="housekeeping"', html)
        self.assertIn('href="#housekeeping"', html)
        self.assertIn('id="housekeeping-content"', html)
        self.assertIn('id="housekeeping-archive-button"', html)
        self.assertIn('id="housekeeping-clean-button"', html)
        self.assertIn("建议开新会话", html)
        self.assertIn("轮数 / 上下文", html)
        self.assertIn("data-archive-session", html)
        self.assertIn("归档此会话", html)
        self.assertIn('id="session-notice"', html)
        self.assertLess(html.find('id="insights"'), html.find('id="housekeeping"'))
        self.assertIn('data-section-toggle="housekeeping"', html)
        self.assertIn('id="housekeeping-body"', html)
        self.assertIn("按需查看", html)


class ScanDirsDashboardTests(unittest.TestCase):
    """验证扫描目录的查询与在线管理接口。"""

    _CLI_HOMES = {
        "codex": None,
        "claude": None,
        "commandcode": None,
        "dsh": None,
        "grok": None,
        "kimi": None,
    }

    @staticmethod
    def _home(root: Path) -> Path:
        """构造一个带 Codex 典型目录结构的假主目录。"""

        home = root / "home"
        (home / ".codex" / "sessions").mkdir(parents=True)
        return home

    def _controller(self, root: Path, home: Path) -> ScanDirsController:
        """在临时目录上构造扫描目录控制器；校验范围指向假主目录。"""

        return ScanDirsController(
            state_dir=root / "state",
            cli_homes=dict(self._CLI_HOMES),
            home_dir=home,
        )

    def _server(
        self,
        root: Path,
        controller: ScanDirsController | None,
    ) -> DashboardServer:
        """启动一个只绑定回环随机端口的 Dashboard。"""

        server = DashboardServer(
            registries={},
            config=DashboardConfig(port=0),
            grok_homes=(),
            kimi_homes=(),
            dsh_homes=(),
            commandcode_homes=(),
            claude_homes=(),
            scan_dirs=controller,
        )
        server.start()
        return server

    @staticmethod
    def _post(url: str, payload: object) -> tuple[int, dict]:
        """发送 JSON POST 请求；出错状态码也解析响应体后返回。"""

        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    @staticmethod
    def _provider(payload: dict, key: str) -> dict:
        """从快照里取出单个 provider 的条目。"""

        for provider in payload["providers"]:
            if provider["key"] == key:
                return provider
        raise AssertionError(f"快照中缺少 provider: {key}")

    def test_get_returns_snapshot_with_all_providers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = self._home(root)
            server = self._server(root, self._controller(root, home))
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with urlopen(f"{base_url}/api/scan-dirs", timeout=5) as response:
                    payload = json.load(response)
                    self.assertEqual(response.status, 200)
                head_request = Request(
                    f"{base_url}/api/scan-dirs",
                    method="HEAD",
                )
                with urlopen(head_request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"")
            finally:
                server.close()

        self.assertTrue(payload["available"])
        self.assertIn("updated_at", payload)
        self.assertEqual(payload["priority"], ["web", "cli", "auto"])
        self.assertEqual(len(payload["providers"]), 6)
        self.assertEqual(
            [provider["key"] for provider in payload["providers"]],
            ["codex", "claude", "commandcode", "dsh", "grok", "kimi"],
        )
        codex = self._provider(payload, "codex")
        self.assertEqual(codex["source"], "auto")
        self.assertIsNone(codex["override_dirs"])
        self.assertEqual(codex["cli_option"], "--codex-home")

    def test_get_without_controller_reports_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server = self._server(root, None)
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with urlopen(f"{base_url}/api/scan-dirs", timeout=5) as response:
                    payload = json.load(response)
                    self.assertEqual(response.status, 200)
            finally:
                server.close()

        self.assertFalse(payload["available"])
        self.assertEqual(payload["providers"], [])

    def test_add_remove_reset_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = self._home(root)
            extra = home / ".codex-work"
            (extra / "sessions").mkdir(parents=True)
            server = self._server(root, self._controller(root, home))
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                add_status, added = self._post(
                    f"{base_url}/api/scan-dirs",
                    {"action": "add", "provider": "codex", "path": str(extra)},
                )
                outside_status, outside = self._post(
                    f"{base_url}/api/scan-dirs",
                    {"action": "add", "provider": "codex", "path": "/etc"},
                )
                unconfirmed_status, unconfirmed = self._post(
                    f"{base_url}/api/scan-dirs",
                    {
                        "action": "remove",
                        "provider": "codex",
                        "path": str(extra),
                    },
                )
                remove_status, removed = self._post(
                    f"{base_url}/api/scan-dirs",
                    {
                        "action": "remove",
                        "provider": "codex",
                        "path": str(extra),
                        "confirm": True,
                    },
                )
                reset_status, reset = self._post(
                    f"{base_url}/api/scan-dirs",
                    {"action": "reset", "provider": "codex", "confirm": True},
                )
                persisted = (root / "state" / "scan-dirs.json").is_file()
            finally:
                server.close()

        self.assertEqual(add_status, 200)
        self.assertTrue(added["ok"])
        self.assertEqual(added["action"], "add")
        codex = self._provider(added, "codex")
        self.assertEqual(codex["source"], "web")
        self.assertEqual(codex["override_dirs"], [str(extra)])
        self.assertEqual(codex["directories"][0]["path"], str(extra))
        self.assertTrue(codex["directories"][0]["ok"])
        self.assertTrue(codex["directories"][0]["structure_ok"])
        self.assertTrue(persisted)

        self.assertEqual(outside_status, 400)
        self.assertEqual(outside["error"], "invalid_scan_dir")
        self.assertIn("主目录", outside["message"])

        self.assertEqual(unconfirmed_status, 400)
        self.assertEqual(unconfirmed["error"], "invalid_scan_dir_action")

        self.assertEqual(remove_status, 200)
        self.assertTrue(removed["ok"])
        codex_after_remove = self._provider(removed, "codex")
        self.assertEqual(codex_after_remove["override_dirs"], [])
        self.assertEqual(codex_after_remove["directories"], [])
        self.assertFalse(codex_after_remove["enabled"])

        self.assertEqual(reset_status, 200)
        self.assertTrue(reset["ok"])
        codex_after_reset = self._provider(reset, "codex")
        self.assertEqual(codex_after_reset["source"], "auto")
        self.assertIsNone(codex_after_reset["override_dirs"])

    def test_reset_requires_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = self._home(root)
            server = self._server(root, self._controller(root, home))
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                status, payload = self._post(
                    f"{base_url}/api/scan-dirs",
                    {"action": "reset", "provider": "codex"},
                )
            finally:
                server.close()

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_scan_dir_action")

    def test_invalid_requests_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = self._home(root)
            server = self._server(root, self._controller(root, home))
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                unknown_action = self._post(
                    f"{base_url}/api/scan-dirs",
                    {"action": "wipe", "provider": "codex"},
                )
                unknown_provider = self._post(
                    f"{base_url}/api/scan-dirs",
                    {
                        "action": "add",
                        "provider": "emacs",
                        "path": str(home / ".emacs"),
                    },
                )
                non_string_path = self._post(
                    f"{base_url}/api/scan-dirs",
                    {"action": "add", "provider": "codex", "path": 123},
                )
                missing_path = self._post(
                    f"{base_url}/api/scan-dirs",
                    {"action": "add", "provider": "codex"},
                )
            finally:
                server.close()

        for status, payload in (
            unknown_action,
            unknown_provider,
            non_string_path,
            missing_path,
        ):
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], "invalid_scan_dir_action")

    def test_post_without_controller_returns_503(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server = self._server(root, None)
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                status, payload = self._post(
                    f"{base_url}/api/scan-dirs",
                    {"action": "reset", "provider": "codex", "confirm": True},
                )
            finally:
                server.close()

        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "scan_dirs_unavailable")

    def test_page_contains_scan_dirs_section(self) -> None:
        """主页只保留指向 /settings 的链接；扫描目录管理在独立设置页。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = self._home(root)
            server = self._server(root, self._controller(root, home))
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with urlopen(f"{base_url}/", timeout=5) as response:
                    html = response.read().decode("utf-8")
                with urlopen(f"{base_url}/settings", timeout=5) as response:
                    settings_html = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                head_request = Request(f"{base_url}/settings", method="HEAD")
                with urlopen(head_request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"")
            finally:
                server.close()

        # 主页：设置入口改为指向独立页面的普通链接，不再内嵌扫描目录区块。
        self.assertIn("设置", html)
        self.assertIn('href="/settings"', html)
        self.assertNotIn('data-nav-target="settings"', html)
        self.assertNotIn('id="settings"', html)
        self.assertNotIn('id="scan-dirs"', html)
        self.assertNotIn("/api/scan-dirs", html)

        # 设置页：第一个子块是扫描目录管理，结构与 JS 保持完整。
        self.assertIn("<title>设置 - Token Monitor</title>", settings_html)
        self.assertIn("返回 Dashboard", settings_html)
        self.assertIn('href="/"', settings_html)
        self.assertIn('id="settings-body"', settings_html)
        self.assertIn('id="scan-dirs"', settings_html)
        self.assertIn('id="scan-dirs-content"', settings_html)
        self.assertIn('id="scan-dirs-count"', settings_html)
        self.assertIn('id="scan-dirs-refresh-button"', settings_html)
        self.assertIn("/api/scan-dirs", settings_html)
        self.assertIn("refreshScanDirs", settings_html)
        self.assertIn("优先级：Web 配置", settings_html)
        # 设置页打开时直接加载扫描目录，不再走主页的懒加载机制。
        self.assertLess(
            settings_html.find('id="scan-dirs"'),
            settings_html.find('id="scan-dirs-content"'),
        )
        self.assertLess(
            settings_html.find('id="settings-body"'),
            settings_html.find('id="scan-dirs"'),
        )

    def test_update_accounts_swaps_registries(self) -> None:
        """update_accounts 应在不重启服务的情况下替换 /api/state 的账号集合。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry_a = MultiSessionRegistry(root / "state-a")
            registry_b = MultiSessionRegistry(root / "state-b")
            server = DashboardServer(
                registries={"codex": registry_a},
                config=DashboardConfig(port=0),
                account_metadata={
                    "codex": {
                        "account_id": "account-a",
                        "profile_name": "codex",
                    }
                },
                grok_homes=(),
                kimi_homes=(),
                dsh_homes=(),
                commandcode_homes=(),
                claude_homes=(),
            )
            server.start()
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with urlopen(f"{base_url}/api/state", timeout=5) as response:
                    before = json.load(response)
                server.update_accounts(
                    {"codex": registry_b},
                    {
                        "codex": {
                            "account_id": "account-b",
                            "profile_name": "codex",
                        }
                    },
                )
                with urlopen(f"{base_url}/api/state", timeout=5) as response:
                    after = json.load(response)
            finally:
                server.close()

        self.assertEqual(
            [account["name"] for account in before["accounts"]],
            ["account-a"],
        )
        self.assertEqual(
            [account["name"] for account in after["accounts"]],
            ["account-b"],
        )
        self.assertEqual(server.registry, registry_b)


class HealthEndpointTests(unittest.TestCase):
    """验证 /healthz、/readyz 与 /api/state 的健康上报。"""

    def _server(self, health: HealthTracker | None = None, **kwargs) -> DashboardServer:
        """启动一个只绑定回环随机端口的 Dashboard。"""

        options = {
            "registries": {},
            "config": DashboardConfig(port=0),
            "grok_homes": (),
            "kimi_homes": (),
            "dsh_homes": (),
            "commandcode_homes": (),
            "claude_homes": (),
            "health": health,
        }
        options.update(kwargs)
        server = DashboardServer(**options)
        server.start()
        return server

    @staticmethod
    def _get(base_url: str, path: str) -> tuple[int, dict]:
        """GET 并解析 JSON；出错状态码也解析响应体后返回。"""

        try:
            with urlopen(f"{base_url}{path}", timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    @staticmethod
    def _head(base_url: str, path: str) -> tuple[int, bytes]:
        """HEAD 请求；返回状态码与（应为空的）响应体。"""

        request = Request(f"{base_url}{path}", method="HEAD")
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    def test_healthz_and_readyz_without_tracker(self) -> None:
        server = self._server()
        base_url = f"http://{server.address[0]}:{server.address[1]}"
        try:
            healthz_status, healthz = self._get(base_url, "/healthz")
            readyz_status, readyz = self._get(base_url, "/readyz")
            head_status, head_body = self._head(base_url, "/healthz")
        finally:
            server.close()

        self.assertEqual(healthz_status, 200)
        self.assertEqual(healthz["status"], "ok")
        self.assertIn("updated_at", healthz)
        self.assertEqual(readyz_status, 200)
        self.assertEqual(readyz["status"], "ok")
        self.assertEqual(head_status, 200)
        self.assertEqual(head_body, b"")

    def test_healthz_ok_when_main_loop_ok(self) -> None:
        tracker = HealthTracker()
        tracker.register("main-loop", "主循环", critical=True)
        tracker.record_success("main-loop")
        server = self._server(health=tracker)
        base_url = f"http://{server.address[0]}:{server.address[1]}"
        try:
            status, payload = self._get(base_url, "/healthz")
            head_status, head_body = self._head(base_url, "/healthz")
        finally:
            server.close()

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertGreaterEqual(payload["uptime_seconds"], 0)
        self.assertIn("updated_at", payload)
        self.assertEqual(head_status, 200)
        self.assertEqual(head_body, b"")

    def test_healthz_stuck_when_main_loop_starting(self) -> None:
        """主循环登记后从未成功（starting）视为卡死。"""

        tracker = HealthTracker()
        tracker.register("main-loop", "主循环", critical=True)
        server = self._server(health=tracker)
        base_url = f"http://{server.address[0]}:{server.address[1]}"
        try:
            status, payload = self._get(base_url, "/healthz")
            head_status, _ = self._head(base_url, "/healthz")
        finally:
            server.close()

        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "stuck")
        self.assertEqual(payload["main_loop"], "starting")
        self.assertEqual(head_status, 503)

    def test_healthz_stuck_when_main_loop_degraded(self) -> None:
        """主循环数据过期（degraded）同样视为卡死。"""

        tracker = HealthTracker()
        tracker.register("main-loop", "主循环", critical=True, stale_after=30)
        tracker.record_success("main-loop", now=time.time() - 120)
        server = self._server(health=tracker)
        base_url = f"http://{server.address[0]}:{server.address[1]}"
        try:
            status, payload = self._get(base_url, "/healthz")
        finally:
            server.close()

        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "stuck")
        self.assertEqual(payload["main_loop"], "degraded")

    def test_readyz_ignores_non_critical_provider_failure(self) -> None:
        """非关键 provider failed 只拉低 overall，不影响就绪判定。"""

        tracker = HealthTracker()
        tracker.register("main-loop", "主循环", critical=True)
        tracker.record_success("main-loop")
        tracker.record_failure("provider:grok", RuntimeError("模拟 Grok 读取失败"))
        server = self._server(health=tracker)
        base_url = f"http://{server.address[0]}:{server.address[1]}"
        try:
            status, payload = self._get(base_url, "/readyz")
        finally:
            server.close()

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["overall"], "failed")
        grok = next(
            item for item in payload["components"] if item["key"] == "provider:grok"
        )
        self.assertEqual(grok["status"], "failed")
        self.assertFalse(grok["critical"])
        self.assertIn("模拟 Grok 读取失败", grok["last_error"])

    def test_readyz_not_ready_when_critical_failed(self) -> None:
        tracker = HealthTracker()
        tracker.register("main-loop", "主循环", critical=True)
        tracker.record_failure("main-loop", RuntimeError("模拟主循环崩溃"))
        server = self._server(health=tracker)
        base_url = f"http://{server.address[0]}:{server.address[1]}"
        try:
            status, payload = self._get(base_url, "/readyz")
        finally:
            server.close()

        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "not_ready")
        self.assertEqual(payload["overall"], "failed")
        main_loop = next(
            item for item in payload["components"] if item["key"] == "main-loop"
        )
        self.assertTrue(main_loop["critical"])
        self.assertEqual(main_loop["status"], "failed")

    def test_state_includes_health_snapshot(self) -> None:
        tracker = HealthTracker()
        tracker.register("main-loop", "主循环", critical=True)
        tracker.record_success("main-loop")
        server = self._server(health=tracker)
        base_url = f"http://{server.address[0]}:{server.address[1]}"
        try:
            status, payload = self._get(base_url, "/api/state")
        finally:
            server.close()

        self.assertEqual(status, 200)
        self.assertIn("health", payload)
        self.assertEqual(payload["health"]["overall"], "ok")
        self.assertIn("uptime_seconds", payload["health"])
        keys = {item["key"] for item in payload["health"]["components"]}
        self.assertIn("main-loop", keys)

    def test_state_health_field_none_without_tracker(self) -> None:
        server = self._server()
        base_url = f"http://{server.address[0]}:{server.address[1]}"
        try:
            status, payload = self._get(base_url, "/api/state")
        finally:
            server.close()

        self.assertEqual(status, 200)
        self.assertIsNone(payload.get("health"))

    def test_provider_failure_recorded_and_state_still_200(self) -> None:
        """provider 读取抛错时记为 failed，且 /api/state 本身仍返回 200。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            grok_home = root / ".grok"
            grok_home.mkdir()
            tracker = HealthTracker()
            server = self._server(health=tracker, grok_homes=(grok_home,))
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                with (
                    mock.patch(
                        "token_monitor.dashboard.read_grok_account",
                        side_effect=RuntimeError("模拟 Grok 目录损坏"),
                    ),
                    self.assertLogs("token_monitor.dashboard", level="ERROR"),
                ):
                    status, payload = self._get(base_url, "/api/state")
            finally:
                server.close()

        self.assertEqual(status, 200)
        self.assertEqual(tracker.component_status("provider:grok"), "failed")
        products = [item.get("product") for item in payload["accounts"]]
        self.assertNotIn("grok", products)

    def test_housekeeping_refresh_failure_degrades_state(self) -> None:
        """housekeeping.refresh 抛错时 /api/state 仍 200，磁盘摘要降级为不可用。"""

        class FailingHousekeeping:
            def latest(self) -> dict:
                return {"observed_at": None}

            def refresh(self) -> dict:
                raise OSError("模拟磁盘不可读")

        server = self._server(housekeeping=FailingHousekeeping())
        base_url = f"http://{server.address[0]}:{server.address[1]}"
        try:
            with self.assertLogs("token_monitor.dashboard", level="ERROR"):
                status, payload = self._get(base_url, "/api/state")
        finally:
            server.close()

        self.assertEqual(status, 200)
        self.assertFalse(payload["housekeeping"]["available"])

    def test_page_contains_health_indicator(self) -> None:
        self.assertIn('id="health-indicator"', _DASHBOARD_HTML)
        self.assertIn('id="health-detail"', _DASHBOARD_HTML)
        self.assertIn("renderHealth", _DASHBOARD_HTML)


class HistoryDashboardTests(unittest.TestCase):
    """验证历史数据管理的查询、保留期配置与清理端点。

    中文注释:retention.py 依赖的 store 方法(如 registry.count_sessions)
    由并行开发实现,这里用轻量 fake manager 验证端点行为,保留期控制
    使用真实的 RetentionController(只依赖 settings.json)。
    """

    class _FakeHistoryManager:
        """实现 HistoryDataManager 的鸭子类型接口。"""

        def __init__(self) -> None:
            self._last_cleanup: dict | None = None
            self.fail_cleanup = False

        @property
        def retention_days(self) -> dict[str, float]:
            return {"usage_days": 90.0, "session_days": 30.0, "alert_days": 14.0}

        @property
        def last_cleanup(self) -> dict | None:
            return self._last_cleanup

        def db_sizes(self) -> list[dict]:
            return [{"key": "usage-index", "label": "用量索引", "bytes": 2048}]

        def preview(self, now: float | None = None) -> dict:
            return {
                "observed_at": time.time(),
                "kinds": [
                    {
                        "kind": "usage",
                        "cutoff": 1000.0,
                        "rows_to_delete": 3,
                        "total_rows": 10,
                        "db_bytes": 2048,
                        "estimated_free_bytes": 600,
                    }
                ],
                "dbs": self.db_sizes(),
                "estimated_free_bytes": 600,
            }

        def cleanup(self, now: float | None = None) -> dict:
            result: dict = {
                "observed_at": time.time(),
                "deleted": {"usage": 3, "sessions": 1, "alerts": 0},
                "freed_bytes": 700,
                "vacuumed": ["usage"],
                "vacuum_skipped": [],
                "errors": [],
            }
            if self.fail_cleanup:
                result["errors"] = ["用量索引删除失败: 模拟磁盘错误"]
                self._last_cleanup = result
                raise RetentionError("用量索引删除失败: 模拟磁盘错误")
            self._last_cleanup = result
            return result

    def _server(
        self,
        root: Path,
        *,
        manager: "_FakeHistoryManager | None" = None,
        with_controller: bool = True,
    ) -> tuple[DashboardServer, "_FakeHistoryManager | None", RetentionController | None]:
        """启动带 fake manager 和真实保留期控制器的回环 Dashboard。"""

        controller = (
            RetentionController(
                state_dir=root / "state",
                cli_values={"usage_days": 90.0, "session_days": 30.0},
            )
            if with_controller
            else None
        )
        server = DashboardServer(
            registries={},
            config=DashboardConfig(port=0),
            grok_homes=(),
            kimi_homes=(),
            dsh_homes=(),
            commandcode_homes=(),
            claude_homes=(),
            history=manager,
            retention=controller,
        )
        server.start()
        return server, manager, controller

    @staticmethod
    def _get(base_url: str, path: str) -> tuple[int, dict]:
        try:
            with urlopen(f"{base_url}{path}", timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    @staticmethod
    def _post(base_url: str, path: str, payload: object) -> tuple[int, dict]:
        request = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_get_without_manager_reports_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server, _, _ = self._server(root, manager=None)
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                status, payload = self._get(base_url, "/api/history")
                head_request = Request(f"{base_url}/api/history", method="HEAD")
                with urlopen(head_request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"")
            finally:
                server.close()

        self.assertEqual(status, 200)
        self.assertFalse(payload["available"])
        self.assertIn("updated_at", payload)

    def test_get_returns_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = self._FakeHistoryManager()
            server, _, _ = self._server(root, manager=manager)
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                status, payload = self._get(base_url, "/api/history")
            finally:
                server.close()

        self.assertEqual(status, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(
            payload["retention_days"],
            {"usage_days": 90.0, "session_days": 30.0, "alert_days": 14.0},
        )
        self.assertEqual(payload["dbs"][0]["key"], "usage-index")
        self.assertIsNone(payload["last_cleanup"])
        self.assertNotIn("preview", payload)
        retention = payload["retention"]
        self.assertEqual(retention["usage_days"]["value"], 90.0)
        self.assertEqual(retention["usage_days"]["source"], "cli")
        self.assertIsNone(retention["usage_days"]["override"])
        self.assertEqual(retention["session_days"]["source"], "cli")

    def test_get_with_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server, _, _ = self._server(root, manager=self._FakeHistoryManager())
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                status, payload = self._get(base_url, "/api/history?preview=1")
            finally:
                server.close()

        self.assertEqual(status, 200)
        preview = payload["preview"]
        self.assertEqual(preview["kinds"][0]["kind"], "usage")
        self.assertEqual(preview["kinds"][0]["rows_to_delete"], 3)
        self.assertEqual(preview["estimated_free_bytes"], 600)

    def test_cleanup_requires_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server, _, _ = self._server(root, manager=self._FakeHistoryManager())
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                status, payload = self._post(
                    base_url, "/api/history", {"action": "cleanup"}
                )
            finally:
                server.close()

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_history_action")

    def test_cleanup_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = self._FakeHistoryManager()
            server, _, _ = self._server(root, manager=manager)
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                status, payload = self._post(
                    base_url,
                    "/api/history",
                    {"action": "cleanup", "confirm": True},
                )
                get_status, state = self._get(base_url, "/api/history")
            finally:
                server.close()

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["deleted"]["usage"], 3)
        self.assertEqual(payload["result"]["freed_bytes"], 700)
        self.assertEqual(get_status, 200)
        self.assertIsNotNone(state["last_cleanup"])
        self.assertEqual(state["last_cleanup"]["deleted"]["sessions"], 1)

    def test_cleanup_partial_failure_returns_500(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = self._FakeHistoryManager()
            manager.fail_cleanup = True
            server, _, _ = self._server(root, manager=manager)
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                status, payload = self._post(
                    base_url,
                    "/api/history",
                    {"action": "cleanup", "confirm": True},
                )
            finally:
                server.close()

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], "history_cleanup_failed")
        self.assertIn("模拟磁盘错误", payload["message"])
        self.assertEqual(payload["result"]["deleted"]["usage"], 3)
        self.assertEqual(payload["result"]["errors"], ["用量索引删除失败: 模拟磁盘错误"])

    def test_set_retention_persists_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server, _, _ = self._server(root, manager=self._FakeHistoryManager())
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                status, payload = self._post(
                    base_url,
                    "/api/history",
                    {"action": "set-retention", "usage_days": 45, "session_days": 10},
                )
            finally:
                server.close()
            reloaded = RetentionController(
                state_dir=root / "state",
                cli_values={"usage_days": 90.0, "session_days": 30.0},
            ).effective()

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["retention"]["usage_days"]["value"], 45)
        self.assertEqual(payload["retention"]["usage_days"]["source"], "web")
        self.assertEqual(payload["retention"]["session_days"]["value"], 10)
        # 中文注释:配置已落盘,新控制器能读到同样的覆盖。
        self.assertEqual(reloaded, {"usage_days": 45.0, "session_days": 10.0})

    def test_set_retention_rejects_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server, _, _ = self._server(root, manager=self._FakeHistoryManager())
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                missing_status, missing = self._post(
                    base_url, "/api/history", {"action": "set-retention"}
                )
                bad_type_status, bad_type = self._post(
                    base_url,
                    "/api/history",
                    {"action": "set-retention", "usage_days": "abc"},
                )
                bad_range_status, bad_range = self._post(
                    base_url,
                    "/api/history",
                    {"action": "set-retention", "session_days": -5},
                )
            finally:
                server.close()

        for status in (missing_status, bad_type_status, bad_range_status):
            self.assertEqual(status, 400)
        for payload in (missing, bad_type, bad_range):
            self.assertEqual(payload["error"], "invalid_retention")

    def test_reset_retention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server, _, _ = self._server(root, manager=self._FakeHistoryManager())
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                self._post(
                    base_url,
                    "/api/history",
                    {"action": "set-retention", "usage_days": 45},
                )
                no_confirm_status, no_confirm = self._post(
                    base_url, "/api/history", {"action": "reset-retention"}
                )
                status, payload = self._post(
                    base_url,
                    "/api/history",
                    {"action": "reset-retention", "confirm": True},
                )
            finally:
                server.close()

        self.assertEqual(no_confirm_status, 400)
        self.assertEqual(no_confirm["error"], "invalid_history_action")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["retention"]["usage_days"]["value"], 90.0)
        self.assertEqual(payload["retention"]["usage_days"]["source"], "cli")

    def test_set_retention_without_controller_returns_503(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server, _, _ = self._server(
                root,
                manager=self._FakeHistoryManager(),
                with_controller=False,
            )
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                status, payload = self._post(
                    base_url,
                    "/api/history",
                    {"action": "set-retention", "usage_days": 45},
                )
            finally:
                server.close()

        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "retention_unavailable")

    def test_post_without_manager_returns_503(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server, _, _ = self._server(root, manager=None)
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                status, payload = self._post(
                    base_url,
                    "/api/history",
                    {"action": "cleanup", "confirm": True},
                )
            finally:
                server.close()

        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "history_unavailable")

    def test_unknown_action_returns_400(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server, _, _ = self._server(root, manager=self._FakeHistoryManager())
            base_url = f"http://{server.address[0]}:{server.address[1]}"
            try:
                status, payload = self._post(
                    base_url, "/api/history", {"action": "explode"}
                )
            finally:
                server.close()

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_history_action")

    def test_settings_page_contains_history_section(self) -> None:
        self.assertIn('id="history-settings"', _SETTINGS_HTML)
        self.assertIn('id="history-content"', _SETTINGS_HTML)
        self.assertIn('id="history-preview-content"', _SETTINGS_HTML)
        self.assertIn("refreshHistory", _SETTINGS_HTML)
        self.assertIn("/api/history", _SETTINGS_HTML)
        # 主页不出现历史数据设置区块。
        self.assertNotIn('id="history-settings"', _DASHBOARD_HTML)
        self.assertNotIn("/api/history", _DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
