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
    DashboardConfig,
    DashboardServer,
    build_multi_dashboard_state,
)
from token_monitor.multi_models import (
    DetectionConfidence,
    SessionStatus,
    TrackedSession,
)
from token_monitor.quota import QuotaSnapshot, QuotaWindow
from token_monitor.registry import MultiSessionRegistry
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
                deadline = time.time() + 10
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
                deadline = time.time() + 10
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


if __name__ == "__main__":
    unittest.main()
