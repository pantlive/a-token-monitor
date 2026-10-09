"""生成 README 用的 Dashboard 截图（演示数据，不读取本机任何真实账号或会话）。

用法（需要 Playwright 与 Chromium，只在本地生成截图时用，不是项目依赖）::

    python -m pip install playwright && python -m playwright install chromium
    python docs/screenshots/generate.py

脚本在临时目录里造一套演示数据，再启动真实的 Dashboard：

* 用量、习惯分析、用量检索：合成 Codex / Claude Code / Grok 会话日志，走真实的解析与
  索引流程；
* 额度、活动会话、流量告警：需要真实凭据、运行中的进程或网络流量，这里通过注册表与
  告警库的正式接口注入演示值。

截图输出到本目录，``<name>-en.png`` 与 ``<name>-zh.png`` 分别给英文 / 中文 README 使用。
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from a_token_monitor.alerts import TrafficAlertStore  # noqa: E402
from a_token_monitor.claude import ClaudeAccount  # noqa: E402
from a_token_monitor.dashboard import DashboardConfig, DashboardServer  # noqa: E402
from a_token_monitor.grok import GrokAccount  # noqa: E402
from a_token_monitor.kimi import KimiAccount  # noqa: E402
from a_token_monitor.local_time import set_local_timezone  # noqa: E402
from a_token_monitor.multi_models import (  # noqa: E402
    DetectionConfidence,
    SessionStatus,
    TrackedSession,
)
from a_token_monitor.providers import PROVIDER_SPECS  # noqa: E402
from a_token_monitor.quota import QuotaSnapshot, QuotaWindow  # noqa: E402
from a_token_monitor.registry import MultiSessionRegistry  # noqa: E402
from a_token_monitor.traffic import (  # noqa: E402
    ConnectionTraffic,
    ProcessTraffic,
    TrafficAlert,
    TrafficSnapshot,
    TrafficThresholds,
)
from a_token_monitor.usage import UsageAggregator  # noqa: E402

OUTPUT = Path(__file__).resolve().parent
NOW = time.time()
RANDOM = random.Random(20261009)
PROJECTS = ("/home/dev/web-app", "/home/dev/api-server", "/home/dev/data-pipeline")


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _working_hours(days: int) -> list[float]:
    """过去 ``days`` 天里落在工作时段的若干请求时间点。"""

    moments = []
    for day in range(days):
        base = NOW - day * 86_400
        for _ in range(RANDOM.randint(3, 9)):
            hour = RANDOM.choice((9, 10, 11, 14, 15, 16, 17, 20, 21))
            moment = datetime.fromtimestamp(base, tz=timezone.utc).replace(
                hour=hour, minute=RANDOM.randint(0, 59)
            ).timestamp() - 8 * 3600
            if moment < NOW - 60:
                moments.append(moment)
    return sorted(moments)


def write_codex(home: Path) -> list[Path]:
    """合成 Codex rollout 日志（累计 token_count 事件）。"""

    paths = []
    for index, start in enumerate(_working_hours(30)[::3]):
        started = datetime.fromtimestamp(start, tz=timezone.utc)
        folder = home / "sessions" / started.strftime("%Y/%m/%d")
        folder.mkdir(parents=True, exist_ok=True)
        session_id = str(uuid.UUID(int=RANDOM.getrandbits(128)))
        path = folder / f"rollout-{started.strftime('%Y-%m-%dT%H-%M-%S')}-{session_id}.jsonl"
        model = RANDOM.choice(("gpt-6.1-sol", "gpt-6.1-sol", "gpt-6-luna"))
        cwd = PROJECTS[index % len(PROJECTS)]
        lines = [
            {"timestamp": _iso(start), "type": "session_meta", "payload": {"id": session_id, "cwd": cwd}},
            {"timestamp": _iso(start + 1), "type": "turn_context", "payload": {"model": model, "cwd": cwd}},
        ]
        total = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
                 "reasoning_output_tokens": 0, "total_tokens": 0}
        for turn in range(RANDOM.randint(3, 14)):
            fresh = RANDOM.randint(1_500, 9_000)
            cached = int(fresh * RANDOM.uniform(1.0, 3.5))
            output = RANDOM.randint(800, 6_000)
            total["input_tokens"] += fresh + cached
            total["cached_input_tokens"] += cached
            total["output_tokens"] += output
            total["reasoning_output_tokens"] += output // 3
            total["total_tokens"] += fresh + cached + output
            lines.append({
                "timestamp": _iso(start + 60 * (turn + 1)),
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {"total_token_usage": dict(total)}},
            })
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
        paths.append(path)
    return paths


def write_claude(home: Path) -> None:
    """合成 Claude Code 项目会话（每条 assistant 记录带 usage）。"""

    for index, start in enumerate(_working_hours(30)[1::3]):
        project = PROJECTS[(index + 1) % len(PROJECTS)]
        folder = home / "projects" / project.replace("/", "-")
        folder.mkdir(parents=True, exist_ok=True)
        session_id = str(uuid.UUID(int=RANDOM.getrandbits(128)))
        model = RANDOM.choice(("claude-opus-4-5", "claude-sonnet-4-5", "claude-sonnet-4-5"))
        lines = []
        for turn in range(RANDOM.randint(5, 24)):
            lines.append({
                "type": "assistant",
                "timestamp": _iso(start + 45 * turn),
                "sessionId": session_id,
                "cwd": project,
                "isSidechain": False,
                "requestId": f"req_{session_id[:8]}_{turn}",
                "message": {
                    "id": f"msg_{session_id[:8]}_{turn}",
                    "model": model,
                    "usage": {
                        "input_tokens": RANDOM.randint(50, 3_000),
                        "output_tokens": RANDOM.randint(300, 4_000),
                        "cache_read_input_tokens": RANDOM.randint(3_000, 26_000),
                        "cache_creation_input_tokens": RANDOM.randint(0, 4_000),
                    },
                },
            })
        (folder / f"{session_id}.jsonl").write_text(
            "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
        )


def write_grok(home: Path) -> None:
    """合成 Grok unified.jsonl 推理记录。"""

    (home / "logs").mkdir(parents=True, exist_ok=True)
    lines = []
    for index, start in enumerate(_working_hours(14)[2::10]):
        session_id = f"session-{index}"
        project = PROJECTS[index % len(PROJECTS)]
        session_dir = home / "sessions" / project.replace("/", "%2F") / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "summary.json").write_text(
            json.dumps({"current_model_id": "grok-4.6", "info": {"cwd": project}}), encoding="utf-8"
        )
        for turn in range(RANDOM.randint(3, 10)):
            lines.append({
                "ts": _iso(start + 30 * turn),
                "msg": "shell.turn.inference_done",
                "sid": session_id,
                "ctx": {
                    "loop_index": turn,
                    "prompt_tokens": RANDOM.randint(3_000, 20_000),
                    "cached_prompt_tokens": RANDOM.randint(1_000, 10_000),
                    "completion_tokens": RANDOM.randint(300, 3_000),
                    "reasoning_tokens": RANDOM.randint(0, 1_500),
                },
            })
    (home / "logs" / "unified.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def _quota(plan: str, five_hour: float, week: float, source: str) -> QuotaSnapshot:
    return QuotaSnapshot(
        observed_at=NOW,
        plan_type=plan,
        source=source,
        windows=(
            QuotaWindow("primary", "primary", five_hour, 300, NOW + 2.4 * 3600),
            QuotaWindow("secondary", "secondary", week, 10_080, NOW + 3.6 * 86_400),
        ),
    )


def _session(product: str, cwd: str, path: Path | None, started: float) -> TrackedSession:
    return TrackedSession(
        thread_id=f"{product}-{uuid.UUID(int=RANDOM.getrandbits(128))}",
        session_id=str(uuid.UUID(int=RANDOM.getrandbits(128))),
        jsonl_path=str(path) if path else None,
        cwd=cwd,
        source="demo",
        status=SessionStatus.RUNNING,
        confidence=DetectionConfidence.OPEN_FILE,
        first_seen_at=started,
        last_seen_at=NOW - RANDOM.randint(5, 90),
        pids=(RANDOM.randint(2_000, 60_000),),
        last_event_at=NOW - RANDOM.randint(5, 90),
        product=product,
    )


def demo_providers(homes: dict[str, Path]) -> dict:
    """额度与活动会话的演示值，经注册表注入（与真实 provider 走同一条展示路径）。"""

    claude = ClaudeAccount(homes["claude"], "alice@example.org", "alice@example.org",
                           has_credentials=True, email="alice@example.org")
    grok = GrokAccount(homes["grok"], "grok-dev", "grok-dev")
    kimi = KimiAccount(homes["kimi"], "kimi-dev", "kimi-dev", logged_in=True)
    return {
        "claude": replace(
            PROVIDER_SPECS["claude"],
            read_account=lambda _home: claude,
            read_quota=lambda _home: _quota("max", 37.0, 61.0, "oauth-usage"),
            active_sessions=lambda _home: (_session("claude", PROJECTS[1], None, NOW - 3_000),),
        ),
        "grok": replace(
            PROVIDER_SPECS["grok"],
            read_account=lambda _home: grok,
            read_quota=lambda _home: _quota("SuperGrok", 12.0, 28.0, "billing"),
            active_sessions=lambda _home: (),
        ),
        "kimi": replace(
            PROVIDER_SPECS["kimi"],
            read_account=lambda _home: kimi,
            read_quota=lambda _home: _quota("Moderato", 54.0, 33.0, "usages"),
            active_sessions=lambda _home: (_session("kimi", PROJECTS[2], None, NOW - 600),),
        ),
    }


def demo_alerts(store: TrafficAlertStore) -> None:
    store.record(
        (
            TrafficAlert("warn", "codex", 41_337, "burst", 11 * 1024 * 1024, 15.0,
                         "15 秒内外发 11.0 MiB", NOW - 3_400, remote="203.0.113.10:443",
                         command="codex", cwd=PROJECTS[0]),
            TrafficAlert("danger", "claude", 52_101, "window", 290 * 1024 * 1024, 300.0,
                         "5 分钟内外发 290.0 MiB", NOW - 86_000, remote="198.51.100.7:443",
                         command="claude", cwd=PROJECTS[1]),
        ),
        now=NOW,
    )


class DemoTraffic:
    """演示用流量监控：返回一份固定快照（真实监控按 /proc 统计进程外发字节）。"""

    def latest(self) -> TrafficSnapshot:
        processes = []
        for index, (product, command, cwd, upload) in enumerate((
            ("codex", "codex", PROJECTS[0], 2_350_000),
            ("claude", "claude", PROJECTS[1], 860_000),
            ("kimi", "kimi", PROJECTS[2], 120_000),
        )):
            pid = 41_337 + index * 977
            processes.append(ProcessTraffic(
                product=product, pid=pid, start_token=str(pid), command=command, cwd=cwd,
                pids=(pid,), external_upload_delta=upload, loopback_upload_delta=0,
                observed_external_bytes=upload * 40, burst_bytes=upload, window_bytes=upload * 9,
                upload_bps=upload / 15, alert_level=None,
                connections=(ConnectionTraffic("203.0.113.10:443", upload * 40, upload * 120,
                                               upload, loopback=False),),
            ))
        return TrafficSnapshot(
            observed_at=NOW, source="proc", thresholds=TrafficThresholds(),
            processes=tuple(processes), alerts=(), interval_seconds=15.0,
        )


def build(root: Path) -> DashboardServer:
    homes = {name: root / f".{name}" for name in ("codex", "claude", "grok", "kimi")}
    for home in homes.values():
        home.mkdir(parents=True)
    codex_paths = write_codex(homes["codex"])
    write_claude(homes["claude"])
    write_grok(homes["grok"])
    (homes["kimi"] / "sessions").mkdir()

    state = root / "state"
    registry = MultiSessionRegistry(state / "codex")
    registry.save_quota(_quota("pro", 23.0, 47.0, "app-server"))
    registry.upsert_session(_session("codex", PROJECTS[0], codex_paths[-1], NOW - 1_800))
    metadata = {"codex": {"account_id": "alice@example.com", "profile_name": "codex",
                          "codex_home": str(homes["codex"]), "plan_type": "pro"}}

    aggregator = UsageAggregator(
        cache_path=state / "usage-index.sqlite3",
        read_budget_bytes=64 * 1024 * 1024,
        homes={"claude": (homes["claude"],), "grok": (homes["grok"],)},
    )
    for _ in range(20):
        snapshot = aggregator.refresh_index({"codex": registry}, metadata)
        if (snapshot.get("indexing") or {}).get("complete"):
            break
    alerts = TrafficAlertStore(state)
    demo_alerts(alerts)
    server = DashboardServer(
        registries={"codex": registry},
        account_metadata=metadata,
        config=DashboardConfig(port=0, budget_usd=200.0),
        usage_aggregator=aggregator,
        homes={key: () for key in PROVIDER_SPECS if key != "codex"}
        | {"claude": (homes["claude"],), "grok": (homes["grok"],), "kimi": (homes["kimi"],)},
        alert_store=alerts,
        traffic_monitor=DemoTraffic(),  # type: ignore[arg-type]
    )
    return server


# 中文注释：演示数据按 UTC+8 的工作时段生成；后端与浏览器都固定在这个时区，
# 任何时候生成截图，「今天」都已有数据。
DEMO_TIMEZONE = timezone(timedelta(hours=8))
DEMO_TIMEZONE_ID = "Asia/Shanghai"

SHOTS = (
    # 名称, 区块 id（None = 首屏）
    ("overview", None),
    ("accounts", "accounts"),
    ("usage", "usage"),
    ("insights", "insights"),
)
# 中文注释：用量与习惯分析按需加载，截图前先点开。
LOAD_BUTTONS = ("#usage-load-button", "#insights-load-button")


def _browser_env(scratch: Path) -> dict[str, str] | None:
    """系统里没有中文字体时（常见于 WSL），借用 Windows 字体目录渲染中文。"""

    import os
    import shutil
    import subprocess

    if shutil.which("fc-list") is None:
        return None
    found = subprocess.run(["fc-list", ":lang=zh"], capture_output=True, text=True).stdout
    windows_fonts = Path("/mnt/c/Windows/Fonts")
    if found.strip() or not windows_fonts.is_dir():
        return None
    config = scratch / "fonts.conf"
    config.write_text(
        '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>'
        "<include ignore_missing=\"yes\">/etc/fonts/fonts.conf</include>"
        f"<dir>{windows_fonts}</dir>"
        f"<cachedir>{scratch / 'fontcache'}</cachedir>"
        "</fontconfig>",
        encoding="utf-8",
    )
    return {**os.environ, "FONTCONFIG_FILE": str(config)}


def _shrink(path: Path) -> None:
    """量化成 256 色调色板 PNG（深色界面多为纯色块，肉眼无差别，体积约降到 1/3）。"""

    try:
        from PIL import Image
    except ImportError:  # 没装 Pillow 就保留原图
        return
    with Image.open(path) as image:
        palette = image.convert("RGB").quantize(
            colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE
        )
    palette.save(path, optimize=True)


def capture(base_url: str, scratch: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(env=_browser_env(scratch))
        for lang in ("en", "zh"):
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                device_scale_factor=2,
                timezone_id=DEMO_TIMEZONE_ID,
                locale="en-US" if lang == "en" else "zh-CN",
            )
            page = context.new_page()
            page.add_init_script("localStorage.setItem('a-token-monitor-theme', 'dark')")
            page.goto(f"{base_url}/?lang={lang}")
            page.wait_for_timeout(3_000)
            # 中文注释：用 JS 直接触发按需加载，不会像真实点击那样把页面滚到按钮处；
            # 首屏的「今日金额」依赖用量加载，所以加载完再截首屏。
            for selector in LOAD_BUTTONS:
                page.evaluate(f"document.querySelector('{selector}').click()")
            page.wait_for_timeout(4_000)
            page.screenshot(path=str(OUTPUT / f"overview-{lang}.png"))
            _shrink(OUTPUT / f"overview-{lang}.png")
            print(f"wrote docs/screenshots/overview-{lang}.png")
            for name, section in SHOTS:
                target = OUTPUT / f"{name}-{lang}.png"
                if section is None:
                    continue
                else:
                    locator = page.locator(f"#{section}")
                    locator.scroll_into_view_if_needed()
                    page.wait_for_timeout(800)
                    locator.screenshot(path=str(target))
                _shrink(target)
                print(f"wrote {target.relative_to(ROOT)}")
            context.close()
        browser.close()


def main() -> None:
    set_local_timezone(DEMO_TIMEZONE)
    with tempfile.TemporaryDirectory(prefix="a-token-monitor-demo-") as temporary:
        root = Path(temporary)
        with mock.patch.dict(PROVIDER_SPECS, demo_providers({n: root / f".{n}" for n in ("claude", "grok", "kimi")})):
            server = build(root)
            server.start()
            try:
                host, port = server.address
                capture(f"http://{host}:{port}", root)
            finally:
                server.close()


if __name__ == "__main__":
    main()
