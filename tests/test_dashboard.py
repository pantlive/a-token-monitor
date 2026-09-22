"""本地只读 Dashboard 的 HTTP 接口测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from token_monitor.dashboard import (
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
                    self.assertIn("Codex Reset Monitor", html)
                    self.assertIn("用量与成本估算", html)
                    self.assertIn("API 等价金额", html)
                    self.assertIn("Dashboard 导航", html)
                    self.assertIn('href="#accounts"', html)
                    self.assertIn('href="#usage"', html)
                    self.assertIn('href="#traffic"', html)
                    self.assertIn("异常流量监控", html)
                    self.assertLess(html.find('href="#traffic"'), html.find('href="#overview"'))
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


if __name__ == "__main__":
    unittest.main()
