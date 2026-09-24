"""本地 Dashboard：展示额度、活动会话、本地用量和历史告警。"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import struct
import time
import zlib
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from .accounts import read_codex_plan_type
from .alerts import (
    MAX_QUERY_LIMIT,
    AlertQuery,
    AlertStoreError,
    TrafficAlertStore,
)
from .i18n import localize_payload, resolve_language, substitute
from .housekeeping import (
    CleanupCriteria,
    HousekeepingError,
    HousekeepingMonitor,
    empty_housekeeping_report,
)
from .claude import (
    list_claude_active_sessions,
    read_claude_account,
    read_claude_quota,
    resolve_claude_homes,
)
from .commandcode import (
    list_commandcode_active_sessions,
    read_commandcode_account,
    read_commandcode_quota,
    resolve_commandcode_homes,
)
from .dsh import (
    list_dsh_active_sessions,
    read_dsh_account,
    read_dsh_quota,
    resolve_dsh_homes,
)
from .grok import (
    list_grok_active_sessions,
    read_grok_account,
    read_grok_quota,
    resolve_grok_homes,
)
from .health import HealthTracker, sanitize_error
from .kimi import (
    list_kimi_active_sessions,
    read_kimi_account,
    read_kimi_quota,
    resolve_kimi_homes,
)
from .multi_models import TrackedSession, session_view
from .quota import (
    QuotaSnapshot,
    quota_period,
    quota_period_label,
    quota_window_duration,
)
from .registry import MultiSessionRegistry, RegistryError
from .retention import HistoryDataManager, RetentionController, RetentionError
from .scan_dirs import PROVIDER_SPECS, ScanDirsController, ScanDirsError
from .traffic import TrafficMonitor, TrafficSnapshot, empty_traffic_snapshot
from .usage import (
    DEFAULT_SEARCH_DAYS,
    SessionSwitchThresholds,
    UsageAggregator,
    enrich_session_views,
    calendar_day_start,
    search_since_days,
)


# 中文注释：告警历史的写接口只接受小请求体，避免 Dashboard 被当成通用上传入口。
_MAX_REQUEST_BYTES = 64 * 1024
# 中文注释：结束不超过该时间的会话仍显示在会话表里，方便单独归档。
_RECENT_FINISHED_SECONDS = 24 * 3600.0

# 中文注释：主页与设置页共用的基础样式。
_BASE_CSS = r"""
    :root {
      color-scheme: dark;
      --bg: #0b0f14;
      --sidebar: #0d131a;
      --panel: #111820;
      --panel-soft: #192532;
      --surface-raised: #151f2a;
      --surface-hover: #1d2c3a;
      --text: #edf3f8;
      --muted: #8fa0b4;
      --muted-strong: #b8c5d3;
      --line: #263849;
      --line-soft: #1c2a37;
      --green: #34d399;
      --green-soft: #12372d;
      --yellow: #f59e0b;
      --yellow-soft: #3b2b11;
      --red: #fb7185;
      --red-soft: #3d1d29;
      --blue: #60a5fa;
      --blue-soft: #152f4d;
      --violet: #8b5cf6;
      --violet-soft: #241c47;
      --cyan: #22d3ee;
      --cyan-soft: #123440;
      --shadow: 0 18px 42px rgba(0, 0, 0, .18);
      --green-soft-strong: #163725;
      --green-border: #1f6f52;
      --green-text: #6ee7b7;
      --green-glow: rgba(52, 211, 153, .1);
      --yellow-soft-strong: #3b3218;
      --yellow-border: #7a5410;
      --yellow-text: #fcd34d;
      --red-soft-strong: #3a2028;
      --red-border: #6c2e43;
      --red-text: #fda4af;
      --red-glow: rgba(251, 113, 133, .1);
      --blue-soft-strong: #1b304c;
      --blue-border: #2c4a78;
      --blue-text: #93c5fd;
      --violet-border: #3d3170;
      --violet-border-accent: #a78bfa;
      --violet-border-strong: #5541a0;
      --violet-border-active: #3b2b69;
      --violet-border-selected: #654bc0;
      --violet-text: #b9a4ff;
      --violet-text-accent: #c4b5fd;
      --violet-text-soft: #eee9ff;
      --violet-text-strong: #f4f0ff;
      --violet-glow: rgba(139, 92, 246, .24);
      --cyan-border: #345064;
      --cyan-border-strong: #2a6f86;
      --cyan-hover: #16404f;
      --cyan-text: #a5f3fc;
      --track: #202e3d;
      --track-strong: #2f4a63;
      --overlay-hover: rgba(255, 255, 255, .018);
      --overlay-chip: rgba(255, 255, 255, .045);
      --muted-dim: #64778b;
      --muted-faint: #66798d;
      --muted-separator: #425366;
      --muted-th: #71859a;
      --placeholder-text: #6c8098;
      --border-strong: #3d5670;
      --focus-ring: rgba(34, 211, 238, .35);
    }

    /* 中文注释：白天模式复用同一组语义变量，只覆盖取值；JS 在 <html> 上切换
       data-theme，保证所有组件（含 SVG 图表、健康徽标、告警等级）一起变色。 */
    html[data-theme="light"] {
      color-scheme: light;
      --bg: #f5f7fb;
      --sidebar: #ffffff;
      --panel: #ffffff;
      --panel-soft: #f2f5f9;
      --surface-raised: #ffffff;
      --surface-hover: #eef2f8;
      --text: #17212b;
      --muted: #5d6b7a;
      --muted-strong: #37424e;
      --line: #d9e1ea;
      --line-soft: #e8edf3;
      --green: #0f9d63;
      --green-soft: #e4f6ed;
      --green-soft-strong: #d2efdf;
      --green-border: #a3dcc0;
      --green-text: #0a7a4b;
      --green-glow: rgba(15, 157, 99, .14);
      --yellow: #b45309;
      --yellow-soft: #fdf2dd;
      --yellow-soft-strong: #fbe6c4;
      --yellow-border: #f0c583;
      --yellow-text: #8a4b06;
      --red: #d6455d;
      --red-soft: #fdeaee;
      --red-soft-strong: #fbdae1;
      --red-border: #f0b4c0;
      --red-text: #a52a44;
      --red-glow: rgba(214, 69, 93, .14);
      --blue: #2563eb;
      --blue-soft: #e8efff;
      --blue-soft-strong: #d8e6ff;
      --blue-border: #aac8f6;
      --blue-text: #1d4ed8;
      --violet: #6d4aff;
      --violet-soft: #efeaff;
      --violet-border: #cec2f8;
      --violet-border-accent: #8b6bff;
      --violet-border-strong: #b7a6f3;
      --violet-border-active: #ddd4fc;
      --violet-border-selected: #b9a8f5;
      --violet-text: #5a35d6;
      --violet-text-accent: #6d4aff;
      --violet-text-soft: #4630a8;
      --violet-text-strong: #35247f;
      --violet-glow: rgba(109, 74, 255, .18);
      --cyan: #0e7490;
      --cyan-soft: #e3f4f9;
      --cyan-border: #a9d8e3;
      --cyan-border-strong: #7cc3d4;
      --cyan-hover: #d5eef5;
      --cyan-text: #0b5f75;
      --track: #e5eaf1;
      --track-strong: #ccd7e3;
      --overlay-hover: rgba(16, 32, 48, .035);
      --overlay-chip: rgba(16, 32, 48, .05);
      --muted-dim: #7c8a99;
      --muted-faint: #8694a3;
      --muted-separator: #b7c1cd;
      --muted-th: #7c8a99;
      --placeholder-text: #9aa6b4;
      --border-strong: #b9c6d4;
      --focus-ring: rgba(14, 116, 144, .28);
      --shadow: 0 12px 28px rgba(15, 32, 56, .10);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 24px;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, sans-serif;
    }
    .app-shell {
      display: grid;
      grid-template-columns: 220px minmax(0, 1fr);
      gap: 24px;
      max-width: 1680px;
      margin: 0 auto;
    }
    main { min-width: 0; }
    .sidebar {
      position: sticky;
      top: 24px;
      align-self: start;
      min-height: calc(100vh - 48px);
      padding: 18px 14px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--panel);
    }
    .sidebar-brand { padding: 2px 10px 18px; font-weight: 700; }
    .sidebar-brand small { display: block; margin-top: 3px; color: var(--muted); font-weight: 400; }
    .sidebar nav { display: grid; gap: 6px; }
    .sidebar-link {
      display: block;
      padding: 9px 10px;
      border: 1px solid transparent;
      border-radius: 8px;
      color: var(--muted);
      text-decoration: none;
    }
    .sidebar-link:hover, .sidebar-link.active { border-color: var(--line); color: var(--text); background: var(--panel-soft); }
    .sidebar-foot { margin: 24px 10px 0; color: var(--muted); font-size: 12px; }
    header { display: flex; justify-content: space-between; gap: 16px; align-items: end; }
    h1 { margin: 0; font-size: 26px; letter-spacing: .01em; }
    h2 { margin: 0 0 14px; font-size: 17px; }
    .muted { color: var(--muted); }
    .error {
      display: none;
      margin: 18px 0;
      padding: 12px 14px;
      border: 1px solid var(--red);
      border-radius: 10px;
      color: var(--red);
      background: var(--red-soft-strong);
    }
    .cards {
      display: grid;
      grid-template-columns: repeat(4, minmax(150px, 1fr));
      gap: 12px;
      margin: 24px 0;
    }
    .card, .panel {
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--panel);
    }
    .card { padding: 16px; }
    .card-label { color: var(--muted); font-size: 12px; }
    .card-value { margin-top: 5px; font-size: 24px; font-weight: 700; }
    .quota-rows { display: grid; gap: 8px; }
    .account-list { display: grid; gap: 16px; }
    .account-block {
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--panel-soft);
    }
    .account-heading {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 16px;
    }
    .account-title { margin: 0; font-size: 18px; }
    .account-subtitle {
      margin: 18px 0 10px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: .04em;
    }
    .account-block .account-subtitle:first-of-type { margin-top: 0; }
    .account-block .table-wrap { margin: 0 -8px -8px; }
    .quota-row { display: grid; align-items: center; column-gap: 10px; }
    .quota-row-label { display: flex; align-items: center; gap: 6px; }
    .bar { height: 8px; margin: 12px 0; border-radius: 99px; background: var(--panel-soft); overflow: hidden; }
    .bar > span { display: block; height: 100%; border-radius: inherit; background: var(--green); }
    .bar > span.warn { background: var(--yellow); }
    .bar > span.danger { background: var(--red); }
    .panel { margin-top: 24px; padding: 18px; overflow: hidden; }
    .panel-heading { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
    .panel-heading { display: flex; align-items: end; justify-content: space-between; gap: 18px; margin-bottom: 19px; }
    .section-description { margin: 5px 0 0; color: var(--muted); font-size: 12px; }
    .section-meta { display: flex; align-items: center; gap: 12px; color: var(--muted); font-size: 12px; }
    .section-count { padding: 4px 8px; border: 1px solid var(--line); border-radius: 99px; color: var(--muted-strong); white-space: nowrap; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 900px; }
    th, td { padding: 10px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: 12px; font-weight: 600; white-space: nowrap; }
    td { font-size: 13px; }
    .session-id { color: var(--blue); font-family: ui-monospace, SFMono-Regular, monospace; }
    .cwd, .event, .error-text { max-width: 340px; overflow-wrap: anywhere; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 99px; font-size: 12px; white-space: nowrap; }
    .pill.running { color: var(--green); background: var(--green-soft-strong); }
    .pill.limit_blocked { color: var(--yellow); background: var(--yellow-soft-strong); }
    .pill.waiting_for_approval { color: var(--blue); background: var(--blue-soft-strong); }
    .pill.failed, .pill.orphaned { color: var(--red); background: var(--red-soft-strong); }
    .pill.other { color: var(--muted); background: var(--panel-soft); }
    .usage-tabs { display: flex; flex-wrap: wrap; gap: 8px; margin: 4px 0 12px; }
    .usage-tab {
      padding: 7px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--muted);
      background: var(--panel-soft);
      cursor: pointer;
    }
    .usage-tab.selected { border-color: var(--blue); color: var(--text); background: var(--blue-soft-strong); }
    .usage-filters { display: flex; flex-wrap: wrap; gap: 10px; margin: 4px 0 12px; }
    .usage-filter { display: grid; gap: 4px; color: var(--muted); font-size: 12px; }
    .usage-filter select {
      min-width: 220px;
      padding: 7px 9px;
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--text);
      background: var(--panel-soft);
    }
    .usage-note { margin-bottom: 12px; color: var(--muted); font-size: 12px; }
    .usage-table table { min-width: 1180px; }
    .usage-models { display: grid; gap: 3px; min-width: 180px; }
    .usage-models-toggle { justify-self: start; padding: 1px 7px; font-size: 11px; }
    .usage-model { color: var(--blue); font-family: ui-monospace, SFMono-Regular, monospace; }
    .usage-number { white-space: nowrap; }

    /* Stitch 运维控制台视觉：用深色 graphite、violet 主色和 cyan 状态色重排信息层级。 */

    html { scroll-behavior: smooth; }
    body {
      padding: 0;
      background: var(--bg);
      font: 14px/1.55 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      letter-spacing: -.01em;
    }
    button, select { font: inherit; }
    button { color: inherit; }
    .app-shell {
      display: grid;
      grid-template-columns: 232px minmax(0, 1fr);
      gap: 0;
      max-width: none;
      min-height: 100vh;
      margin: 0;
    }
    main {
      width: 100%;
      max-width: 1500px;
      padding: 0 40px 64px;
    }
    .sidebar {
      position: sticky;
      top: 0;
      display: flex;
      flex-direction: column;
      width: 232px;
      height: 100vh;
      min-height: 0;
      padding: 22px 14px 18px;
      border: 0;
      border-right: 1px solid var(--line-soft);
      border-radius: 0;
      background: var(--sidebar);
    }
    .sidebar-brand {
      display: flex;
      align-items: center;
      gap: 11px;
      padding: 2px 10px 28px;
    }
    /* 中文注释：品牌图形与浏览器标签页图标是同一份几何（含渐变徽章），
       所以容器只负责尺寸，不再叠一层紫色底板。 */
    .brand-mark { display: grid; width: 36px; height: 36px; flex: 0 0 auto; place-items: center; }
    .brand-mark svg { display: block; width: 36px; height: 36px; border-radius: 8px; box-shadow: 0 8px 20px var(--violet-glow); }
    .brand-name { font-size: 15px; font-weight: 750; letter-spacing: -.02em; }
    .brand-name span { color: var(--violet-text); }
    .sidebar-brand small { display: block; margin-top: 2px; color: var(--muted); font-size: 11px; font-weight: 400; }
    .sidebar-label { padding: 0 11px 9px; color: var(--muted-dim); font-size: 10px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
    .sidebar nav { display: grid; gap: 5px; }
    .sidebar-link {
      display: flex;
      align-items: center;
      gap: 11px;
      min-height: 42px;
      padding: 9px 11px;
      border: 1px solid transparent;
      border-radius: 9px;
      color: var(--muted);
      text-decoration: none;
      transition: border-color .15s ease, color .15s ease, background .15s ease;
    }
    .sidebar-link svg { width: 17px; height: 17px; flex: 0 0 auto; opacity: .82; }
    .sidebar-link:hover { border-color: var(--line); color: var(--muted-strong); background: var(--panel); }
    .sidebar-link.active { border-color: var(--violet-border-active); color: var(--violet-text-strong); background: var(--violet-soft); box-shadow: inset 2px 0 0 var(--violet); }
    .sidebar-link.active svg { color: var(--violet-text); opacity: 1; }
    .nav-count { min-width: 22px; margin-left: auto; padding: 1px 6px; border: 1px solid var(--line); border-radius: 99px; color: var(--muted-strong); font-size: 11px; text-align: center; }
    .sidebar-bottom { display: grid; gap: 8px; margin-top: auto; padding: 14px 11px 0; border-top: 1px solid var(--line-soft); }
    .service-state, .live-indicator { display: inline-flex; align-items: center; gap: 7px; color: var(--muted-strong); font-size: 12px; }
    .status-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--green); box-shadow: 0 0 0 4px var(--green-glow); }
    .status-dot.error { background: var(--red); box-shadow: 0 0 0 4px var(--red-glow); }
    .health-badge { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border: 1px solid var(--line); border-radius: 99px; background: none; color: var(--muted-strong); font-size: 11px; cursor: pointer; }
    .health-badge .health-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
    .health-badge.ok { border-color: var(--green-border); color: var(--green-text); background: var(--green-soft); }
    .health-badge.degraded { border-color: var(--yellow-border); color: var(--yellow-text); background: var(--yellow-soft); }
    .health-badge.failed { border-color: var(--red-border); color: var(--red-text); background: var(--red-soft); }
    .health-badge.starting { color: var(--muted); }
    .health-detail { margin: -14px 0 20px; padding: 12px 14px; border: 1px solid var(--line); border-radius: 10px; color: var(--muted-strong); font-size: 12px; }
    .health-detail ul { margin: 8px 0 0; padding-left: 18px; }
    .health-detail li { margin: 4px 0; }
    .sidebar-foot { margin: 0; color: var(--muted-faint); font-size: 11px; }
    .topbar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 16px; row-gap: 12px; min-height: 72px; margin-bottom: 34px; border-bottom: 1px solid var(--line-soft); }
    .breadcrumb { display: flex; align-items: center; gap: 9px; color: var(--muted); font-size: 12px; }
    .breadcrumb strong { color: var(--muted-strong); font-weight: 600; }
    .breadcrumb-separator { color: var(--muted-separator); }
    .topbar-actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 15px; row-gap: 10px; }
    .refresh-button { display: inline-flex; align-items: center; gap: 7px; padding: 7px 10px; border: 1px solid var(--line); border-radius: 7px; color: var(--muted-strong); background: var(--panel); cursor: pointer; transition: border-color .15s ease, color .15s ease, background .15s ease; }
    .refresh-button:hover { border-color: var(--violet); color: var(--text); background: var(--surface-raised); }
    .refresh-button:disabled { cursor: wait; opacity: .7; }
    .refresh-button svg { width: 15px; height: 15px; }
    .refresh-button.is-spinning svg { animation: spin .8s linear infinite; }
    .theme-toggle { display: inline-flex; align-items: center; gap: 7px; padding: 7px 10px; border: 1px solid var(--line); border-radius: 7px; color: var(--muted-strong); background: var(--panel); cursor: pointer; transition: border-color .15s ease, color .15s ease, background .15s ease; }
    .theme-toggle:hover { border-color: var(--violet); color: var(--text); background: var(--surface-raised); }
    .theme-toggle svg { width: 15px; height: 15px; display: none; }
    /* 中文注释：三种模式各用一个图标，用 html 上的 data-theme-mode 决定显示哪个。 */
    html[data-theme-mode="system"] .theme-icon-system, html[data-theme-mode="light"] .theme-icon-light, html[data-theme-mode="dark"] .theme-icon-dark { display: block; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .page-hero { display: flex; align-items: end; justify-content: space-between; gap: 24px; margin-bottom: 25px; }
    .eyebrow, .section-kicker { margin-bottom: 7px; color: var(--cyan); font-size: 10px; font-weight: 750; letter-spacing: .13em; text-transform: uppercase; }
    h1 { margin: 0; font-size: 32px; letter-spacing: -.04em; line-height: 1.15; }
    h2 { margin: 0; font-size: 18px; letter-spacing: -.025em; }
    h3 { margin: 0; }
    .hero-description { margin: 8px 0 0; color: var(--muted); }
    .hero-context { display: flex; align-items: center; gap: 10px; padding-bottom: 3px; }
    .muted { color: var(--muted); }
    .error { display: none; margin: 0 0 20px; padding: 12px 14px; border: 1px solid var(--red-border); border-radius: 9px; color: var(--red-text); background: var(--red-soft); }
    .card, .panel { border: 1px solid var(--line); border-radius: 12px; background: var(--panel); }
    .account-subtitle { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 18px 0 10px; color: var(--muted-strong); font-size: 12px; font-weight: 650; }
    .table-wrap { overflow-x: auto; border: 1px solid var(--line-soft); border-radius: 9px; background: var(--panel); }
    table { width: 100%; min-width: 900px; border-collapse: collapse; }
    th, td { padding: 11px 12px; border-bottom: 1px solid var(--line-soft); text-align: left; vertical-align: top; }
    tbody tr:last-child td { border-bottom: 0; }
    tbody tr:hover { background: var(--overlay-hover); }
    th { color: var(--muted-th); font-size: 10px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; white-space: nowrap; }
    td { color: var(--muted-strong); font-size: 12px; }
    .pill { display: inline-block; padding: 3px 8px; border-radius: 99px; font-size: 11px; white-space: nowrap; }
    .pill.running { color: var(--green); background: var(--green-soft); }
    .pill.limit_blocked { color: var(--yellow); background: var(--yellow-soft); }
    .pill.waiting_for_approval { color: var(--blue); background: var(--blue-soft); }
    .pill.failed, .pill.orphaned, .pill.danger { color: var(--red); background: var(--red-soft); }
    .pill.warn { color: var(--yellow); background: var(--yellow-soft); }
    .pill.ok { color: var(--green); background: var(--green-soft); }
    .pill.other { color: var(--muted); background: var(--panel-soft); }
    .usage-filter select:focus, .refresh-button:focus-visible, .usage-tab:focus-visible, .sidebar-link:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
    .usage-note { margin: 0 0 13px; color: var(--muted); font-size: 11px; }
    .empty-state { padding: 26px 12px; color: var(--muted); text-align: center; }
    .empty-state .empty-title { display: block; color: var(--muted-strong); font-weight: 650; }
    .empty-state .empty-hint { display: block; margin-top: 5px; font-size: 11px; }
    /* 工具栏与表单控件：统一深色输入框，替换浏览器默认外观。 */
    .toolbar { display: flex; flex-wrap: wrap; align-items: end; gap: 10px; padding: 12px 13px; margin: 0 0 14px; border: 1px solid var(--line-soft); border-radius: 10px; background: var(--surface-raised); }
    .field { display: grid; gap: 5px; min-width: 0; }
    .field-label { color: var(--muted); font-size: 10px; font-weight: 650; letter-spacing: .07em; text-transform: uppercase; }
    .field select, .field input { min-width: 128px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 7px; color: var(--text); background: var(--panel); font: inherit; font-size: 12px; }
    .field select { padding-right: 28px; cursor: pointer; }
    .field input::placeholder { color: var(--placeholder-text); }
    .field.wide select, .field.wide input { min-width: 208px; }
    .field input[type="date"] { color-scheme: dark; }
    .field select:focus, .field input:focus { border-color: var(--cyan); outline: 2px solid var(--focus-ring); outline-offset: 1px; }
    .toolbar-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-left: auto; }
    .toolbar-note { flex-basis: 100%; color: var(--muted); font-size: 11px; }
    /* 按钮：一个基础样式加少量语义变体，替换此前到处复用的 .refresh-button。 */
    .btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 12px; border: 1px solid var(--line); border-radius: 7px; color: var(--muted-strong); background: var(--panel); cursor: pointer; font: inherit; font-size: 12px; white-space: nowrap; transition: border-color .15s ease, background .15s ease, color .15s ease; }
    .btn:hover { border-color: var(--border-strong); color: var(--text); background: var(--surface-hover); }
    .btn.primary { border-color: var(--cyan-border-strong); color: var(--cyan-text); background: var(--cyan-soft); }
    .btn.primary:hover { border-color: var(--cyan); background: var(--cyan-hover); }
    .btn.warn { border-color: var(--yellow-border); color: var(--yellow-text); background: var(--yellow-soft); }
    .btn.warn:hover { border-color: var(--yellow); }
    .btn.danger { border-color: var(--red-border); color: var(--red-text); background: var(--red-soft); }
    .btn.danger:hover { border-color: var(--red); }
    .btn.mini { padding: 6px 10px; font-size: 11px; }
    .btn[disabled] { opacity: .55; cursor: not-allowed; }
    .btn:focus-visible, .chip-button:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
    /* 表格：紧凑两行单元格 + 数字对齐 + 截断长路径。 */
    table.tight { min-width: 0; }
    table.tight th, table.tight td { padding: 10px 12px; }
    table.tight td { font-size: 12px; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
    .cell-main { color: var(--muted-strong); font-size: 12px; }
    .cell-sub { margin-top: 3px; color: var(--muted); font-size: 11px; }
    .chip { display: inline-flex; align-items: center; gap: 5px; padding: 3px 8px; border: 1px solid var(--line); border-radius: 99px; color: var(--muted-strong); background: var(--panel); font-size: 11px; white-space: nowrap; }
    .chip.warn { border-color: var(--yellow-border); color: var(--yellow-text); background: var(--yellow-soft); }
    .chip.danger { border-color: var(--red-border); color: var(--red-text); background: var(--red-soft); }
    .chip.ok { border-color: var(--green-border); color: var(--green-text); background: var(--green-soft); }
    .chip-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
    .chip-button { padding: 0; border: 0; color: var(--cyan); background: none; cursor: pointer; font: inherit; font-size: 11px; text-align: left; }
    .chip-button:hover { text-decoration: underline; }
    /* 目录与归档卡片。 */
    .card-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; }
    .mini-card { padding: 15px 16px; border: 1px solid var(--line-soft); border-radius: 10px; background: var(--surface-raised); }
    .mini-card-head { display: flex; align-items: start; justify-content: space-between; gap: 12px; }
    .mini-card-title { color: var(--text); font-size: 13px; font-weight: 700; }
    .mini-card-path { margin-top: 3px; color: var(--muted); font-size: 11px; overflow-wrap: anywhere; }
    .criteria-row { display: flex; flex-wrap: wrap; align-items: end; gap: 10px; margin-top: 12px; }
"""


# 中文注释：主页独有的样式（KPI、账号卡片、用量、告警、习惯分析、磁盘管理等）。
_DASHBOARD_CSS = r"""
    #overview, #accounts, #usage, #insights, #traffic { scroll-margin-top: 88px; }
    .scope-badge { display: inline-flex; align-items: center; gap: 8px; padding: 7px 10px; border: 1px solid var(--line); border-radius: 7px; color: var(--muted-strong); background: var(--panel); font-size: 12px; }
    .scope-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--cyan); }
    .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 13px; margin-bottom: 34px; }
    .kpi-card { position: relative; min-height: 142px; padding: 17px 17px 14px; overflow: hidden; box-shadow: var(--shadow); border: 1px solid var(--line); border-radius: 12px; background: var(--panel); }
    .kpi-card::after { position: absolute; top: 0; right: 20px; left: 20px; height: 2px; background: var(--accent); content: ''; opacity: .82; }
    .kpi-card.accent-violet { --accent: var(--violet); }
    .kpi-card.accent-yellow { --accent: var(--yellow); }
    .kpi-card.accent-cyan { --accent: var(--cyan); }
    .kpi-card.accent-green { --accent: var(--green); }
    .kpi-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .kpi-label { color: var(--muted); font-size: 12px; }
    .kpi-icon { display: grid; width: 28px; height: 28px; place-items: center; border-radius: 7px; color: var(--accent); background: var(--overlay-chip); }
    .kpi-icon svg { width: 16px; height: 16px; }
    .kpi-value { margin-top: 8px; font-size: 28px; font-weight: 750; letter-spacing: -.045em; }
    .kpi-foot { display: flex; align-items: center; gap: 5px; margin-top: 9px; color: var(--muted); font-size: 11px; }
    .kpi-foot strong { color: var(--muted-strong); font-weight: 600; }
    .section-block { margin-top: 24px; padding: 21px; overflow: hidden; }
    .section-toggle { display: flex; align-items: flex-start; gap: 11px; width: 100%; padding: 0; border: 0; color: inherit; background: none; cursor: pointer; font: inherit; text-align: left; }
    .section-toggle-text { display: block; min-width: 0; }
    .section-toggle-title { display: block; margin: 0; font-size: 17px; font-weight: 700; letter-spacing: -.01em; }
    .section-toggle:hover .section-toggle-title { color: var(--cyan); }
    .section-toggle:focus-visible { outline: 2px solid var(--cyan); outline-offset: 3px; border-radius: 6px; }
    .section-toggle .chevron { width: 15px; height: 15px; margin-top: 5px; flex: 0 0 auto; color: var(--muted); transition: transform .16s ease; transform: rotate(90deg); }
    .section-block.is-collapsed .section-toggle .chevron { transform: rotate(0deg); }
    .section-toggle .section-kicker { display: block; }
    .section-toggle .section-description { display: block; }
    .section-block.is-collapsed .section-toggle .section-description { display: none; }
    .section-summary { display: none; margin-top: 6px; color: var(--muted-strong); font-size: 12px; overflow-wrap: anywhere; }
    .section-block.is-collapsed .section-summary { display: block; }
    .section-block.is-collapsed { padding-bottom: 18px; }
    .section-block.is-collapsed .section-body { display: none; }
    .section-body { margin-top: 2px; }
    /* 中文注释：同一行的卡片等高（默认 stretch），否则高度不一，页脚和行位置都会错开。 */
    .account-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 15px; }
    .account-block { display: flex; flex-direction: column; padding: 20px 21px 21px; border: 1px solid var(--line); border-radius: 11px; background: var(--surface-raised); }
    /* 中文注释：紧跟在额度行之后的那段小标题（活动会话）吃掉剩余空间，
       卡片等高时页脚自然贴底对齐；注意不能用 :nth-of-type，它按 div 序号算。 */
    .account-block .quota-rows-block + .account-subtitle { margin-top: auto; }
    .account-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; padding-bottom: 17px; border-bottom: 1px solid var(--line-soft); }
    .account-identity { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .account-avatar { display: grid; width: 38px; height: 38px; flex: 0 0 auto; place-items: center; border: 1px solid var(--violet-border); border-radius: 10px; color: var(--violet-text-accent); background: var(--violet-soft); font-size: 13px; font-weight: 750; }
    .account-label { color: var(--cyan); font-size: 10px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; }
    .account-title { margin: 2px 0 3px; font-size: 17px; letter-spacing: -.02em; overflow-wrap: anywhere; }
    /* 中文注释：账号 ID 长短会让 meta 折成 1 行或 2 行，这里固定成两行高度，
       heading 高度才不会跟着变、三行额度窗口才能跨卡片对齐。 */
    .account-meta { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; min-height: 3.1em; color: var(--muted); font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
    .account-side { display: grid; justify-items: end; gap: 7px; text-align: right; }
    .plan-badge { padding: 4px 8px; border: 1px solid var(--cyan-border); border-radius: 6px; color: var(--cyan); background: var(--cyan-soft); font-size: 11px; }
    .account-activity { color: var(--muted); font-size: 11px; }
    .account-block .account-subtitle:first-of-type { margin-top: 17px; }
    /* 中文注释：额度行固定四列（周期 / 进度条 / 百分比 / 重置与时长）、固定行高，
       缺的周期也占位，所以不同订阅的卡片同构、同名行对齐。 */
    .quota-row {
      /* 两行布局：第一行「周期 / 进度条 / 百分比」，第二行把重置时间放在进度条下方。
         卡片并排时宽度会变，固定四列会把右侧文案挤出卡片，所以让重置换行。 */
      grid-template-columns: 92px minmax(60px, 1fr) 76px;
      grid-template-areas: "label bar percent" "label meta meta";
      row-gap: 3px;
      min-height: 52px;
      padding: 8px 11px;
      border: 1px solid var(--line-soft);
      border-radius: 8px;
      background: var(--surface-raised);
    }
    .quota-row-label { grid-area: label; color: var(--muted-strong); font-size: 12px; font-weight: 650; }
    .quota-bar { grid-area: bar; }
    .quota-row-percent { grid-area: percent; color: var(--text); font-size: 15px; font-weight: 700; letter-spacing: -.02em; white-space: nowrap; }
    .quota-row-percent.warn { color: var(--yellow-text); }
    .quota-row-percent.danger { color: var(--red-text); }
    .quota-row-percent.absent { color: var(--muted-dim); font-size: 12px; font-weight: 600; }
    .quota-row-meta { grid-area: meta; color: var(--muted); font-size: 11px; overflow-wrap: anywhere; }
    .quota-row.absent { border-style: dashed; background: transparent; }
    .quota-row.absent .quota-bar > span { width: 0 !important; }
    .quota-bar { margin: 0; }
    .quota-badge { padding: 1px 6px; border: 1px solid var(--line); border-radius: 99px; color: var(--muted); font-size: 10px; font-weight: 600; white-space: nowrap; }
    .quota-note { padding: 12px 13px; border: 1px dashed var(--line); border-radius: 8px; color: var(--muted); font-size: 12px; }
    .bar { height: 6px; margin: 13px 0 12px; border-radius: 99px; background: var(--track); overflow: hidden; }
    .bar > span { display: block; height: 100%; border-radius: inherit; background: var(--green); }
    .bar > span.warn { background: var(--yellow); }
    .bar > span.danger { background: var(--red); }
    .session-table table { min-width: 1020px; }
    .session-id { color: var(--cyan); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; overflow-wrap: anywhere; }
    .cwd, .event, .error-text { max-width: 340px; overflow-wrap: anywhere; }
    td .muted { margin-top: 2px; font-size: 11px; }
    .traffic-table table { min-width: 1080px; }
    .traffic-remote { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; overflow-wrap: anywhere; }
    .usage-tabs { display: flex; flex-wrap: wrap; gap: 7px; margin: 0 0 14px; }
    .usage-tab { padding: 7px 12px; border: 1px solid var(--line); border-radius: 7px; color: var(--muted); background: var(--surface-raised); cursor: pointer; }
    .usage-tab:hover { border-color: var(--violet-border-strong); color: var(--muted-strong); }
    .usage-tab.selected { border-color: var(--violet-border-selected); color: var(--violet-text-soft); background: var(--violet-soft); }
    .usage-tabs-divider { width: 1px; align-self: stretch; min-height: 26px; margin: 0 3px; background: var(--line); }
    .session-toggle { margin: 10px 0 0; padding: 6px 10px; border: 1px solid var(--line); border-radius: 7px; color: var(--muted-strong); background: var(--surface-raised); cursor: pointer; font-size: 12px; }
    .session-toggle:hover { border-color: var(--violet-border-strong); background: var(--surface-hover); }
    /* 中文注释：三个筛选字段等宽排列（auto-fit 会退化成 2+1 的参差换行），
       窄屏由响应式规则切成一列；汇总卡按 170px 自适应列数，尽量一行放完。 */
    .usage-filters { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; margin: 0 0 14px; padding: 12px; border: 1px solid var(--line-soft); border-radius: 9px; background: var(--surface-raised); }
    .usage-filter { display: grid; gap: 5px; min-width: 0; color: var(--muted); font-size: 11px; }
    .usage-filter select { width: 100%; min-width: 0; padding: 8px 30px 8px 10px; border: 1px solid var(--line); border-radius: 7px; color: var(--text); background: var(--panel); cursor: pointer; }
    .usage-summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 14px; }
    .usage-summary-item { padding: 12px 13px; border: 1px solid var(--line-soft); border-radius: 8px; background: var(--surface-raised); }
    .usage-summary-label { color: var(--muted); font-size: 11px; }
    .usage-summary-value { margin-top: 3px; color: var(--text); font-size: 17px; font-weight: 700; }
    .usage-table table { min-width: 1180px; }
    .usage-models { display: grid; gap: 3px; min-width: 180px; }
    .usage-model { color: var(--cyan); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
    .usage-number { white-space: nowrap; }
    #alert-list { display: grid; gap: 8px; margin-bottom: 20px; }
    #alert-list:empty { display: none; }
    #session-notice { display: grid; gap: 8px; margin-bottom: 20px; }
    #session-notice:empty { display: none; }
    .alert-head { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 10px; font-weight: 700; letter-spacing: .09em; text-transform: uppercase; }
    .alert-head-spacer { flex: 1 1 auto; }
    .alert-row { display: grid; grid-template-columns: 3px minmax(0, 1fr) auto; align-items: center; gap: 13px; padding: 10px 13px; border: 1px solid var(--line); border-radius: 9px; background: var(--surface-raised); }
    .alert-row .alert-accent { width: 3px; align-self: stretch; border-radius: 99px; background: var(--blue); }
    .alert-row.warn .alert-accent { background: var(--yellow); }
    .alert-row.danger .alert-accent { background: var(--red); }
    .alert-row .alert-body { min-width: 0; }
    .alert-row .alert-title { color: var(--text); font-size: 12.5px; font-weight: 650; overflow-wrap: anywhere; }
    .alert-row .alert-detail { margin-top: 3px; color: var(--muted); font-size: 11px; overflow-wrap: anywhere; }
    .alert-row .alert-link { color: var(--cyan); font-size: 11px; text-decoration: none; white-space: nowrap; }
    .alert-row .alert-link:hover { text-decoration: underline; }
    .alert-list { display: grid; gap: 8px; }
    .alert-banner { padding: 11px 13px; border: 1px solid var(--line); border-radius: 9px; color: var(--muted-strong); background: var(--surface-raised); font-size: 12px; }
    .alert-banner.warn { border-color: var(--yellow-border); color: var(--yellow-text); background: var(--yellow-soft); }
    .alert-banner.danger { border-color: var(--red-border); color: var(--red-text); background: var(--red-soft); }
    .alert-banner.tip { border-color: var(--blue-border); color: var(--blue-text); background: var(--blue-soft); }
    .alert-banner.info { border-color: var(--line); color: var(--muted-strong); background: var(--surface-raised); }
    .alert-filters { display: flex; flex-wrap: wrap; align-items: end; gap: 10px; margin: 0 0 14px; padding: 12px; border: 1px solid var(--line-soft); border-radius: 9px; background: var(--surface-raised); }
    .alert-filter { display: grid; gap: 5px; color: var(--muted); font-size: 11px; }
    .alert-filter select, .alert-filter input { min-width: 150px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 7px; color: var(--text); background: var(--panel); }
    .alert-filter select { padding-right: 30px; cursor: pointer; }
    .alert-filter input:focus, .alert-filter select:focus { outline: 2px solid var(--cyan); outline-offset: 2px; }
    .alert-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-left: auto; }
    .alert-history-table table { min-width: 1120px; }
    .alert-row-unread { font-weight: 600; }
    .alert-ack-button { padding: 4px 8px; border: 1px solid var(--line); border-radius: 6px; color: var(--muted-strong); background: var(--panel); cursor: pointer; font-size: 11px; }
    .alert-ack-button:hover { border-color: var(--violet); color: var(--text); }
    .suggestion-saving { float: right; margin-left: 10px; color: var(--green); font-weight: 700; white-space: nowrap; }
    .insights-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; margin-bottom: 14px; }
    .observation-list { margin: 0; padding-left: 18px; color: var(--muted-strong); font-size: 12px; }
    .observation-list li { padding: 2px 0; }
    .usage-trend, .usage-top-projects { margin: 0 0 14px; padding: 13px; border: 1px solid var(--line-soft); border-radius: 9px; background: var(--surface-raised); }
    /* 中文注释：账号 / 项目成本排行并排两列；只有一个有数据时占满整行。 */
    .usage-rankings { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-bottom: 14px; }
    .usage-rankings.single { grid-template-columns: minmax(0, 1fr); }
    .usage-rankings .usage-top-projects { margin: 0; min-width: 0; }
    .usage-trend-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; color: var(--muted-strong); font-size: 12px; font-weight: 650; }
    .trend-chart { display: flex; align-items: flex-end; gap: 2px; height: 72px; }
    .trend-bar { flex: 1 1 0; min-width: 0; min-height: 2px; border-radius: 3px 3px 0 0; background: var(--violet); opacity: .75; }
    .trend-bar:hover { opacity: 1; }
    .trend-bar.today { background: var(--cyan); opacity: 1; }
    .trend-bar.empty { background: var(--line-soft); opacity: .6; }
    .budget-bar { margin: 8px 0 0; }
    .top-project { padding: 7px 0; border-top: 1px solid var(--line-soft); }
    .top-project:first-of-type { border-top: 0; }
    .top-project-row { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
    .top-project-label { color: var(--muted-strong); font-size: 12px; overflow-wrap: anywhere; }
    .top-project-value { color: var(--text); font-size: 12px; font-weight: 650; white-space: nowrap; }
    .top-project .bar { margin: 6px 0 0; }
    /* 统计小块：区块顶部的关键数字，避免把结论埋进表格。 */
    .stat-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(148px, 1fr)); gap: 10px; margin: 0 0 14px; }
    .stat { padding: 12px 14px; border: 1px solid var(--line-soft); border-radius: 10px; background: var(--surface-raised); }
    .stat-label { color: var(--muted); font-size: 11px; letter-spacing: .02em; }
    .stat-value { margin-top: 4px; font-size: 21px; font-weight: 750; letter-spacing: -.035em; }
    .stat-value.warn { color: var(--yellow); }
    .stat-value.danger { color: var(--red); }
    .stat-value.ok { color: var(--green); }
    .stat-foot { margin-top: 3px; color: var(--muted); font-size: 11px; }
    .table-actions { display: flex; justify-content: center; margin-top: 12px; }
    td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
    .truncate { display: block; max-width: 250px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .row-unread td:first-child { box-shadow: inset 2px 0 0 var(--cyan); }
    .row-unread .cell-main { color: var(--text); font-weight: 650; }
    .mini-card-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 13px; }
    .metric-label { color: var(--muted); font-size: 10px; letter-spacing: .06em; text-transform: uppercase; }
    .metric-value { margin-top: 2px; font-size: 14px; font-weight: 700; }
    .ratio { display: flex; height: 6px; margin-top: 11px; border-radius: 99px; background: var(--track); overflow: hidden; }
    .ratio span { display: block; height: 100%; }
    .ratio .ratio-sessions { background: var(--violet); }
    .ratio .ratio-other { background: var(--track-strong); }
    .action-result { margin: 0 0 12px; }
"""


# 中文注释：两页共用的响应式覆盖；必须放在各页独有样式之后，否则媒体查询里的
# 覆盖会被后面的同名非媒体规则压回去。
_RESPONSIVE_CSS = r"""
    @media (max-width: 1100px) {
      main { padding-right: 28px; padding-left: 28px; }
      .sidebar { width: 208px; }
      .app-shell { grid-template-columns: 208px minmax(0, 1fr); }
      .kpi-grid { grid-template-columns: repeat(2, minmax(150px, 1fr)); }
    }
    @media (max-width: 900px) {
      .usage-rankings { grid-template-columns: minmax(0, 1fr); }
      .app-shell { display: block; }
      .sidebar { position: static; width: auto; height: auto; padding: 14px 16px; border-right: 0; border-bottom: 1px solid var(--line-soft); }
      .sidebar-brand { padding: 0 4px 14px; }
      .sidebar-label { display: none; }
      .sidebar nav { display: flex; overflow-x: auto; }
      .sidebar-link { flex: 0 0 auto; min-height: 38px; white-space: nowrap; }
      .sidebar-bottom { display: none; }
      main { max-width: none; padding-top: 0; }
      .topbar { margin-bottom: 28px; }
    }
    @media (max-width: 760px) {
      /* 中文注释：窄屏两列；张数为奇数时让最后一张占满整行，
         避免出现一张孤零零的卡片挂在第二行。 */
      .usage-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .usage-summary-item:last-child:nth-child(odd) { grid-column: 1 / -1; }
    }
    @media (max-width: 700px) {
      .usage-filters { grid-template-columns: minmax(0, 1fr); }
    }
    @media (max-width: 640px) {
      main { padding: 0 16px 40px; }
      .topbar { min-height: 60px; }
      .topbar .live-indicator { display: none; }
      .page-hero { align-items: flex-start; flex-direction: column; gap: 14px; }
      h1 { font-size: 28px; }
      .hero-context { padding-bottom: 0; }
      .kpi-grid { gap: 9px; }
      .kpi-card { min-height: 128px; padding: 14px; }
      .kpi-value { font-size: 24px; }
      .section-block { padding: 16px; }
      .panel-heading { align-items: flex-start; flex-direction: column; gap: 9px; }
      .account-heading { flex-direction: column; }
      .account-side { justify-items: start; text-align: left; }
      .usage-summary,
      .usage-filters { grid-template-columns: minmax(0, 1fr); }
      .stat-row { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .toolbar { align-items: stretch; }
      .field, .field select, .field input, .field.wide select, .field.wide input { width: 100%; min-width: 0; }
      .toolbar-actions { margin-left: 0; }
      .mini-card-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .usage-filter, .usage-filter select { width: 100%; min-width: 0; }
    }
    @media (max-width: 430px) {
      .kpi-grid { grid-template-columns: 1fr; }
      .breadcrumb { font-size: 11px; }
      .refresh-button span { display: none; }
    }
"""


# 中文注释：设置页独有的样式；设置子块平级排列，之后可直接追加新的设置项。
_SETTINGS_CSS = r"""
    .settings-block { scroll-margin-top: 88px; }
"""


# 中文注释：浏览器标签页图标（favicon）与页面内品牌图形。图形由一组几何原语描述，
# 同一份原语既生成矢量 SVG（现代浏览器走 /favicon.svg），也用 zlib/struct 光栅化成
# PNG / ICO（老浏览器与 Safari 走 /favicon.ico），所以不需要图像库、不写外部文件、
# 也不发起站外请求。候选方案来自 Stitch MCP 的设计稿，换方案只改 _FAVICON_STYLE。
_FAVICON_SIZE = 64.0
_FAVICON_RADIUS = 14.0
# 中文注释：徽章渐变取页面自己的调色板——左上 violet（页面主色 #8b5cf6），
# 右下 cyan（状态色 #06b6d4），这样标签页图标、侧栏 logo 和页面是同一套颜色。
_FAVICON_BACKGROUND = ((0x8B, 0x5C, 0xF6), (0x06, 0xB6, 0xD4))
_FAVICON_FOREGROUND = (0xFF, 0xFF, 0xFF)
_FAVICON_ICO_SIZES = (16, 32)
_FAVICON_SVG_ROUTE = "/favicon.svg"
_FAVICON_ICO_ROUTE = "/favicon.ico"
_FAVICON_SVG_MIME = "image/svg+xml"
_FAVICON_ICO_MIME = "image/x-icon"
# 中文注释：图标是内容寻址的（URL 带几何指纹）且可以长期缓存。浏览器会把
# 「这个页面没有图标」也记进 favicon 数据库，改图标时指纹变化等于换 URL，
# 于是旧浏览器一定会重新拉取，不会一直顶着空白标签。
_FAVICON_CACHE_SECONDS = 604800
_FAVICON_ICO_CACHE: bytes | None = None

# 中文注释：几何原语。坐标都在 64×64 画布上，形状列表顺序即绘制顺序，
# mode="cut" 的原语从已画好的白色图形里挖洞（露出渐变底）。
#   ("rect", x, y, w, h, rx)                  填充圆角矩形
#   ("circle", cx, cy, r)                     填充圆
#   ("ring", cx, cy, r, width)                圆环
#   ("capsule", x1, y1, x2, y2, width)        圆头线段
#   ("arc", cx, cy, r, start, end, width)     圆弧（角度制，顺时针，0° 指右）
#   ("polyline", points, width)               折线（圆角连接）
#   ("polygon", points)                       填充多边形
_FAVICON_GLYPHS: dict[str, tuple[tuple, ...]] = {
    # A. 用量柱 + 高水位刻度孔（Stitch 方案 A）。
    "bars": (
        ("rect", 14.0, 34.0, 8.0, 18.0, 4.0),
        ("rect", 28.0, 24.0, 8.0, 28.0, 4.0),
        ("rect", 42.0, 14.0, 8.0, 38.0, 4.0),
        {"mode": "cut", "shape": ("circle", 46.0, 18.5, 2.8)},
    ),
    # B. 代币 + 脉搏线（监视 + token）。
    "token": (
        ("ring", 32.0, 32.0, 17.0, 5.0),
        (
            "polyline",
            (
                (12.0, 33.0),
                (24.0, 33.0),
                (28.5, 21.0),
                (35.5, 43.0),
                (40.0, 29.0),
                (52.0, 29.0),
            ),
            4.2,
        ),
    ),
    # C. 额度表盘：弧 + 指针 + 轴心。
    "gauge": (
        ("arc", 32.0, 33.0, 16.0, 145.0, 395.0, 5.0),
        ("capsule", 32.0, 33.0, 42.5, 22.5, 4.4),
        ("circle", 32.0, 33.0, 3.8),
    ),
    # D. 成本盾牌 + 脉搏线（预算守护）。
    "guard": (
        (
            "polygon",
            (
                (32.0, 12.0),
                (49.0, 18.5),
                (49.0, 33.0),
                (47.0, 41.0),
                (41.0, 47.5),
                (32.0, 52.0),
                (23.0, 47.5),
                (17.0, 41.0),
                (15.0, 33.0),
                (15.0, 18.5),
            ),
        ),
        {"mode": "cut", "shape": (
            "polyline",
            (
                (19.0, 33.0),
                (26.0, 33.0),
                (29.5, 24.5),
                (34.5, 41.0),
                (38.0, 31.0),
                (45.0, 31.0),
            ),
            4.0,
        )},
    ),
    # E. 鲸鱼吉祥物（对应 DeepSeek 那种白色剪影，但为 16px 做了粗简化）。
    "whale": (
        (
            "polygon",
            (
                (13.0, 36.0),
                (14.5, 29.5),
                (19.0, 25.0),
                (26.0, 22.5),
                (34.0, 22.5),
                (41.0, 24.5),
                (45.5, 28.0),
                (47.5, 32.0),
                (47.0, 37.0),
                (43.0, 41.0),
                (36.0, 43.5),
                (27.0, 44.0),
                (19.0, 42.0),
            ),
        ),
        ("polygon", ((47.0, 28.0), (54.0, 21.0), (52.5, 33.0), (54.0, 45.0), (46.5, 37.5))),
        {"mode": "cut", "shape": ("circle", 21.5, 32.0, 2.6)},
    ),
}
# 中文注释：当前上线的方案（改这一个常量即可切换，测试会渲染全部方案）。
# "guard" 来自 Stitch MCP 设计稿的「成本盾牌 + 脉搏线」：16px 下依然能认出盾牌轮廓。
_FAVICON_STYLE = "guard"


def _favicon_shapes() -> tuple[tuple, ...]:
    """返回当前方案的徽章 + 图形原语列表。"""

    badge = {"mode": "fill", "shape": ("rect", 0.0, 0.0, _FAVICON_SIZE, _FAVICON_SIZE, _FAVICON_RADIUS)}
    try:
        glyph = _FAVICON_GLYPHS[_FAVICON_STYLE]
    except KeyError as error:  # pragma: no cover - 常量写错时立刻暴露
        raise ValueError(f"未知的图标方案: {_FAVICON_STYLE}") from error
    shapes: list[tuple] = [badge]
    for item in glyph:
        if isinstance(item, dict):
            shapes.append({"mode": item.get("mode", "cut"), "shape": item["shape"]})
        else:
            shapes.append({"mode": "draw", "shape": item})
    return tuple(shapes)


def _hex_color(color: tuple[int, int, int]) -> str:
    """把 RGB 三元组转成 SVG 用的 #rrggbb。"""

    return "#{:02x}{:02x}{:02x}".format(*color)


def _favicon_token() -> str:
    """返回图标几何的短指纹，用作 favicon URL 的版本参数。"""

    payload = repr(
        (
            _FAVICON_SIZE,
            _FAVICON_BACKGROUND,
            _FAVICON_FOREGROUND,
            _FAVICON_STYLE,
            _FAVICON_GLYPHS[_FAVICON_STYLE],
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]


# 中文注释：先给 ICO（老浏览器与 Safari 稳），再给 SVG（大小屏幕都清晰）。
_LANGUAGE_COOKIE_NAME = "a-token-monitor-language"


def _localize_page(html: str, language: str) -> str:
    """按语言出页面：英文走目录表替换，并改 <html lang>。"""

    if language != "en":
        return html
    return substitute(html, "en").replace('<html lang="zh-CN">', '<html lang="en">', 1)


_FAVICON_LINK = (
    f'  <link rel="icon" type="{_FAVICON_ICO_MIME}" '
    f'href="{_FAVICON_ICO_ROUTE}?v={_favicon_token()}" sizes="16x16 32x32">\n'
    f'  <link rel="icon" type="{_FAVICON_SVG_MIME}" '
    f'href="{_FAVICON_SVG_ROUTE}?v={_favicon_token()}">\n'
)


def _fill_attribute(mode: str, gradient_id: str) -> str:
    """填充类原语的 fill：徽章用渐变，挖洞用黑，图形继承父级白色。"""

    if mode == "badge":
        return f' fill="url(#{gradient_id})"'
    if mode == "cut":
        return ' fill="black"'
    return ""


def _stroke_attribute(mode: str, width: float) -> str:
    """描边类原语的 stroke：图形白色，挖洞黑色。"""

    color = "black" if mode == "cut" else _hex_color(_FAVICON_FOREGROUND)
    return f' stroke="{color}" stroke-width="{width:g}"'


def _shape_svg(shape: tuple, gradient_id: str, mode: str) -> str:
    """把一个几何原语渲染成 SVG 元素。"""

    kind = shape[0]
    if kind == "rect":
        _, x, y, width, height, radius = shape
        return (
            f'<rect x="{x:g}" y="{y:g}" width="{width:g}" height="{height:g}" '
            f'rx="{radius:g}"{_fill_attribute(mode, gradient_id)}/>'
        )
    if kind == "circle":
        _, cx, cy, radius = shape
        return (
            f'<circle cx="{cx:g}" cy="{cy:g}" r="{radius:g}"'
            f'{_fill_attribute(mode, gradient_id)}/>'
        )
    if kind == "ring":
        _, cx, cy, radius, width = shape
        return (
            f'<circle cx="{cx:g}" cy="{cy:g}" r="{radius:g}" fill="none"'
            f'{_stroke_attribute(mode, width)}/>'
        )
    if kind == "capsule":
        _, x1, y1, x2, y2, width = shape
        return (
            f'<line x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" y2="{y2:g}"'
            f'{_stroke_attribute(mode, width)} stroke-linecap="round"/>'
        )
    if kind == "arc":
        _, cx, cy, radius, start, end, width = shape
        points = _arc_points(cx, cy, radius, start, end)
        return (
            f'<polyline points="{_svg_points(points)}" fill="none"'
            f'{_stroke_attribute(mode, width)} stroke-linecap="round" '
            'stroke-linejoin="round"/>'
        )
    if kind == "polyline":
        _, points, width = shape
        return (
            f'<polyline points="{_svg_points(points)}" fill="none"'
            f'{_stroke_attribute(mode, width)} stroke-linecap="round" '
            'stroke-linejoin="round"/>'
        )
    if kind == "polygon":
        _, points = shape
        return (
            f'<polygon points="{_svg_points(points)}"'
            f'{_fill_attribute(mode, gradient_id)}/>'
        )
    raise ValueError(f"未知的几何原语: {kind}")


def _svg_points(points: tuple[tuple[float, float], ...]) -> str:
    """把点序列格式化成 SVG 的 points 属性。"""

    return " ".join(f"{x:g},{y:g}" for x, y in points)


def _arc_points(
    cx: float,
    cy: float,
    radius: float,
    start: float,
    end: float,
    steps: int = 24,
) -> tuple[tuple[float, float], ...]:
    """把一段圆弧离散成折线点（SVG 与光栅化共用同一份采样）。"""

    total = end - start
    return tuple(
        (
            cx + radius * math.cos(math.radians(start + total * index / steps)),
            cy + radius * math.sin(math.radians(start + total * index / steps)),
        )
        for index in range(steps + 1)
    )


def _icon_svg(gradient_id: str, label: str | None = None) -> str:
    """按共享几何生成图标 SVG：标签页图标和页面内 logo 用的是同一份图形。

    ``gradient_id`` 让同一页面里多处引用也不会撞 id；``label`` 为空时按装饰性
    图形处理（外层已经有 aria-hidden）。
    """

    stops = "".join(
        f'<stop offset="{index}" stop-color="{_hex_color(color)}"/>'
        for index, color in enumerate(_FAVICON_BACKGROUND)
    )
    size = int(_FAVICON_SIZE)
    attributes = f'role="img" aria-label="{label}"' if label else 'aria-hidden="true"'
    shapes = _favicon_shapes()
    badge = _shape_svg(shapes[0]["shape"], gradient_id, "badge")
    draws = [entry["shape"] for entry in shapes[1:] if entry["mode"] != "cut"]
    cuts = [entry["shape"] for entry in shapes[1:] if entry["mode"] == "cut"]
    parts = [
        f'<defs><linearGradient id="{gradient_id}" x1="0" y1="0" x2="1" y2="1">'
        f"{stops}</linearGradient></defs>",
        badge,
    ]
    if draws:
        glyph = "".join(
            _shape_svg(shape, gradient_id, "glyph") for shape in draws
        )
        if cuts:
            # 中文注释：白色图形统一走一个 mask 组，黑色原语即为挖掉的洞
            # （例如柱状图的高水位点、盾牌里的脉搏线），露出的就是渐变底。
            mask_id = f"{gradient_id}-glyph"
            holes = "".join(
                _shape_svg(shape, gradient_id, "cut") for shape in cuts
            )
            parts.append(
                f'<mask id="{mask_id}"><rect width="{size}" height="{size}" '
                f'fill="white"/>{holes}</mask>'
            )
            parts.append(
                f'<g mask="url(#{mask_id})" fill="{_hex_color(_FAVICON_FOREGROUND)}">'
                f"{glyph}</g>"
            )
        else:
            parts.append(
                f'<g fill="{_hex_color(_FAVICON_FOREGROUND)}">{glyph}</g>'
            )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f"{attributes}>" + "".join(parts) + "</svg>"
    )


def _favicon_svg() -> str:
    """浏览器标签页图标：带尺寸和可访问名称，可独立作为图片加载。"""

    svg = _icon_svg("badge", label="Token Monitor")
    return svg.replace(
        "<svg ",
        f'<svg width="{int(_FAVICON_SIZE)}" height="{int(_FAVICON_SIZE)}" ',
        1,
    )


def _brand_mark_svg() -> str:
    """页面内品牌图形：与标签页图标同一份几何，只换主题里的渐变 id。"""

    return _icon_svg("brand-badge")


def _inside_rounded_square(
    x: float,
    y: float,
    width: float,
    height: float,
    radius: float,
) -> bool:
    """判断点是否落在圆角矩形内（用于光栅化时的覆盖率采样）。"""

    inner_x = min(max(x, radius), width - radius)
    inner_y = min(max(y, radius), height - radius)
    delta_x = x - inner_x
    delta_y = y - inner_y
    return delta_x * delta_x + delta_y * delta_y <= radius * radius


def _distance_to_segment(
    x: float,
    y: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> float:
    """点到线段的距离。"""

    delta_x = x2 - x1
    delta_y = y2 - y1
    length_squared = delta_x * delta_x + delta_y * delta_y
    if length_squared <= 0:
        return math.hypot(x - x1, y - y1)
    ratio = ((x - x1) * delta_x + (y - y1) * delta_y) / length_squared
    ratio = min(1.0, max(0.0, ratio))
    return math.hypot(x - (x1 + ratio * delta_x), y - (y1 + ratio * delta_y))


def _inside_polygon(x: float, y: float, points: tuple[tuple[float, float], ...]) -> bool:
    """射线法判断点是否在多边形内。"""

    inside = False
    count = len(points)
    for index in range(count):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % count]
        if (y1 > y) != (y2 > y):
            crossing = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < crossing:
                inside = not inside
    return inside


def _inside_shape(x: float, y: float, shape: tuple) -> bool:
    """判断点是否落在某个几何原语内（描边类形状按半宽判定）。"""

    kind = shape[0]
    if kind == "rect":
        _, rect_x, rect_y, width, height, radius = shape
        return _inside_rounded_square(x - rect_x, y - rect_y, width, height, radius)
    if kind == "circle":
        _, cx, cy, radius = shape
        return math.hypot(x - cx, y - cy) <= radius
    if kind == "ring":
        _, cx, cy, radius, width = shape
        return abs(math.hypot(x - cx, y - cy) - radius) <= width / 2
    if kind == "capsule":
        _, x1, y1, x2, y2, width = shape
        return _distance_to_segment(x, y, x1, y1, x2, y2) <= width / 2
    if kind == "arc":
        _, cx, cy, radius, start, end, width = shape
        return _distance_to_polyline(
            x, y, _arc_points(cx, cy, radius, start, end)
        ) <= width / 2
    if kind == "polyline":
        _, points, width = shape
        return _distance_to_polyline(x, y, points) <= width / 2
    if kind == "polygon":
        _, points = shape
        return _inside_polygon(x, y, points)
    raise ValueError(f"未知的几何原语: {kind}")


def _distance_to_polyline(
    x: float,
    y: float,
    points: tuple[tuple[float, float], ...],
) -> float:
    """点到折线的最短距离（圆角连接靠逐段取最小自然成立）。"""

    if len(points) < 2:
        return math.hypot(x - points[0][0], y - points[0][1]) if points else math.inf
    return min(
        _distance_to_segment(x, y, x1, y1, x2, y2)
        for (x1, y1), (x2, y2) in zip(points, points[1:])
    )


def _favicon_sample(x: float, y: float) -> tuple[int, int, int, int]:
    """返回 64×64 画布上某个采样点的 RGBA：图形纯白，徽章按对角渐变。"""

    shapes = _favicon_shapes()
    badge = shapes[0]["shape"]
    for entry in shapes[1:]:
        if entry["mode"] == "cut" and _inside_shape(x, y, entry["shape"]):
            return (*_gradient_color(x, y), 255)
    if not _inside_shape(x, y, badge):
        return (0, 0, 0, 0)
    for entry in shapes[1:]:
        if entry["mode"] != "cut" and _inside_shape(x, y, entry["shape"]):
            return (*_FAVICON_FOREGROUND, 255)
    return (*_gradient_color(x, y), 255)


def _gradient_color(x: float, y: float) -> tuple[int, int, int]:
    """按对角线位置在品牌渐变上取色。"""

    ratio = min(1.0, max(0.0, (x + y) / (2.0 * _FAVICON_SIZE)))
    start, end = _FAVICON_BACKGROUND
    return (
        round(start[0] + (end[0] - start[0]) * ratio),
        round(start[1] + (end[1] - start[1]) * ratio),
        round(start[2] + (end[2] - start[2]) * ratio),
    )


def _favicon_pixels(size: int) -> bytes:
    """把图标光栅化成 RGBA 扫描行；每个像素做 4×4 超采样抗锯齿。"""

    samples = 4
    total_samples = samples * samples
    rows = bytearray()
    for row in range(size):
        rows.append(0)  # PNG 每行前缀：过滤器类型 0（None）
        for column in range(size):
            red = green = blue = alpha_sum = 0.0
            for sub_row in range(samples):
                for sub_column in range(samples):
                    # 采样点从目标像素换算回 64×64 画布坐标。
                    x = (column + (sub_column + 0.5) / samples) * _FAVICON_SIZE / size
                    y = (row + (sub_row + 0.5) / samples) * _FAVICON_SIZE / size
                    sample = _favicon_sample(x, y)
                    weight = sample[3] / 255.0
                    red += sample[0] * weight
                    green += sample[1] * weight
                    blue += sample[2] * weight
                    alpha_sum += weight
            if alpha_sum <= 0:
                rows.extend((0, 0, 0, 0))
                continue
            # 先按预乘 alpha 平均，再还原颜色，避免边缘出现黑边。
            rows.extend(
                (
                    round(red / alpha_sum),
                    round(green / alpha_sum),
                    round(blue / alpha_sum),
                    round(alpha_sum / total_samples * 255),
                )
            )
    return bytes(rows)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    """组装一个 PNG 数据块（长度 + 类型 + 内容 + CRC32）。"""

    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _favicon_png(size: int) -> bytes:
    """生成 8 位 RGBA 的 PNG 图标，只依赖 zlib。"""

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(_favicon_pixels(size), 9))
        + _png_chunk(b"IEND", b"")
    )


def _favicon_ico() -> bytes:
    """把多个尺寸的 PNG 打包成 ICO（Vista 起支持 PNG 负载，结果做进程内缓存）。"""

    global _FAVICON_ICO_CACHE
    if _FAVICON_ICO_CACHE is not None:
        return _FAVICON_ICO_CACHE
    images = [(size, _favicon_png(size)) for size in _FAVICON_ICO_SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = bytearray()
    for size, image in images:
        entries.extend(
            struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(image), offset)
        )
        offset += len(image)
    _FAVICON_ICO_CACHE = b"".join([header, bytes(entries)] + [i for _, i in images])
    return _FAVICON_ICO_CACHE


def favicon_response(path: str) -> tuple[str, bytes] | None:
    """返回 favicon 路由对应的 MIME 与内容；不是 favicon 路由时返回 None。

    带 ``?v=<指纹>`` 的版本参数会被忽略，这样页面可以用内容寻址的 URL
    绕开浏览器里「这个页面没有图标」的旧缓存。
    """

    route = path.split("?", 1)[0]
    if route == _FAVICON_SVG_ROUTE:
        return _FAVICON_SVG_MIME, _favicon_svg().encode("utf-8")
    if route == _FAVICON_ICO_ROUTE:
        return _FAVICON_ICO_MIME, _favicon_ico()
    return None


# 中文注释：主题实现由 Dashboard 与设置页共享，避免两个页面各写一份。
_LANGUAGE_BOOT_SCRIPT = r"""  <script>
    // 语言预置：localStorage 里记住的选择优先于浏览器语言。服务端按 cookie 决定语言，
    // 所以这里发现不一致就补写 cookie 并重载一次；用 sessionStorage 打标记，避免在
    // 禁用 cookie 的浏览器里来回重载。
    (() => {
      try {
        const stored = window.localStorage.getItem('a-token-monitor-language');
        if (stored !== 'zh' && stored !== 'en') return;
        const current = document.documentElement.lang === 'en' ? 'en' : 'zh';
        if (stored === current) return;
        if (window.sessionStorage.getItem('a-token-monitor-language-applied') === stored) return;
        window.sessionStorage.setItem('a-token-monitor-language-applied', stored);
        document.cookie = `a-token-monitor-language=${stored}; path=/; max-age=31536000; SameSite=Lax`;
        window.location.reload();
      } catch (error) {
        // 拿不到存储就按服务端语言渲染，不折腾。
      }
    })();
  </script>
"""

_THEME_BOOT_SCRIPT = r"""  <script>
    // 主题预置：在样式解析前写入 data-theme / data-theme-mode，避免切换主题时闪白或闪黑。
    (() => {
      try {
        const stored = window.localStorage.getItem('a-token-monitor-theme');
        const mode = ['light', 'dark', 'system'].includes(stored) ? stored : 'system';
        const prefersLight = window.matchMedia
          ? window.matchMedia('(prefers-color-scheme: light)').matches
          : false;
        const light = mode === 'light' || (mode === 'system' && prefersLight);
        const root = document.documentElement;
        root.dataset.theme = light ? 'light' : 'dark';
        root.dataset.themeMode = mode;
      } catch (error) {
        document.documentElement.dataset.theme = 'dark';
        document.documentElement.dataset.themeMode = 'dark';
      }
    })();
  </script>
"""

_LANGUAGE_TOGGLE_HTML = r"""        <button id="language-toggle" class="theme-toggle" type="button" title="切换到英文（English）" aria-label="切换到英文界面">
          <span id="language-label">EN</span>
        </button>
"""

_THEME_TOGGLE_HTML = r"""        <button id="theme-toggle" class="theme-toggle" type="button" title="主题：跟随系统（点击切换）" aria-label="切换主题，当前跟随系统">
          <svg class="theme-icon theme-icon-system" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4.5" width="18" height="12" rx="2"/><path d="M8 20h8M12 16.5v3.5"/></svg>
          <svg class="theme-icon theme-icon-light" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.2 5.2l1.4 1.4M17.4 17.4l1.4 1.4M18.8 5.2l-1.4 1.4M6.6 17.4l-1.4 1.4"/></svg>
          <svg class="theme-icon theme-icon-dark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z"/></svg>
          <span id="theme-label">跟随系统</span>
        </button>
"""

_LANGUAGE_SCRIPT = r"""  // 语言：点一下切到另一种语言。界面文案由服务端按语言渲染，所以这里写 cookie +
  // localStorage 后整页重载；cookie 让后续的接口请求也带上同一种语言。
  const LANGUAGE_STORAGE_KEY = 'a-token-monitor-language';
  const LANGUAGE_COOKIE = 'a-token-monitor-language';
  const currentLanguage = document.documentElement.lang === 'en' ? 'en' : 'zh';
  const writeLanguageCookie = (language) => {
    const maxAge = 60 * 60 * 24 * 365;
    document.cookie = `${LANGUAGE_COOKIE}=${language}; path=/; max-age=${maxAge}; SameSite=Lax`;
  };
  const switchLanguage = () => {
    const next = currentLanguage === 'en' ? 'zh' : 'en';
    writeLanguageCookie(next);
    try {
      window.localStorage.setItem(LANGUAGE_STORAGE_KEY, next);
    } catch (error) {
      // 隐私模式下写不了 localStorage：cookie 仍然生效。
    }
    window.location.reload();
  };
  const languageToggle = document.getElementById('language-toggle');
  if (languageToggle) {
    const label = document.getElementById('language-label');
    if (label) label.textContent = currentLanguage === 'en' ? '中' : 'EN';
    languageToggle.title = currentLanguage === 'en'
      ? '切换到中文（Chinese）'
      : '切换到英文（English）';
    languageToggle.setAttribute('aria-label', languageToggle.title);
    languageToggle.addEventListener('click', switchLanguage);
  }
"""

_THEME_SCRIPT = r"""  // 主题：跟随系统 / 白天 / 夜间，选择存 localStorage，切换不需要刷新页面。
  const THEME_STORAGE_KEY = 'a-token-monitor-theme';
  const THEME_MODES = ['system', 'light', 'dark'];
  const THEME_LABELS = { system: '跟随系统', light: '白天', dark: '夜间' };
  const themeQuery = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;
  const readThemeMode = () => {
    try {
      const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
      return THEME_MODES.includes(stored) ? stored : 'system';
    } catch (error) {
      return 'system';
    }
  };
  const resolvedTheme = (mode) => (mode === 'system'
    ? (themeQuery && themeQuery.matches ? 'light' : 'dark')
    : mode);
  const applyTheme = () => {
    const mode = readThemeMode();
    const root = document.documentElement;
    root.dataset.theme = resolvedTheme(mode);
    root.dataset.themeMode = mode;
    const label = document.getElementById('theme-label');
    if (label) label.textContent = THEME_LABELS[mode];
    const toggle = document.getElementById('theme-toggle');
    if (toggle) {
      toggle.title = `主题：${THEME_LABELS[mode]}（点击切换）`;
      toggle.setAttribute('aria-label', `切换主题，当前${THEME_LABELS[mode]}`);
    }
  };
  const cycleTheme = () => {
    const mode = readThemeMode();
    const next = THEME_MODES[(THEME_MODES.indexOf(mode) + 1) % THEME_MODES.length];
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch (error) {
      // 隐私模式下写不了 localStorage：本次仍然生效，只是不记忆。
    }
    applyTheme();
  };
  document.getElementById('theme-toggle')?.addEventListener('click', cycleTheme);
  if (themeQuery && themeQuery.addEventListener) {
    themeQuery.addEventListener('change', () => {
      if (readThemeMode() === 'system') applyTheme();
    });
  }
  applyTheme();
"""


_PAGE_THEME_REPLACEMENTS = (
    ("__THEME_BOOT__", _THEME_BOOT_SCRIPT + _LANGUAGE_BOOT_SCRIPT),
    ("__THEME_TOGGLE__", _LANGUAGE_TOGGLE_HTML + _THEME_TOGGLE_HTML),
    ("__THEME_SCRIPT__", _THEME_SCRIPT + _LANGUAGE_SCRIPT),
    ("__FAVICON__", _FAVICON_LINK),
    ("__BRAND_MARK__", _brand_mark_svg()),
)


def _apply_theme_parts(html: str) -> str:
    """把共享的主题实现和页面标签注入模板，并确认占位符都已替换。"""

    for marker, snippet in _PAGE_THEME_REPLACEMENTS:
        html = html.replace(marker, snippet)
    return html


_DASHBOARD_HTML = _apply_theme_parts((
    r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
__FAVICON__
__THEME_BOOT__
  <title>Token Monitor</title>
  <style>
"""
    + _BASE_CSS
    + _DASHBOARD_CSS
    + _RESPONSIVE_CSS
    + r"""  </style>
</head>
<body>
<div class="app-shell">
  <aside class="sidebar" aria-label="Dashboard 导航">
    <div class="sidebar-brand">
      <div class="brand-mark" aria-hidden="true">__BRAND_MARK__</div>
      <div><div class="brand-name">Token <span>Monitor</span></div><small>本地 code agent 控制台</small></div>
    </div>
    <div class="sidebar-label">工作台</div>
    <nav>
      <a class="sidebar-link active" data-nav-target="overview" href="#overview"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><rect x="14" y="14" width="6" height="6" rx="1"/></svg><span>总览</span></a>
      <a class="sidebar-link" data-nav-target="traffic" href="#traffic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12h16"/><path d="M13 5l7 7-7 7"/></svg><span>异常流量监控</span></a>
      <a class="sidebar-link" data-nav-target="accounts" href="#accounts"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="3"/><path d="M5 20c.8-3.4 3.1-5 7-5s6.2 1.6 7 5"/></svg><span>账号与额度</span></a>
      <a class="sidebar-link" data-nav-target="usage" href="#usage"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 19V9M12 19V5M19 19v-7"/><path d="M3 19h18"/></svg><span>用量与费用</span></a>
      <a class="sidebar-link" data-nav-target="insights" href="#insights"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18h6M10 21.5h4"/><path d="M12 3a6 6 0 0 0-3.3 11.1c.8.5 1.3 1.3 1.3 2.2v.7h4v-.7c0-.9.5-1.7 1.3-2.2A6 6 0 0 0 12 3z"/></svg><span>习惯分析</span></a>
    </nav>
    <div class="sidebar-label">按需查看</div>
    <nav>
      <a class="sidebar-link" data-nav-target="alert-history" href="#alert-history"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 7v5l3 2"/><circle cx="12" cy="12" r="8"/></svg><span>告警历史</span></a>
      <a class="sidebar-link" data-nav-target="usage-search" href="#usage-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="6"/><path d="M15.5 15.5 20 20"/></svg><span>用量检索</span></a>
      <a class="sidebar-link" data-nav-target="housekeeping" href="#housekeeping"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h10"/></svg><span>磁盘与会话管理</span></a>
      <a class="sidebar-link" href="/settings"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7 7 0 0 0-2-1.2L14 3h-4l-.5 2.6a7 7 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6A7 7 0 0 0 5 12c0 .4 0 .8.1 1.2l-2 1.6 2 3.4 2.4-1a7 7 0 0 0 2 1.2L10 21h4l.5-2.6a7 7 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2z"/></svg><span>设置</span></a>
    </nav>
    <div class="sidebar-bottom">
      <div class="service-state"><span id="service-dot" class="status-dot"></span><span id="service-state">监控服务在线</span></div>
      <div class="sidebar-foot">状态每 5 秒同步 · 只记录元数据</div>
    </div>
  </aside>

  <main>
    <header class="topbar">
      <div class="breadcrumb"><span>Token Monitor</span><span class="breadcrumb-separator">/</span><strong>Dashboard</strong></div>
      <div class="topbar-actions">
        <div class="live-indicator"><span class="status-dot"></span><span id="service-sync">等待首次同步</span></div>
        <button id="health-indicator" class="health-badge" type="button" style="display:none" aria-expanded="false"></button>
__THEME_TOGGLE__
        <button id="refresh-button" class="refresh-button" type="button" aria-label="立即刷新状态"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14.7-4L4 9"/><path d="M4 4v5h5"/><path d="M4 13a8 8 0 0 0 14.7 4L20 15"/><path d="M20 20v-5h-5"/></svg><span>刷新</span></button>
      </div>
    </header>

    <div id="health-detail" class="health-detail" style="display:none"></div>

    <section id="overview" class="page-hero">
      <div>
        <div class="eyebrow">实时监控 · Codex / Grok / Kimi / DSH / Command Code</div>
        <h1>运行概览</h1>
        <p class="hero-description">实时查看账号额度、活动会话、本地用量与 token 历史，掌握异常流量告警，并管理磁盘占用和历史会话的归档与清理。</p>
      </div>
      <div class="hero-context"><span class="scope-badge"><span class="scope-dot"></span><span id="quota-source">正在识别账号</span></span></div>
    </section>

    <div id="error" class="error" role="alert"></div>
    <div id="alert-list"></div>
    <div id="session-notice"></div>

    <section class="kpi-grid" aria-label="监控摘要">
      <article class="kpi-card accent-cyan"><div class="kpi-top"><span class="kpi-label">近 15 秒外发</span><span class="kpi-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12h16M13 5l7 7-7 7"/></svg></span></div><div id="upload-burst-count" class="kpi-value">—</div><div class="kpi-foot"><strong id="upload-alert-count">—</strong> 条未读告警 · <a href="#alert-history">历史</a></div></article>
      <article class="kpi-card accent-violet"><div class="kpi-top"><span class="kpi-label">活动会话</span><span class="kpi-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="3"/><path d="M5 20c.8-3.4 3.1-5 7-5s6.2 1.6 7 5"/></svg></span></div><div id="active-count" class="kpi-value">—</div><div class="kpi-foot"><strong id="process-count">—</strong> 个有进程证据</div></article>
      <article class="kpi-card accent-yellow"><div class="kpi-top"><span class="kpi-label">额度窗口</span><span class="kpi-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 10h8M8 14h5"/></svg></span></div><div id="quota-window-count" class="kpi-value">—</div><div class="kpi-foot">当前账号可见的额度窗口</div></article>
      <article class="kpi-card accent-cyan"><div class="kpi-top"><span class="kpi-label">监控账号</span><span class="kpi-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 10h8M8 14h5"/></svg></span></div><div id="accounts-online" class="kpi-value">—</div><div class="kpi-foot">按真实账号 ID 归组</div></article>
      <article class="kpi-card accent-green"><div class="kpi-top"><span class="kpi-label">今日 API 等价金额</span><span class="kpi-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M14.8 9.5c-.6-.7-1.5-1-2.7-1-1.4 0-2.3.7-2.3 1.7 0 2.5 5 1.2 5 3.8 0 1-.9 1.7-2.5 1.7-1.2 0-2.2-.4-2.9-1.2M12 6.7v1.8M12 15.7v1.8"/></svg></span></div><div id="spend-count" class="kpi-value">—</div><div class="kpi-foot">非 Plus 实际账单；索引完成后显示</div></article>
    </section>

    <section id="traffic" class="panel section-block">
      <div class="panel-heading">
        <div><div class="section-kicker">Traffic anomaly</div><h2>异常流量监控</h2><p class="section-description">按进程统计 code agent 的 TCP 外发增量，发现异常大数据上传；回环不计入，不读取连接内容。</p></div>
        <div class="section-meta"><span class="section-count" id="traffic-section-count">等待扫描</span></div>
      </div>
      <div id="traffic-content"><div class="empty-state">正在扫描本机 code agent 异常流量…</div></div>
    </section>

    <section id="accounts" class="panel section-block">
      <div class="panel-heading">
        <div><div class="section-kicker">Account health</div><h2>账号与额度</h2><p class="section-description">卡片以「产品 · 套餐」为标题、按真实账号 ID 归并，固定展示 5 小时 / 周 / 月三行额度（缺的标「不适用」）；没有额度窗口的订阅不显示，活动会话默认折叠。</p></div>
        <div class="section-meta"><span class="section-count" id="account-section-count">— 个账号</span></div>
      </div>
      <div id="account-list" class="account-list"><div class="empty-state">正在读取账号状态…</div></div>
    </section>

    <section id="usage" class="panel section-block">
      <div class="panel-heading">
        <div><div class="section-kicker">Usage analytics</div><h2>用量与成本估算</h2></div>
        <div class="section-meta"><span class="section-count">按需统计 · 缓存 5 分钟</span><button id="usage-load-button" class="refresh-button" type="button">加载用量</button></div>
      </div>
      <div id="usage-content"><div class="empty-state">为避免周期读取大量历史 JSONL，用量统计改为按需加载。</div></div>
    </section>
    <section id="insights" class="panel section-block">      <div class="panel-heading">
        <div><div class="section-kicker">Insights</div><h2>习惯分析与省 token 建议</h2><p class="section-description">基于已索引对话的 token 元数据提炼使用习惯，可按时间段切换；不读取对话内容。</p></div>
        <div class="section-meta"><span class="section-count">按需分析 · 基于用量索引</span><button id="insights-load-button" class="refresh-button" type="button">生成分析</button></div>
      </div>
      <div class="usage-tabs" id="insights-period-tabs">
        <button class="usage-tab selected" type="button" data-insights-days="">全部</button>
        <button class="usage-tab" type="button" data-insights-days="today">今天</button>
        <button class="usage-tab" type="button" data-insights-days="7">近 7 天</button>
        <button class="usage-tab" type="button" data-insights-days="30">近 30 天</button>
      </div>
      <div id="insights-content"><div class="empty-state">点击「生成分析」，对已索引的对话做使用习惯画像并给出省 token 建议。</div></div>
    </section>
    <section id="alert-history" class="panel section-block is-collapsed">
      <div class="panel-heading">
        <button class="section-toggle" type="button" data-section-toggle="alert-history" aria-expanded="false" aria-controls="alert-history-body">
          <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>
          <span class="section-toggle-text">
            <span class="section-kicker">Alert history</span>
            <span class="section-toggle-title">告警历史</span>
            <span class="section-description">异常流量告警落盘在状态目录，可按时间、级别、规则和已读状态筛选；只保存进程、对端与字节数等元数据。</span>
            <span class="section-summary" id="alert-history-summary">展开查看详情</span>
          </span>
        </button>
        <div class="section-meta"><span class="section-count" id="alert-history-count">等待加载</span><button id="alert-history-load-button" class="btn primary" type="button">加载告警</button></div>
      </div>
      <div class="section-body" id="alert-history-body">
      <div class="stat-row" id="alert-history-stats"></div>
      <div class="toolbar">
        <label class="field">时间范围<select id="alert-range-filter">
          <option value="1">近 24 小时</option>
          <option value="7" selected>近 7 天</option>
          <option value="30">近 30 天</option>
          <option value="">全部</option>
        </select></label>
        <label class="field">级别<select id="alert-level-filter">
          <option value="">全部级别</option>
          <option value="danger">红色 · 异常大上传</option>
          <option value="warn">黄色 · 偏高</option>
        </select></label>
        <label class="field">规则<select id="alert-kind-filter">
          <option value="">全部规则</option>
          <option value="burst">突发窗口</option>
          <option value="window">累计窗口</option>
        </select></label>
        <label class="field">状态<select id="alert-ack-filter">
          <option value="unread" selected>未读</option>
          <option value="read">已读</option>
          <option value="">全部</option>
        </select></label>
        <label class="field wide">关键词<input id="alert-keyword-filter" type="search" placeholder="Agent / 目录 / 对端"></label>
        <div class="toolbar-actions">
          <button id="alert-ack-all-button" class="btn" type="button">全部标为已读</button>
          <button id="alert-clear-button" class="btn danger" type="button">清理…</button>
        </div>
      </div>
      <div id="alert-history-content"><div class="empty-state"><span class="empty-title">还没有加载告警</span><span class="empty-hint">点击「加载告警」，读取已落盘的历史告警。</span></div></div>
      </div>
    </section>
    <section id="usage-search" class="panel section-block is-collapsed">
      <div class="panel-heading">
        <button class="section-toggle" type="button" data-section-toggle="usage-search" aria-expanded="false" aria-controls="usage-search-body">
          <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>
          <span class="section-toggle-text">
            <span class="section-kicker">Usage search</span>
            <span class="section-toggle-title">用量检索</span>
            <span class="section-description">检索已索引的 token 历史，可切换会话明细 / 按日期 / 按模型 / 按账号四种视图；只读 token 元数据，不读对话内容。</span>
            <span class="section-summary" id="usage-search-summary">展开查看详情</span>
          </span>
        </button>
        <div class="section-meta"><span class="section-count" id="usage-search-count">等待检索</span><button id="usage-search-load-button" class="btn primary" type="button">检索</button></div>
      </div>
      <div class="section-body" id="usage-search-body">
      <div class="stat-row" id="usage-search-stats"></div>
      <div class="toolbar">
        <label class="field">时间范围<select id="usage-search-range">
          <option value="7">近 7 天</option>
          <option value="30" selected>近 30 天</option>
          <option value="90">近 90 天</option>
          <option value="0">全部历史</option>
        </select></label>
        <label class="field">起始日期<input id="usage-search-from" type="date"></label>
        <label class="field">结束日期<input id="usage-search-to" type="date"></label>
        <label class="field">模型<select id="usage-search-model"><option value="">全部模型</option></select></label>
        <label class="field">账号<select id="usage-search-account"><option value="">全部账号</option></select></label>
        <label class="field wide">关键词<input id="usage-search-keyword" type="search" placeholder="会话 ID / 项目路径 / 模型"></label>
        <label class="field">排序<select id="usage-search-sort">
          <option value="recent" selected>最近活动</option>
          <option value="tokens">token 用量</option>
          <option value="cost">估算金额</option>
        </select></label>
        <div class="toolbar-actions">
          <button id="usage-search-refresh-button" class="btn" type="button">刷新</button>
        </div>
      </div>
      <div class="usage-tabs" id="usage-search-group-tabs">
        <button class="usage-tab selected" type="button" data-usage-search-group="session">会话明细</button>
        <button class="usage-tab" type="button" data-usage-search-group="date">按日期汇总</button>
        <button class="usage-tab" type="button" data-usage-search-group="model">按模型汇总</button>
        <button class="usage-tab" type="button" data-usage-search-group="account">按账号汇总</button>
      </div>
      <div id="usage-search-content"><div class="empty-state"><span class="empty-title">还没有检索</span><span class="empty-hint">点击「检索」，按日期、模型或会话查找历史 token 用量。</span></div></div>
      </div>
    </section>
    <section id="housekeeping" class="panel section-block is-collapsed">
      <div class="panel-heading">
        <button class="section-toggle" type="button" data-section-toggle="housekeeping" aria-expanded="false" aria-controls="housekeeping-body">
          <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>
          <span class="section-toggle-text">
            <span class="section-kicker">Housekeeping</span>
            <span class="section-toggle-title">磁盘与会话管理</span>
            <span class="section-description">统计各 agent 数据目录占用，超阈值提醒；可按最后修改时间归档（tar.gz + manifest，可恢复）或清理 Codex 会话，都会跳过运行中的会话。</span>
            <span class="section-summary" id="housekeeping-summary">展开查看详情</span>
          </span>
        </button>
        <div class="section-meta"><span class="section-count" id="housekeeping-count">等待扫描</span><button id="housekeeping-scan-button" class="btn primary" type="button">重新扫描</button></div>
      </div>
      <div class="section-body" id="housekeeping-body">
      <div class="stat-row" id="housekeeping-stats"></div>
      <div id="housekeeping-content"><div class="empty-state"><span class="empty-title">正在统计目录占用…</span></div></div>
      <div class="criteria-row">
        <label class="field">保留天数<input id="housekeeping-days" type="number" min="1" max="3650" value="30"></label>
        <label class="field">最小体积 MiB<input id="housekeeping-min-size" type="number" min="0" value="0"></label>
        <div class="toolbar-actions">
          <button id="housekeeping-preview-button" class="btn" type="button">预览可归档会话</button>
          <button id="housekeeping-archive-button" class="btn warn" type="button">压缩归档</button>
          <button id="housekeeping-clean-button" class="btn danger" type="button">直接清理</button>
        </div>
      </div>
      <div id="housekeeping-actions"></div>
      </div>
    </section>
  </main>
</div>
<script>
  const statusNames = {
    running: '运行中', limit_blocked: '额度受限',
    waiting_for_approval: '等待批准', discovered: '已发现',
    completed: '已完成', failed: '失败', orphaned: '已结束', unknown: '未知'
  };
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
  const formatTime = (seconds) => {
    if (seconds === null || seconds === undefined) return '未知';
    return new Date(Number(seconds) * 1000).toLocaleString();
  };
  const formatRelativeTime = (seconds) => {
    if (seconds === null || seconds === undefined) return '未知';
    const delta = Math.max(0, Date.now() / 1000 - Number(seconds));
    if (delta < 60) return `${Math.floor(delta)} 秒前`;
    if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
    if (delta < 86400) return `${(delta / 3600).toFixed(1)} 小时前`;
    return `${(delta / 86400).toFixed(1)} 天前`;
  };
  const formatDay = (seconds) => {
    if (seconds === null || seconds === undefined) return '未知';
    const moment = new Date(Number(seconds) * 1000);
    const pad = (value) => String(value).padStart(2, '0');
    return `${moment.getFullYear()}-${pad(moment.getMonth() + 1)}-${pad(moment.getDate())}`;
  };
  const formatReset = (seconds) => {
    if (seconds === null || seconds === undefined) return '未知';
    const remaining = Math.round(Number(seconds) - Date.now() / 1000);
    const suffix = remaining > 0 ? `（${Math.ceil(remaining / 60)} 分钟后）` : '（已到时间）';
    return `${formatTime(seconds)} ${suffix}`;
  };
  const formatPercent = (value) => value === null || value === undefined ? '未知' : `${Number(value).toFixed(1)}%`;
  const formatNumber = (value) => Number(value || 0).toLocaleString('zh-CN');
  const formatTokens = (value) => {
    const amount = Number(value || 0);
    if (amount >= 1e9) return `${(amount / 1e9).toFixed(2)}B`;
    if (amount >= 1e6) return `${(amount / 1e6).toFixed(2)}M`;
    if (amount >= 1e3) return `${(amount / 1e3).toFixed(1)}K`;
    return String(amount);
  };
  const formatBytes = (value) => `${(Number(value || 0) / 1024 / 1024).toFixed(1)} MiB`;
  const formatDataSize = (value) => {
    const amount = Math.max(0, Number(value || 0));
    if (amount < 1024) return `${Math.round(amount)} B`;
    if (amount < 1024 * 1024) return `${(amount / 1024).toFixed(amount >= 10240 ? 0 : 1)} KiB`;
    if (amount < 1024 * 1024 * 1024) return `${(amount / 1024 / 1024).toFixed(amount >= 10 * 1024 * 1024 ? 1 : 2)} MiB`;
    return `${(amount / 1024 / 1024 / 1024).toFixed(2)} GiB`;
  };
  const formatCredits = (value) => value === null || value === undefined ? '不可反推' : `${Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 4 })} credits`;
  const formatUsd = (value) => value === null || value === undefined ? '未知' : `$${Number(value).toFixed(6)}`;
  // 中文注释：汇总卡用同一套金额格式——≥$1 保留 2 位、小额保留 4 位，
  // 避免「$7.117772」这种六位小数和旁边「$113.97」两种风格并排。
  const formatUsdSummary = (value) => {
    if (value === null || value === undefined) return '未知';
    const number = Number(value);
    return `$${number.toFixed(Math.abs(number) >= 1 ? 2 : 4)}`;
  };
  const formatUsdCompact = (value) => value === null || value === undefined ? '未计价' : `$${Number(value).toFixed(2)}`;
  // 中文注释：头像显示订阅缩写，而不是账号 ID 的前两位（"3" / "5" 这种看不出含义）。
  // 规则：产品与套餐各取单词首字母，最多 3 个字符——Codex · Plus → CP、
  // Codex · Pro Lite → CPL、Grok · SuperGrok → GS、Command Code · GOAT → CCG、Kimi → K。
  const subscriptionInitials = (value) => {
    const words = String(value || '').split(/[^0-9A-Za-z\u4e00-\u9fff]+/).filter(Boolean);
    if (!words.length) return '?';
    const letters = [words[0][0], ...words.slice(1).map((word) => word[0])];
    return letters.join('').slice(0, 3).toUpperCase() || '?';
  };
  // 订阅类型：Codex 的 planType 是小写（plus / prolite），Grok 是 SuperGrok，
  // Command Code 是 GOAT；未收录的值只做首字母大写，形如模型名的原样保留。
  const PLAN_NAMES = {
    plus: 'Plus', pro: 'Pro', prolite: 'Pro Lite', probusiness: 'Pro Business',
    team: 'Team', business: 'Business', enterprise: 'Enterprise', free: 'Free',
    edu: 'Edu', max: 'Max', supergrok: 'SuperGrok',
    supergrokheavy: 'SuperGrok Heavy', goat: 'GOAT'
  };
  const planLabel = (value) => {
    const raw = String(value || '').trim();
    if (!raw) return '';
    if (raw.includes('/')) return raw;
    const mapped = PLAN_NAMES[raw.toLowerCase().replace(/[\s_-]+/g, '')];
    if (mapped) return mapped;
    return raw.charAt(0).toUpperCase() + raw.slice(1);
  };
  const statusClass = (status) => ['running', 'limit_blocked', 'waiting_for_approval', 'failed', 'orphaned'].includes(status) ? status : 'other';
  // 额度窗口统一成固定行：5 小时 / 周 / 月永远都在、顺序固定，缺的周期显示「不适用」，
  // 这样不同订阅的卡片同构、同名行横向对齐；完全没有额度窗口的订阅只给一行说明。
  const QUOTA_PERIOD_LABELS = { five_hours: '5 小时', day: '日', week: '周', month: '月', other: '其它' };
  const QUOTA_PERIOD_ORDER = ['five_hours', 'day', 'week', 'month', 'other'];
  const QUOTA_FIXED_PERIODS = ['five_hours', 'week', 'month'];
  // 兜底：万一后端没带 period，就按和后端一致的规则现场判一次（时长优先、名字兜底）。
  const quotaPeriod = (window) => {
    if (window && window.period) return window.period;
    const minutes = Number(window && window.window_minutes);
    if (Number.isFinite(minutes) && minutes > 0) {
      if (minutes <= 360) return 'five_hours';
      if (minutes <= 2160) return 'day';
      if (minutes <= 14400) return 'week';
      return 'month';
    }
    const name = `${(window && window.name) || ''} ${(window && window.limit_id) || ''}`.toLowerCase();
    if (name.includes('hour') || name.includes('5h')) return 'five_hours';
    if (name.includes('day')) return 'day';
    if (name.includes('week')) return 'week';
    if (name.includes('month')) return 'month';
    return 'other';
  };
  const quotaPeriodLabel = (period) => QUOTA_PERIOD_LABELS[period] || QUOTA_PERIOD_LABELS.other;
  const quotaDuration = (window) => {
    if (window && window.duration_label) return window.duration_label;
    const minutes = Number(window && window.window_minutes);
    if (!Number.isFinite(minutes) || minutes <= 0) return '周期未知';
    if (minutes < 60) return `${Math.round(minutes)} 分钟`;
    if (minutes < 1440) return `${Math.round(minutes / 60)} 小时`;
    if (minutes < 14400) return `${Math.round(minutes / 1440)} 天`;
    return `${Math.round(minutes / 43200)} 个月`;
  };
  const quotaPercentText = (window) => (window.used_percent === null || window.used_percent === undefined
    ? '待采集'
    : formatPercent(window.used_percent));
  const quotaWindowTitle = (window) => `${window.limit_id}/${window.name} · ${quotaPercentText(window)} · ${quotaDuration(window)}`;
  const renderQuotaRow = (period, label, windows) => {
    if (!windows.length) {
      return `<div class="quota-row absent">
        <span class="quota-row-label">${escapeHtml(label)}</span>
        <div class="bar quota-bar"><span style="width:0"></span></div>
        <span class="quota-row-percent absent">不适用</span>
        <span class="quota-row-meta">该订阅没有${escapeHtml(label)}额度</span>
      </div>`;
    }
    // 同一周期有多条窗口（例如 Kimi 的 limit_month_total / limit_month_code）时取最紧的
    // 一条做主行，其余用角标提示，保证「一行一个周期」。
    const ordered = [...windows].sort((left, right) => (
      Number(right.used_percent === null || right.used_percent === undefined ? -1 : right.used_percent)
      - Number(left.used_percent === null || left.used_percent === undefined ? -1 : left.used_percent)
    ));
    const main = ordered[0];
    const rest = ordered.slice(1);
    const unknown = main.used_percent === null || main.used_percent === undefined;
    const percent = unknown ? 0 : Math.max(0, Math.min(100, Number(main.used_percent)));
    const level = main.is_exhausted ? 'danger' : percent >= 80 ? 'warn' : '';
    const stateText = main.is_exhausted ? '已耗尽' : unknown ? '未返回已用比例' : '可用';
    const badge = rest.length
      ? ` <span class="quota-badge" title="${escapeHtml(ordered.map((window) => quotaWindowTitle(window)).join('\n'))}">另有 ${rest.length} 条</span>`
      : '';
    const meta = unknown
      ? `${escapeHtml(stateText)} · ${escapeHtml(quotaDuration(main))}`
      : `重置 ${escapeHtml(formatReset(main.resets_at))} · ${escapeHtml(quotaDuration(main))}`;
    return `<div class="quota-row ${level || 'ok'}">
      <span class="quota-row-label">${escapeHtml(label)}</span>
      <div class="bar quota-bar"><span class="${level}" style="width:${percent}%"></span></div>
      <span class="quota-row-percent ${level}">${escapeHtml(quotaPercentText(main))}</span>
      <span class="quota-row-meta">${meta}${badge}</span>
    </div>`;
  };
  const renderQuotaRows = (quotas) => {
    const windows = (Array.isArray(quotas) ? quotas : []).flatMap((quota) => quota.windows || []);
    if (!windows.length) {
      return '<div class="quota-note">该订阅不提供额度窗口，这里只统计用量</div>';
    }
    const buckets = new Map();
    windows.forEach((window) => {
      const period = quotaPeriod(window);
      if (!buckets.has(period)) buckets.set(period, []);
      buckets.get(period).push(window);
    });
    // 固定三段永远渲染；额外的周期（如「日」或判不出的「其它」）按规范顺序插进去，
    // 宁可多一行也不丢信息。
    const rows = QUOTA_PERIOD_ORDER
      .filter((period) => QUOTA_FIXED_PERIODS.includes(period) || buckets.has(period))
      .map((period) => renderQuotaRow(period, quotaPeriodLabel(period), buckets.get(period) || []));
    return `<div class="quota-rows">${rows.join('')}</div>`;
  };
  // 活动会话整块默认折叠，点击按钮再展开；折叠状态在 5 秒自动刷新之间保持。
  const expandedSessionTables = new Set();
  const renderSessionTable = (sessions, accountKey) => {
    if (!sessions || sessions.length === 0) {
      return '<div class="empty-state">没有活动会话</div>';
    }
    const renderRow = (session) => {
      const active = session.active !== false;
      const status = escapeHtml(statusNames[session.status] || session.status || '未知');
      const cssStatus = active ? statusClass(session.status) : 'other';
      const pids = session.pids && session.pids.length ? session.pids.join(', ') : '无';
      const detail = session.last_error || '—';
      const usage = session.usage || {};
      const turns = usage.turns === undefined || usage.turns === null ? null : Number(usage.turns);
      const advice = usage.reminder
        ? `<div><span class="pill warn">建议开新会话</span></div>`
        : '';
      const usageCell = turns === null
        ? '<span class="muted">—</span>'
        : `${escapeHtml(String(turns))} 轮<div class="muted">上下文 ${escapeHtml(formatDataSize(usage.context_tokens || 0))} · 累计 ${escapeHtml(formatDataSize(usage.total_tokens || 0))}</div>${advice}`;
      const archive = session.archive || {};
      const archiveCell = !session.jsonl_path
        ? '<span class="muted">无 JSONL</span>'
        : archive.eligible
          ? `<button class="btn mini" type="button" data-archive-session="${escapeHtml(session.jsonl_path)}" title="把这个会话压缩归档（tar.gz，可恢复）">归档此会话</button>`
          : `<span class="muted" title="${escapeHtml(archive.reason || '当前不可归档')}">不可归档</span>`;
      return `<tr>
        <td><div class="session-id">${escapeHtml(session.session_id || session.thread_id)}</div><div class="muted">${escapeHtml(session.source)}</div></td>
        <td><span class="pill ${cssStatus}">${status}</span>${active ? '' : '<div class="cell-sub">已结束</div>'}</td>
        <td>${escapeHtml(pids)}<div class="muted">${session.process_backed ? '已绑定 JSONL' : '无进程证据'}</div></td>
        <td class="usage-number">${usageCell}</td>
        <td class="cwd">${escapeHtml(session.cwd || '未知')}</td>
        <td class="event">${escapeHtml(session.last_event_type || '未知')}<div class="muted">${escapeHtml(formatTime(session.last_event_at))}</div></td>
        <td class="error-text">${escapeHtml(detail)}</td>
        <td>${archiveCell}</td>
      </tr>`;
    };
    const expanded = expandedSessionTables.has(accountKey);
    return `<div class="session-collapsible"${expanded ? '' : ' style="display:none"'}><div class="table-wrap session-table"><table>
      <thead><tr><th>会话</th><th>状态</th><th>进程</th><th>轮数 / 上下文</th><th>工作目录</th><th>最近事件</th><th>说明</th><th>归档</th></tr></thead>
      <tbody>${sessions.map(renderRow).join('')}</tbody>
    </table></div></div>
    <button class="session-toggle" type="button" data-session-toggle="${escapeHtml(accountKey)}" aria-expanded="${expanded}">${expanded ? '收起会话列表' : `展开 ${sessions.length} 个会话（含最近结束）`}</button>`;
  };
  let selectedUsagePeriod = 'today';
  let selectedUsageModel = '';
  let selectedUsageProject = '';
  // 用量统计维度：账号 / 模型 / 项目共用同一份叶子聚合，只切换分组方式。
  const usageDimensions = [['account', '按账号'], ['model', '按模型'], ['project', '按项目']];
  const usageDimensionLabels = { account: '按账号', model: '按模型', project: '按项目' };
  let selectedUsageDimension = 'account';
  let selectedUsageAccount = '';
  // 中文注释：一个账号可能用了几十个模型，全部铺开会把行撑得非常高；
  // 默认只列用量最大的前几个，展开状态记录在 Set 里（重新渲染后保持）。
  const USAGE_MODEL_PREVIEW = 3;
  const expandedUsageModelLists = new Set();
  let latestState = null;
  let latestUsageState = null;
  let latestHealthState = null;
  let usagePollTimer = 0;
  let latestInsightsState = null;
  let insightsPollTimer = 0;
  let insightsPeriodDays = '';
  // 低频分区默认折叠：状态记在 localStorage，展开时才加载明细。
  const COLLAPSED_SECTIONS_KEY = 'a-token-monitor-collapsed-sections';
  const DEFAULT_COLLAPSED = ['alert-history', 'usage-search', 'housekeeping'];
  const readCollapsedSections = () => {
    try {
      const stored = window.localStorage.getItem(COLLAPSED_SECTIONS_KEY);
      if (stored === null) return new Set(DEFAULT_COLLAPSED);
      const parsed = JSON.parse(stored);
      return new Set(Array.isArray(parsed) ? parsed : DEFAULT_COLLAPSED);
    } catch (error) {
      return new Set(DEFAULT_COLLAPSED);
    }
  };
  const collapsedSections = readCollapsedSections();
  const loadedSections = new Set();
  const sectionLoaders = {
    'alert-history': () => refreshAlertHistory(),
    'usage-search': () => refreshUsageSearch(),
    'housekeeping': () => refreshHousekeeping()
  };
  const persistCollapsedSections = () => {
    try {
      window.localStorage.setItem(COLLAPSED_SECTIONS_KEY, JSON.stringify([...collapsedSections]));
    } catch (error) {
      /* 隐私模式下忽略存储失败 */
    }
  };
  const applySectionState = (id) => {
    const section = document.getElementById(id);
    const toggle = document.querySelector(`[data-section-toggle="${id}"]`);
    if (!section) return;
    const collapsed = collapsedSections.has(id);
    section.classList.toggle('is-collapsed', collapsed);
    if (toggle) toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  };
  const setSectionCollapsed = (id, collapsed) => {
    if (collapsed) {
      collapsedSections.add(id);
    } else {
      collapsedSections.delete(id);
    }
    applySectionState(id);
    persistCollapsedSections();
    if (!collapsed && sectionLoaders[id] && !loadedSections.has(id)) {
      loadedSections.add(id);
      sectionLoaders[id]();
    }
  };
  const ensureSectionLoaded = (id) => {
    if (!collapsedSections.has(id) && sectionLoaders[id] && !loadedSections.has(id)) {
      loadedSections.add(id);
      sectionLoaders[id]();
    }
  };
  const navLinks = [...document.querySelectorAll('[data-nav-target]')];
  const updateActiveNav = () => {
    const current = ['housekeeping', 'usage-search', 'alert-history', 'insights', 'usage', 'accounts', 'traffic', 'overview'].find((id) => {
      const target = document.getElementById(id);
      return target && target.getBoundingClientRect().top <= 120;
    }) || 'overview';
    navLinks.forEach((link) => link.classList.toggle('active', link.dataset.navTarget === current));
  };
  window.addEventListener('scroll', updateActiveNav, { passive: true });
  updateActiveNav();
  const usagePeriodOrder = ['today', 'seven_days', 'month', 'year'];
  const usageTokenFields = [
    'input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
    'output_tokens', 'reasoning_output_tokens', 'total_tokens'
  ];
  const distinctSorted = (values) => [...new Set(values.filter((value) => value))].sort();
  const sumNullable = (items, key) => {
    const values = items.map((item) => item[key]).filter((value) => value !== null && value !== undefined);
    return values.length ? values.reduce((sum, value) => sum + Number(value), 0) : null;
  };
  const sumUsageModels = (models) => {
    const summary = {};
    usageTokenFields.forEach((field) => {
      summary[field] = models.reduce((sum, model) => sum + Number(model[field] || 0), 0);
    });
    summary.models = models;
    summary.estimated_credits = sumNullable(models, 'estimated_credits');
    summary.estimated_cost_usd = sumNullable(models, 'estimated_cost_usd');
    summary.api_equivalent_cost_usd = sumNullable(models, 'api_equivalent_cost_usd');
    summary.cache_savings_usd = sumNullable(models, 'cache_savings_usd');
    summary.subscription_cost_usd = null;
    summary.unpriced_models = distinctSorted(models.filter((model) => (
      !model.api_pricing_known
    )).map((model) => model.model));
    return summary;
  };
  // 把当前筛选条件（账号 / 模型 / 项目）应用到账号列表上，并把模型级条目
  // 摊平成叶子：按账号、按模型、按项目三种维度都从同一份叶子聚合，
  // 保证任何维度的合计都等于账号合计，不会出现口径不一致。
  const usageLeafModels = (account) => {
    const leaves = [];
    const projects = Array.isArray(account.projects) && account.projects.length
      ? account.projects
      : [{ project: '', models: account.models || [] }];
    projects.forEach((project) => {
      if (selectedUsageProject && (project.project || '') !== selectedUsageProject) return;
      (project.models || []).forEach((model) => {
        if (selectedUsageModel && model.model !== selectedUsageModel) return;
        leaves.push({ model, project: project.project || '' });
      });
    });
    return leaves;
  };
  const usageAccountName = (account) => account.account || account.account_id || '未知账号';
  const usageAccountMatches = (account) => (
    !selectedUsageAccount || usageAccountName(account) === selectedUsageAccount
  );
  const usageGroups = (accounts, dimension) => {
    const groups = new Map();
    (accounts || []).forEach((account) => {
      const name = usageAccountName(account);
      usageLeafModels(account).forEach((leaf) => {
        const key = dimension === 'model'
          ? (leaf.model.model || '未知模型')
          : dimension === 'project'
            ? (leaf.project || '未知项目')
            : name;
        let group = groups.get(key);
        if (!group) {
          group = {
            key,
            models: [],
            accounts: new Map(),
            profiles: new Set(),
            products: new Set(),
            accountId: account.account_id || '',
          };
          groups.set(key, group);
        }
        if (!group.accountId && account.account_id) group.accountId = account.account_id;
        group.models.push(leaf.model);
        group.accounts.set(name, (group.accounts.get(name) || 0) + Number(leaf.model.total_tokens || 0));
        (account.profiles || []).forEach((profile) => group.profiles.add(profile));
        (account.products || []).forEach((product) => group.products.add(product));
      });
    });
    return [...groups.values()].map((group) => ({
      ...group,
      stats: sumUsageModels(group.models),
      accountList: [...group.accounts.entries()].sort((a, b) => b[1] - a[1]).map((item) => item[0]),
    }));
  };
  const usageTagLine = (values, fallback) => {
    const items = [...values].filter((item) => item);
    return items.length ? items.join(' · ') : fallback;
  };
  const usageAccountSubLabel = (group) => usageTagLine(
    group.products,
    usageTagLine(group.profiles, '未知 profile'),
  );
  // 中文注释：产品展示名与「账号与额度」卡片共用同一套映射（Codex / Grok / Kimi /
  // DeepSeek Harness / Command Code / Claude Code），避免两处叫法不一致。
  const accountProductLabel = (account) => {
    const profiles = (account && account.profiles) || [];
    const matches = (id) => account && (account.product === id || profiles.some((profile) => (typeof profile === 'string' ? profile : profile.name) === id));
    return matches('kimi') ? 'Kimi'
      : matches('grok') ? 'Grok'
        : matches('dsh') ? 'DeepSeek Harness'
          : matches('command-code') ? 'Command Code'
            : matches('claude') ? 'Claude Code'
              : 'Codex';
  };
  // 账号 → 产品 · 套餐：套餐取自 /api/state（与卡片同源、同一套展示名归一化）。
  const usageAccountSubscriptions = () => {
    const map = new Map();
    const accounts = (latestState && Array.isArray(latestState.accounts)) ? latestState.accounts : [];
    const quotas = (latestState && Array.isArray(latestState.quotas)) ? latestState.quotas : [];
    accounts.forEach((account) => {
      const plans = distinctSorted(
        quotas
          .filter((quota) => (quota.account || 'codex') === account.name)
          .map((quota) => planLabel(quota.plan_type))
          .concat([planLabel(account.plan_type)]),
      );
      map.set(account.name, { product: accountProductLabel(account), plan: plans.join(' / ') });
    });
    return map;
  };
  // 账号成本排行的标签：`Codex · Plus/账号 ID`，与卡片标题同源。
  const usageAccountRankingLabel = (group, subscriptions) => {
    const meta = (subscriptions || usageAccountSubscriptions()).get(group.key) || {};
    const product = meta.product || usageTagLine(group.products, '未知产品');
    const subscription = meta.plan ? `${product} · ${meta.plan}` : product;
    const accountId = group.accountId || group.key;
    const suffix = accountId && accountId.toLowerCase() !== product.toLowerCase()
      ? `<span class="muted">/${escapeHtml(accountId)}</span>`
      : '';
    return `${escapeHtml(subscription)}${suffix}`;
  };
  const usageContributorLabel = (group) => {
    const names = group.accountList || [];
    if (!names.length) return '';
    return names.length === 1 ? names[0] : `${names[0]} 等 ${names.length} 个账号`;
  };
  const usageSortByCost = (groups) => [...groups].sort((a, b) => {
    const left = Number(a.stats.estimated_cost_usd || 0);
    const right = Number(b.stats.estimated_cost_usd || 0);
    if (left !== right) return right - left;
    return Number(b.stats.total_tokens || 0) - Number(a.stats.total_tokens || 0);
  });
  const renderUsageRanking = (title, hint, groups, labelOf) => {
    const ranked = usageSortByCost(groups).filter((group) => Number(group.stats.estimated_cost_usd || 0) > 0);
    if (!ranked.length) return '';
    const total = ranked.reduce((sum, group) => sum + Number(group.stats.estimated_cost_usd || 0), 0);
    const max = Number(ranked[0].stats.estimated_cost_usd) || 1;
    const top = ranked.slice(0, 5);
    const items = top.map((group) => {
      const cost = Number(group.stats.estimated_cost_usd || 0);
      const share = total > 0 ? (cost / total) * 100 : 0;
      const width = Math.max(3, Math.round((cost / max) * 100));
      return `<div class="top-project"><div class="top-project-row"><span class="top-project-label">${labelOf(group)}</span><span class="top-project-value">${escapeHtml(formatUsdCompact(cost))} · ${share.toFixed(1)}%</span></div><div class="bar"><span style="width:${width}%"></span></div></div>`;
    }).join('');
    return `<div class="usage-top-projects"><div class="usage-trend-head"><span>${escapeHtml(title)} Top ${top.length}</span><span class="muted">${escapeHtml(hint)}</span></div>${items}</div>`;
  };
  const usageFilterControls = (periods) => {
    const models = distinctSorted(periods.flatMap((period) => (period.accounts || []).flatMap((account) => (account.models || []).map((model) => model.model))));
    const projects = distinctSorted(periods.flatMap((period) => (period.accounts || []).flatMap((account) => (account.projects || []).map((project) => project.project))));
    const accounts = distinctSorted(periods.flatMap((period) => (period.accounts || []).map((account) => account.account || account.account_id)));
    if (!models.includes(selectedUsageModel)) selectedUsageModel = '';
    if (!projects.includes(selectedUsageProject)) selectedUsageProject = '';
    if (!accounts.includes(selectedUsageAccount)) selectedUsageAccount = '';
    const modelOptions = [`<option value="">全部模型</option>`, ...models.map((model) => `<option value="${escapeHtml(model)}"${model === selectedUsageModel ? ' selected' : ''}>${escapeHtml(model)}</option>`)].join('');
    const projectOptions = [`<option value="">全部项目</option>`, ...projects.map((project) => `<option value="${escapeHtml(project)}"${project === selectedUsageProject ? ' selected' : ''}>${escapeHtml(project)}</option>`)].join('');
    const accountOptions = [`<option value="">全部账号</option>`, ...accounts.map((account) => `<option value="${escapeHtml(account)}"${account === selectedUsageAccount ? ' selected' : ''}>${escapeHtml(account)}</option>`)].join('');
    return `<div class="usage-filters">
      <label class="usage-filter">账号<select id="usage-account-filter">${accountOptions}</select></label>
      <label class="usage-filter">模型<select id="usage-model-filter">${modelOptions}</select></label>
      <label class="usage-filter">项目 / 工作目录<select id="usage-project-filter">${projectOptions}</select></label>
    </div>`;
  };
  const updateSpendFromUsage = (usage) => {
    if (!usage || (usage.indexing && usage.indexing.complete === false)) {
      document.getElementById('spend-count').textContent = '索引中';
      return;
    }
    const periods = usage && Array.isArray(usage.periods) ? usage.periods : [];
    const today = periods.find((period) => period.key === 'today');
    const todayCost = today ? sumNullable(today.accounts || [], 'estimated_cost_usd') : null;
    document.getElementById('spend-count').textContent = formatUsdCompact(todayCost);
  };
  const monthCost = (periods) => {
    const month = (periods || []).find((period) => period.key === 'month');
    return month ? sumNullable(month.accounts || [], 'estimated_cost_usd') : null;
  };
  const budgetUsd = () => {
    if (!latestState || latestState.budget_usd === null || latestState.budget_usd === undefined) return null;
    const value = Number(latestState.budget_usd);
    return value > 0 ? value : null;
  };
  // 配额耗尽 / 超 90% 与预算超 80% 的告警横幅，随 5 秒状态轮询刷新。
  // 折叠状态下也能看到关键结论，不必展开分区。
  const renderSectionSummaries = (state) => {
    const alertHistory = (state && state.alert_history) || {};
    const alertSummary = document.getElementById('alert-history-summary');
    if (alertSummary) {
      alertSummary.textContent = alertHistory.available
        ? `未读 ${formatNumber(alertHistory.unread)} 条 · 共 ${formatNumber(alertHistory.total)} 条 · 最近 ${formatTime(alertHistory.last_alert_at)}`
        : '告警历史暂不可用（监控进程未启用落盘）';
    }
    const usageIndex = (state && state.usage_index) || {};
    const usageSummary = document.getElementById('usage-search-summary');
    if (usageSummary) {
      usageSummary.textContent = usageIndex.available
        ? `索引 ${formatNumber(usageIndex.records)} 条记录 / ${formatNumber(usageIndex.sessions)} 个会话 · ${formatNumber(usageIndex.models)} 个模型 · ${formatNumber(usageIndex.accounts)} 个账号 · ${formatDay(usageIndex.first_at)} ~ ${formatDay(usageIndex.last_at)}`
        : '用量索引还是空的，先让 daemon 完成一次索引';
    }
    const housekeeping = (state && state.housekeeping) || {};
    const housekeepingSummary = document.getElementById('housekeeping-summary');
    if (housekeepingSummary) {
      const totals = housekeeping.totals || {};
      const preview = housekeeping.preview || {};
      housekeepingSummary.textContent = housekeeping.available
        ? `合计 ${formatDataSize(totals.bytes || 0)} · 会话文件 ${formatDataSize(totals.session_bytes || 0)}（${formatNumber(totals.session_files || 0)} 个）· ${formatNumber(preview.count || 0)} 个可归档 · ${formatNumber((housekeeping.reminders || []).length)} 条磁盘提醒`
        : '磁盘统计不可用（监控进程未启动扫描）';
    }
  };
  // 顶部关注区：把额度、流量、预算、磁盘和长会话提醒收敛成可跳转的紧凑行。
  const ALERT_VISIBLE_ROWS = 3;
  let expandedAlerts = false;
  const renderAlerts = () => {
    const container = document.getElementById('alert-list');
    if (!container) return;
    const alerts = [];
    const quotas = latestState && Array.isArray(latestState.quotas) ? latestState.quotas : [];
    quotas.forEach((quota) => {
      const account = quota.account || 'codex';
      (quota.windows || []).forEach((window) => {
        const label = `${account} · ${window.limit_id}/${window.name}`;
        const percent = Number(window.used_percent);
        if (window.is_exhausted) {
          alerts.push({
            level: 'danger',
            title: `${label} 额度已耗尽`,
            detail: `重置时间 ${formatReset(window.resets_at)}`,
            href: '#accounts',
            link: '查看额度'
          });
        } else if (window.used_percent !== null && window.used_percent !== undefined && !Number.isNaN(percent) && percent >= 90) {
          alerts.push({
            level: 'warn',
            title: `${label} 已使用 ${percent.toFixed(1)}%`,
            detail: '接近额度上限，注意剩余用量',
            href: '#accounts',
            link: '查看额度'
          });
        }
      });
    });
    const traffic = (latestState && latestState.traffic) || {};
    (traffic.alerts || []).forEach((alert) => {
      if (!alert || !alert.message) return;
      alerts.push({
        level: alert.level === 'danger' ? 'danger' : 'warn',
        title: alert.message,
        detail: `${alert.kind === 'burst' ? '突发窗口' : '累计窗口'} · 对端 ${alert.remote || '未知'}`,
        href: '#alert-history',
        link: '查看告警历史'
      });
    });
    const budget = budgetUsd();
    if (budget !== null && latestUsageState && latestUsageState.usage) {
      const spent = monthCost(latestUsageState.usage.periods);
      if (spent !== null) {
        const ratio = (spent / budget) * 100;
        if (ratio >= 100) {
          alerts.push({ level: 'danger', title: `本月 API 等价金额 ${formatUsdCompact(spent)} 已超预算`, detail: `月预算 ${formatUsdCompact(budget)}（${ratio.toFixed(0)}%）`, href: '#usage', link: '查看用量' });
        } else if (ratio >= 80) {
          alerts.push({ level: 'warn', title: `本月 API 等价金额 ${formatUsdCompact(spent)} 已达预算 ${ratio.toFixed(0)}%`, detail: `月预算 ${formatUsdCompact(budget)}`, href: '#usage', link: '查看用量' });
        }
      }
    }
    const housekeeping = (latestState && latestState.housekeeping) || {};
    (housekeeping.reminders || []).forEach((reminder) => {
      alerts.push({
        level: reminder.level === 'danger' ? 'danger' : 'warn',
        title: reminder.title || reminder.message,
        detail: reminder.detail || '',
        href: '#housekeeping',
        link: '查看磁盘与会话管理'
      });
    });
    const sessionAdvice = (latestState && latestState.session_advice) || {};
    (sessionAdvice.sessions || []).forEach((reminder) => {
      alerts.push({
        level: 'warn',
        title: reminder.title || reminder.message,
        detail: reminder.detail || '',
        href: '#accounts',
        link: '查看会话'
      });
    });
    if (alerts.length === 0) {
      container.innerHTML = '';
      return;
    }
    const visible = expandedAlerts ? alerts : alerts.slice(0, ALERT_VISIBLE_ROWS);
    const rows = visible.map((alert) => `<div class="alert-row ${alert.level}" role="status">
        <span class="alert-accent" aria-hidden="true"></span>
        <div class="alert-body">
          <div class="alert-title">${escapeHtml(alert.title)}</div>
          ${alert.detail ? `<div class="alert-detail">${escapeHtml(alert.detail)}</div>` : ''}
        </div>
        ${alert.href ? `<a class="alert-link" href="${escapeHtml(alert.href)}">${escapeHtml(alert.link || '查看')}</a>` : ''}
      </div>`).join('');
    const rest = alerts.length - visible.length;
    const toggle = alerts.length > ALERT_VISIBLE_ROWS
      ? `<button id="alert-toggle-button" class="btn mini" type="button">${rest > 0 ? `展开其余 ${rest} 条` : '收起'}</button>`
      : '';
    container.innerHTML = `<div class="alert-head"><span>需要关注</span><span class="section-count">${alerts.length} 条</span><span class="alert-head-spacer"></span>${toggle}</div>${rows}`;
    document.getElementById('alert-toggle-button')?.addEventListener('click', () => {
      expandedAlerts = !expandedAlerts;
      renderAlerts();
    });
  };
  const renderTrend = (daily) => {
    if (!Array.isArray(daily) || !daily.length) return '';
    const costs = daily.map((day) => Number(day && day.estimated_cost_usd) || 0);
    const maxCost = costs.reduce((max, value) => Math.max(max, value), 0);
    const total = costs.reduce((sum, value) => sum + value, 0);
    const todayKey = new Date().toLocaleDateString('sv-SE');
    const bars = daily.map((day, index) => {
      const cost = costs[index];
      const height = maxCost > 0 ? Math.max(3, Math.round((cost / maxCost) * 100)) : 3;
      const classes = ['trend-bar'];
      if (day.date === todayKey) classes.push('today');
      if (cost <= 0) classes.push('empty');
      const unpriced = day.has_unpriced ? '（含未定价模型）' : '';
      const title = `${day.date} · ${formatUsdCompact(day.estimated_cost_usd)} · ${formatNumber(day.total_tokens)} tokens${unpriced}`;
      return `<div class="${classes.join(' ')}" style="height:${height}%" title="${escapeHtml(title)}"></div>`;
    }).join('');
    return `<div class="usage-trend"><div class="usage-trend-head"><span>近 ${daily.length} 天 API 等价金额趋势</span><span class="muted">合计 ${escapeHtml(formatUsdCompact(total))}</span></div><div class="trend-chart">${bars}</div></div>`;
  };
  // Kimi booster 钱包返回的是真实扣费（分），与本地按公开 API 价的估算并排展示。
  const renderKimiReconciliation = (periods) => {
    if (!latestState || !Array.isArray(latestState.quotas)) return '';
    const month = (periods || []).find((period) => period.key === 'month');
    const monthAccounts = month && Array.isArray(month.accounts) ? month.accounts : [];
    const lines = latestState.quotas.filter((quota) => quota.product === 'kimi').map((quota) => {
      const meta = quota.metadata || {};
      if (meta.booster_monthly_used_cents === undefined) return '';
      const symbol = meta.booster_currency === 'CNY' ? '¥' : '$';
      const formatMoney = (cents) => `${symbol}${(Number(cents || 0) / 100).toFixed(2)}`;
      const limitEnabled = meta.booster_monthly_charge_limit_enabled === 'true' && Number(meta.booster_monthly_charge_limit_cents || 0) > 0;
      const limitText = limitEnabled ? `（限额 ${formatMoney(meta.booster_monthly_charge_limit_cents)}）` : '';
      const accountUsage = monthAccounts.find((account) => account.account === quota.account);
      const estimate = accountUsage && accountUsage.estimated_cost_usd !== null && accountUsage.estimated_cost_usd !== undefined
        ? ` · 本地 API 等价估算 ${formatUsdCompact(accountUsage.estimated_cost_usd)}`
        : '';
      return `<div class="usage-note">Kimi 对账 · ${escapeHtml(quota.account || 'kimi')}：本月 booster 真实扣费 ${escapeHtml(formatMoney(meta.booster_monthly_used_cents))}${escapeHtml(limitText)}，余额 ${escapeHtml(formatMoney(meta.booster_balance_cents))}${escapeHtml(estimate)}</div>`;
    }).filter((line) => line);
    return lines.join('');
  };
  // Command Code 订阅返回真实名额扣费（美元），与本地 API 等价估算并排展示。
  const renderCommandCodeReconciliation = (periods) => {
    if (!latestState || !Array.isArray(latestState.quotas)) return '';
    const month = (periods || []).find((period) => period.key === 'month');
    const monthAccounts = month && Array.isArray(month.accounts) ? month.accounts : [];
    const lines = latestState.quotas.filter((quota) => quota.product === 'command-code').map((quota) => {
      const meta = quota.metadata || {};
      if (meta.period_credits_spent === undefined) return '';
      const parts = [`本月订阅扣费 $${meta.period_credits_spent}`, `剩余名额 $${meta.monthly_credits_remaining || '0.00'}`];
      if (meta.period_requests !== undefined) parts.push(`${meta.period_requests} 次请求`);
      if (meta.days_left !== undefined) parts.push(`${meta.days_left} 天后重置`);
      const accountUsage = monthAccounts.find((account) => account.account === quota.account);
      const estimate = accountUsage && accountUsage.estimated_cost_usd !== null && accountUsage.estimated_cost_usd !== undefined
        ? ` · 本地 API 等价估算 ${formatUsdCompact(accountUsage.estimated_cost_usd)}`
        : '';
      return `<div class="usage-note">Command Code 对账 · ${escapeHtml(quota.account || 'command-code')}：${escapeHtml(parts.join('，'))}${escapeHtml(estimate)}</div>`;
    }).filter((line) => line);
    return lines.join('');
  };
  const renderUsage = (state) => {
    const container = document.getElementById('usage-content');
    const usage = state.usage || {};
    const periods = Array.isArray(usage.periods) ? usage.periods : [];
    const indexing = usage.indexing || {};
    const duplicateFiles = Number(indexing.deduplicated_files || 0);
    const duplicateNote = duplicateFiles > 0
      ? `已忽略 ${duplicateFiles} 份重复会话（${formatBytes(indexing.deduplicated_bytes)}）。`
      : '';
    if (indexing.complete === false) {
      const percent = Number(indexing.percent || 0).toFixed(2);
      const remainingBytes = Math.max(0, Number(indexing.total_bytes || 0) - Number(indexing.indexed_bytes || 0));
      const bytesPerSec = Number(indexing.bytes_per_sec || 0);
      const etaSeconds = bytesPerSec > 0 ? Math.ceil(remainingBytes / bytesPerSec) : 0;
      const eta = etaSeconds > 0 ? `预计剩余约 ${etaSeconds} 秒。` : '正在后台继续索引。';
      container.innerHTML = `<div class="empty-state"><strong>历史索引进行中（${escapeHtml(percent)}%）</strong><br>已确认 ${escapeHtml(formatBytes(indexing.indexed_bytes))} / ${escapeHtml(formatBytes(indexing.total_bytes))}。完成前不显示 token 和金额。待处理 ${escapeHtml(indexing.pending_files ?? 0)} 个文件。${escapeHtml(duplicateNote)}${escapeHtml(eta)} 页面会自动更新，无需连点刷新。</div>`;
      return;
    }
    if (periods.length === 0) {
      container.innerHTML = '<div class="empty-state">暂无可读取的 session JSONL 用量记录</div>';
      return;
    }
    const period = periods.find((item) => item.key === selectedUsagePeriod) || periods[0];
    selectedUsagePeriod = period.key;
    const tabs = usagePeriodOrder.map((key) => {
      const item = periods.find((candidate) => candidate.key === key);
      if (!item) return '';
      const selected = item.key === selectedUsagePeriod ? ' selected' : '';
      const pressed = item.key === selectedUsagePeriod ? 'true' : 'false';
      return `<button class="usage-tab${selected}" type="button" aria-pressed="${pressed}" data-usage-period="${escapeHtml(item.key)}">${escapeHtml(item.label)}</button>`;
    }).join('');
    const accounts = Array.isArray(period.accounts) ? period.accounts : [];
    const filterControls = usageFilterControls(periods);
    const note = usage.pricing && usage.pricing.note
      ? usage.pricing.note
      : '金额为估算值，不代表 Plus 实际扣款。';
    const indexingNote = duplicateNote ? `${duplicateNote} ` : '';
    const filteredAccounts = accounts.filter(usageAccountMatches);
    const dimensionTabs = usageDimensions.map(([key, label]) => {
      const selected = key === selectedUsageDimension;
      return `<button class="usage-tab${selected ? ' selected' : ''}" type="button" aria-pressed="${selected ? 'true' : 'false'}" data-usage-dimension="${escapeHtml(key)}">${escapeHtml(label)}</button>`;
    }).join('');
    // 三种维度共用同一份叶子聚合结果：合计始终等于账号合计，切换维度不会变化。
    const groups = usageSortByCost(usageGroups(filteredAccounts, selectedUsageDimension));
    const statsList = groups.map((group) => group.stats);
    const totalTokens = statsList.reduce((sum, stats) => sum + Number(stats.total_tokens || 0), 0);
    const totalCredits = sumNullable(statsList, 'estimated_credits');
    const totalUsd = sumNullable(statsList, 'estimated_cost_usd');
    const totalCacheSavings = sumNullable(statsList, 'cache_savings_usd');
    const savingsItem = totalCacheSavings !== null && totalCacheSavings > 0
      ? `<div class="usage-summary-item"><div class="usage-summary-label">缓存节省（等价）</div><div class="usage-summary-value" title="${escapeHtml(formatUsd(totalCacheSavings))}">≈ ${escapeHtml(formatUsdSummary(totalCacheSavings))}</div></div>`
      : '';
    const budget = budgetUsd();
    let budgetItem = '';
    if (budget !== null) {
      const monthSpent = monthCost(periods);
      const ratio = monthSpent !== null ? (monthSpent / budget) * 100 : 0;
      const level = ratio >= 100 ? 'danger' : ratio >= 80 ? 'warn' : '';
      budgetItem = `<div class="usage-summary-item"><div class="usage-summary-label">本月预算（已用 ${escapeHtml(ratio.toFixed(0))}%）</div><div class="usage-summary-value">${escapeHtml(formatUsdCompact(monthSpent))} / ${escapeHtml(formatUsdCompact(budget))}</div><div class="bar budget-bar"><span class="${level}" style="width:${Math.min(100, Math.max(0, ratio))}%"></span></div></div>`;
    }
    const summary = `<div class="usage-summary">
      <div class="usage-summary-item"><div class="usage-summary-label">匹配账号</div><div class="usage-summary-value">${formatNumber(filteredAccounts.length)}</div></div>
      <div class="usage-summary-item"><div class="usage-summary-label">总 token</div><div class="usage-summary-value" title="${escapeHtml(formatNumber(totalTokens))}">${formatNumber(totalTokens)}</div></div>
      <div class="usage-summary-item"><div class="usage-summary-label">API 等价金额</div><div class="usage-summary-value" title="${escapeHtml(formatUsd(totalUsd))}">${escapeHtml(formatUsdSummary(totalUsd))}</div></div>
      ${savingsItem}${budgetItem}
    </div>`;
    const dimensionHeaders = {
      account: '<tr><th>订阅 / 账号 ID</th><th>调用模型</th><th>输入 token</th><th>缓存输入</th><th>缓存写入</th><th>输出 token</th><th>推理输出</th><th>总 token</th><th>Plus credits（不可反推）</th><th>API 等价金额</th><th>占比</th></tr>',
      model: '<tr><th>模型</th><th>使用账号</th><th>输入 token</th><th>缓存输入</th><th>缓存写入</th><th>输出 token</th><th>推理输出</th><th>总 token</th><th>Plus credits（不可反推）</th><th>API 等价金额</th><th>占比</th></tr>',
      project: '<tr><th>项目 / 工作目录</th><th>账号</th><th>输入 token</th><th>缓存输入</th><th>缓存写入</th><th>输出 token</th><th>推理输出</th><th>总 token</th><th>Plus credits（不可反推）</th><th>API 等价金额</th><th>占比</th></tr>',
    };
    const shareText = (stats) => {
      if (totalUsd === null || totalUsd <= 0) return '—';
      const cost = stats.estimated_cost_usd;
      if (cost === null || cost === undefined) return '—';
      return `${((Number(cost) / totalUsd) * 100).toFixed(1)}%`;
    };
    const groupsByAccount = usageGroups(filteredAccounts, 'account');
    const accountGroupByKey = new Map(groupsByAccount.map((group) => [group.key, group]));
    // 订阅（产品 · 套餐）从 /api/state 取一次，表格与排行共用同一份标签。
    const accountSubscriptions = usageAccountSubscriptions();
    const accountLabel = (group) => usageAccountRankingLabel(group, accountSubscriptions);
    const rows = groups.map((group) => {
      const stats = group.stats;
      const accountGroup = accountGroupByKey.get(group.key) || group;
      // 模型按 token 用量倒序，折叠时优先露出来的就是大头。
      const modelItems = (stats.models || []).slice().sort((left, right) => Number(right.total_tokens || 0) - Number(left.total_tokens || 0));
      const modelsExpanded = expandedUsageModelLists.has(group.key);
      const visibleModels = modelsExpanded ? modelItems : modelItems.slice(0, USAGE_MODEL_PREVIEW);
      const hiddenModels = modelItems.length - visibleModels.length;
      const models = visibleModels.map((model) => `<div><span class="usage-model">${escapeHtml(model.model)}</span> · ${formatNumber(model.total_tokens)} tokens</div>`).join('');
      const modelsToggle = modelItems.length > USAGE_MODEL_PREVIEW
        ? `<button class="chip-button usage-models-toggle" type="button" data-usage-models="${escapeHtml(group.key)}" aria-expanded="${modelsExpanded ? 'true' : 'false'}">${modelsExpanded ? '收起模型' : `另有 ${hiddenModels} 个模型`}</button>`
        : '';
      const profiles = usageTagLine(accountGroup.profiles, '未知 profile');
      const pricingWarning = stats.unpriced_models && stats.unpriced_models.length
        ? `<div class="muted">未定价（未计入金额）：${escapeHtml(stats.unpriced_models.join(', '))}</div>`
        : '';
      let labelCell;
      if (selectedUsageDimension === 'model') {
        labelCell = `<td><div>${escapeHtml(group.key)}</div><div class="muted">${escapeHtml(usageContributorLabel(group) || '未知账号')}</div></td>`;
      } else if (selectedUsageDimension === 'project') {
        labelCell = `<td><div>${escapeHtml(group.key)}</div><div class="muted">${escapeHtml(usageContributorLabel(group) || '未知账号')}</div></td>`;
      } else {
        // 中文注释：与账号成本排行同一份标签（产品 · 套餐/账号 ID），
        // 次要行保留 profile，用来区分同一账号下的多个登录目录。
        labelCell = `<td><div>${accountLabel(group)}</div><div class="muted">Profile：${escapeHtml(profiles)}</div></td>`;
      }
      const detailCell = selectedUsageDimension === 'account'
        ? `<td class="usage-models">${models || '<span class="muted">暂无模型</span>'}${modelsToggle}${pricingWarning}</td>`
        : `<td class="usage-models">${group.accountList.slice(0, 3).map((name) => `<div>${escapeHtml(name)}</div>`).join('')}${group.accountList.length > 3 ? `<div class="muted">等 ${group.accountList.length} 个账号</div>` : ''}${pricingWarning}</td>`;
      return `<tr>
        ${labelCell}
        ${detailCell}
        <td class="usage-number">${formatNumber(stats.input_tokens)}</td>
        <td class="usage-number">${formatNumber(stats.cached_input_tokens)}</td>
        <td class="usage-number">${formatNumber(stats.cache_write_input_tokens)}</td>
        <td class="usage-number">${formatNumber(stats.output_tokens)}</td>
        <td class="usage-number">${formatNumber(stats.reasoning_output_tokens)}</td>
        <td class="usage-number">${formatNumber(stats.total_tokens)}</td>
        <td class="usage-number">${formatCredits(stats.estimated_credits)}</td>
        <td class="usage-number">${formatUsd(stats.estimated_cost_usd)}</td>
        <td class="usage-number">${escapeHtml(shareText(stats))}</td>
      </tr>`;
    }).join('');
    const totalsRow = groups.length > 1
      ? `<tfoot><tr>
        <td>合计（${formatNumber(groups.length)} 组）</td>
        <td>${formatNumber(filteredAccounts.length)} 个账号</td>
        <td class="usage-number">${formatNumber(statsList.reduce((sum, stats) => sum + Number(stats.input_tokens || 0), 0))}</td>
        <td class="usage-number">${formatNumber(statsList.reduce((sum, stats) => sum + Number(stats.cached_input_tokens || 0), 0))}</td>
        <td class="usage-number">${formatNumber(statsList.reduce((sum, stats) => sum + Number(stats.cache_write_input_tokens || 0), 0))}</td>
        <td class="usage-number">${formatNumber(statsList.reduce((sum, stats) => sum + Number(stats.output_tokens || 0), 0))}</td>
        <td class="usage-number">${formatNumber(statsList.reduce((sum, stats) => sum + Number(stats.reasoning_output_tokens || 0), 0))}</td>
        <td class="usage-number">${formatNumber(totalTokens)}</td>
        <td class="usage-number">${formatCredits(totalCredits)}</td>
        <td class="usage-number">${formatUsd(totalUsd)}</td>
        <td class="usage-number">${totalUsd && totalUsd > 0 ? '100.0%' : '—'}</td>
      </tr></tfoot>`
      : '';
    const accountRanking = renderUsageRanking(
      '账号成本排行',
      '当前筛选 · 占比为筛选内合计',
      groupsByAccount,
      accountLabel,
    );
    const projectLabel = (group) => {
      const sub = usageContributorLabel(group);
      return `${escapeHtml(group.key)}${sub ? `<span class="muted"> · ${escapeHtml(sub)}</span>` : ''}`;
    };
    const projectRanking = renderUsageRanking(
      '项目成本排行',
      '当前筛选 · 按 API 等价金额',
      usageGroups(filteredAccounts, 'project'),
      projectLabel,
    );
    // 中文注释：两个排行并排两列；只有一个有数据时（例如全是未定价模型）
    // 让剩下那个占满整行，避免半张空栏。
    const rankingBlocks = [accountRanking, projectRanking].filter(Boolean);
    const rankings = rankingBlocks.length
      ? `<div class="usage-rankings${rankingBlocks.length === 1 ? ' single' : ''}">${rankingBlocks.join('')}</div>`
      : '';
    container.innerHTML = `${renderTrend(usage.daily)}
      <div class="usage-tabs">${tabs}<span class="usage-tabs-divider" aria-hidden="true"></span>${dimensionTabs}</div>
      ${filterControls}${summary}${rankings}<div class="usage-note">${indexingNote}${escapeHtml(note)} 统计维度：${escapeHtml(usageDimensionLabels[selectedUsageDimension] || '按账号')} · 时间范围：${escapeHtml(formatTime(period.start_at))} 至 ${escapeHtml(formatTime(period.end_at))} · credits：${escapeHtml(formatCredits(totalCredits))}</div>${renderKimiReconciliation(periods)}${renderCommandCodeReconciliation(periods)}
      <div class="table-wrap usage-table"><table>
        <thead>${dimensionHeaders[selectedUsageDimension] || dimensionHeaders.account}</thead>
        <tbody>${rows || '<tr><td colspan="11" class="empty-state">这个时间范围没有匹配的账号、模型或项目</td></tr>'}</tbody>
        ${totalsRow}
      </table></div>`;
    container.querySelectorAll('[data-usage-period]').forEach((button) => {
      button.addEventListener('click', () => {
        selectedUsagePeriod = button.dataset.usagePeriod || 'today';
        renderUsage(state);
      });
    });
    container.querySelectorAll('[data-usage-models]').forEach((button) => {
      button.addEventListener('click', () => {
        const key = button.dataset.usageModels || '';
        if (expandedUsageModelLists.has(key)) {
          expandedUsageModelLists.delete(key);
        } else {
          expandedUsageModelLists.add(key);
        }
        renderUsage(state);
      });
    });
    container.querySelectorAll('[data-usage-dimension]').forEach((button) => {
      button.addEventListener('click', () => {
        selectedUsageDimension = button.dataset.usageDimension || 'account';
        renderUsage(state);
      });
    });
    const accountFilter = document.getElementById('usage-account-filter');
    const modelFilter = document.getElementById('usage-model-filter');
    const projectFilter = document.getElementById('usage-project-filter');
    if (accountFilter) {
      accountFilter.addEventListener('change', () => {
        selectedUsageAccount = accountFilter.value;
        renderUsage(state);
      });
    }
    if (modelFilter) {
      modelFilter.addEventListener('change', () => {
        selectedUsageModel = modelFilter.value;
        renderUsage(state);
      });
    }
    if (projectFilter) {
      projectFilter.addEventListener('change', () => {
        selectedUsageProject = projectFilter.value;
        renderUsage(state);
      });
    }
  };
  const refreshUsage = async () => {
    const button = document.getElementById('usage-load-button');
    const container = document.getElementById('usage-content');
    if (usagePollTimer) {
      window.clearTimeout(usagePollTimer);
      usagePollTimer = 0;
    }
    if (button) {
      button.disabled = true;
      button.textContent = '正在统计…';
    }
    if (!latestUsageState) {
      container.innerHTML = '<div class="empty-state">正在启动后台用量索引…</div>';
    }
    const poll = async () => {
      try {
        const response = await fetch('/api/usage', { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        latestUsageState = { usage: payload.usage || {} };
        renderUsage(latestUsageState);
        updateSpendFromUsage(latestUsageState.usage);
        renderAlerts();
        const indexing = latestUsageState.usage.indexing || {};
        if (indexing.complete === false) {
          if (button) {
            button.textContent = `索引中 ${Number(indexing.percent || 0).toFixed(1)}%`;
          }
          usagePollTimer = window.setTimeout(poll, 1000);
          return;
        }
        if (button) {
          button.disabled = false;
          button.textContent = '刷新用量';
        }
      } catch (error) {
        container.innerHTML = `<div class="error" style="display:block">读取用量失败：${escapeHtml(error.message)}</div>`;
        if (button) {
          button.disabled = false;
          button.textContent = '刷新用量';
        }
      }
    };
    await poll();
  };
  const renderInsights = (payload) => {
    const container = document.getElementById('insights-content');
    const insights = (payload && payload.insights) || {};
    if (insights.ready !== true) {
      const indexing = insights.indexing || {};
      if (indexing.complete === false) {
        const percent = Number(indexing.percent || 0).toFixed(2);
        container.innerHTML = `<div class="empty-state"><strong>用量索引进行中（${escapeHtml(percent)}%）</strong><br>索引完成后才能生成习惯分析，页面会自动更新。</div>`;
      } else {
        container.innerHTML = '<div class="empty-state">用量索引尚未建立，请先在「用量与费用」区加载用量。</div>';
      }
      return;
    }
    if (insights.insufficient) {
      container.innerHTML = `<div class="empty-state">对话数量不足（当前 ${formatNumber(insights.conversation_count || 0)} 个，至少需要 3 个），再积累一些使用后再来分析。</div>`;
      return;
    }
    const hitRate = insights.cache_hit_rate;
    const windowDays = Number(insights.window_days || 0);
    // 中文注释：今天按本地日历日（后端 days=today 用同一个起点），与用量区一致。
    const windowLabel = insights.window_kind === 'today'
      ? '今天'
      : windowDays > 0 ? `近 ${windowDays} 天` : '全部历史';
    const summary = `<div class="usage-summary">
      <div class="usage-summary-item"><div class="usage-summary-label">分析对话数 · ${escapeHtml(windowLabel)}</div><div class="usage-summary-value">${formatNumber(insights.conversation_count)}</div></div>
      <div class="usage-summary-item"><div class="usage-summary-label">总 token</div><div class="usage-summary-value">${formatNumber(insights.total_tokens)}</div></div>
      <div class="usage-summary-item"><div class="usage-summary-label">整体缓存命中率</div><div class="usage-summary-value">${hitRate === null || hitRate === undefined ? '未知' : `${Number(hitRate).toFixed(1)}%`}</div></div>
      <div class="usage-summary-item"><div class="usage-summary-label">预计可节省</div><div class="usage-summary-value">${escapeHtml(formatUsdCompact(insights.potential_savings_usd))}</div></div>
    </div>`;
    const hours = Array.isArray(insights.hour_histogram) ? insights.hour_histogram : [];
    const maxHour = hours.reduce((max, item) => Math.max(max, Number(item.total_tokens || 0)), 0);
    const hourBars = hours.map((item) => {
      const tokens = Number(item.total_tokens || 0);
      const height = maxHour > 0 ? Math.max(3, Math.round(tokens / maxHour * 100)) : 3;
      const classes = tokens > 0 ? 'trend-bar' : 'trend-bar empty';
      return `<div class="${classes}" style="height:${height}%" title="${escapeHtml(`${item.hour}:00 · ${formatNumber(tokens)} tokens`)}"></div>`;
    }).join('');
    const hoursPanel = `<div class="usage-trend"><div class="usage-trend-head"><span>活跃时段</span><span class="muted">按 token 加权 · 24 小时</span></div><div class="trend-chart">${hourBars}</div></div>`;
    const statRow = (label, valueText, width, hint, title) => `<div class="top-project"><div class="top-project-row"><span class="top-project-label"${title ? ` title="${escapeHtml(title)}"` : ''}>${escapeHtml(label)}${hint ? `<span class="muted"> · ${escapeHtml(hint)}</span>` : ''}</span><span class="top-project-value">${escapeHtml(valueText)}</span></div><div class="bar"><span style="width:${width}%"></span></div></div>`;
    const models = Array.isArray(insights.models) ? insights.models : [];
    const maxModelCost = models.reduce((max, item) => Math.max(max, Number(item.estimated_cost_usd || 0)), 0);
    const modelRows = models.map((item) => statRow(item.model, `${formatUsdCompact(item.estimated_cost_usd)} · ${formatNumber(item.total_tokens)} tokens`, maxModelCost > 0 ? Math.max(3, Math.round(Number(item.estimated_cost_usd || 0) / maxModelCost * 100)) : 3)).join('');
    const modelsPanel = `<div class="usage-trend"><div class="usage-trend-head"><span>模型成本分布</span><span class="muted">Top ${models.length}</span></div>${modelRows || '<div class="muted">暂无模型数据</div>'}</div>`;
    const buckets = Array.isArray(insights.size_buckets) ? insights.size_buckets : [];
    const maxBucketCount = buckets.reduce((max, item) => Math.max(max, Number(item.conversations || 0)), 0);
    const bucketRows = buckets.map((item) => statRow(item.label, `${formatNumber(item.conversations)} 个 · ${formatUsdCompact(item.estimated_cost_usd)}`, maxBucketCount > 0 ? Math.max(3, Math.round(Number(item.conversations || 0) / maxBucketCount * 100)) : 3)).join('');
    const bucketsPanel = `<div class="usage-trend"><div class="usage-trend-head"><span>对话规模分布</span><span class="muted">按轮数</span></div>${bucketRows}</div>`;
    const suggestions = Array.isArray(insights.suggestions) ? insights.suggestions : [];
    const suggestionCards = suggestions.length
      ? suggestions.map((item) => `<div class="alert-banner ${escapeHtml(item.level || 'info')}">${item.saving_usd !== null && item.saving_usd !== undefined ? `<span class="suggestion-saving">可省 ${escapeHtml(formatUsdCompact(item.saving_usd))}</span>` : ''}<strong>${escapeHtml(item.title)}</strong><div>${escapeHtml(item.detail)}</div></div>`).join('')
      : '<div class="empty-state">当前用量模式很健康，没有发现明显的浪费点。</div>';
    const observations = Array.isArray(insights.observations) ? insights.observations : [];
    const observationItems = observations.map((item) => `<li>${escapeHtml(item)}</li>`).join('');
    const observationsPanel = observations.length
      ? `<div class="usage-trend"><div class="usage-trend-head"><span>习惯画像</span><span class="muted">${escapeHtml(windowLabel)}的已索引对话</span></div><ul class="observation-list">${observationItems}</ul></div>`
      : '';
    const topConversations = Array.isArray(insights.top_conversations) ? insights.top_conversations : [];
    const maxTopCost = Number((topConversations[0] || {}).estimated_cost_usd || 0);
    // 中文注释：最贵对话按「订阅（产品 · 套餐）· 账号 · 会话 · 项目路径」展示。
    // 订阅与账号卡片同源（/api/state + 同一套 planLabel 归一化），会话 ID 只显示
    // 前 12 位、完整 ID 放在悬停提示里，未定价提示仍然缀在最后。
    const topSubscriptions = usageAccountSubscriptions();
    const conversationTitle = (item) => {
      const meta = topSubscriptions.get(item.account) || {};
      const subscription = meta.plan ? `${meta.product} · ${meta.plan}` : (meta.product || '');
      return subscription || `${item.account || '未知账号'}`;
    };
    const conversationDetail = (item, fullSessionId = false) => {
      const sessionId = String(item.label || '').trim();
      return [
        item.account || '未知账号',
        sessionId ? `会话 ${fullSessionId ? sessionId : sessionId.slice(0, 12)}` : '',
        item.project || '',
      ].filter(Boolean).join(' · ');
    };
    const topRows = topConversations.map((item) => statRow(
      conversationTitle(item),
      `${formatUsdCompact(item.estimated_cost_usd)} · ${formatNumber(item.total_tokens)} tokens · ${formatNumber(item.turns)} 轮`,
      maxTopCost > 0 ? Math.max(3, Math.round(Number(item.estimated_cost_usd || 0) / maxTopCost * 100)) : 3,
      `${conversationDetail(item)}${item.has_unpriced ? ' · 含未定价模型' : ''}`,
      `${conversationTitle(item)} · ${conversationDetail(item, true)}`
    )).join('');
    container.innerHTML = `${summary}${observationsPanel}<div class="insights-grid">${hoursPanel}${modelsPanel}${bucketsPanel}</div><div class="usage-trend"><div class="usage-trend-head"><span>省 token 建议</span><span class="muted">${escapeHtml(windowLabel)} · 按用量规模估算</span></div><div class="alert-list">${suggestionCards}</div></div><div class="usage-trend"><div class="usage-trend-head"><span>最贵对话 Top ${topConversations.length}</span><span class="muted">按 API 等价金额</span></div>${topRows || '<div class="muted">暂无可计价对话</div>'}</div>`;
  };
  const refreshInsights = async () => {
    const button = document.getElementById('insights-load-button');
    const container = document.getElementById('insights-content');
    if (insightsPollTimer) {
      window.clearTimeout(insightsPollTimer);
      insightsPollTimer = 0;
    }
    if (button) {
      button.disabled = true;
      button.textContent = '正在分析…';
    }
    if (!latestInsightsState) {
      container.innerHTML = '<div class="empty-state">正在读取用量索引…</div>';
    }
    const poll = async () => {
      try {
        const insightsUrl = insightsPeriodDays ? `/api/insights?days=${insightsPeriodDays}` : '/api/insights';
        const response = await fetch(insightsUrl, { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        latestInsightsState = payload;
        renderInsights(payload);
        const insights = payload.insights || {};
        const indexing = insights.indexing || {};
        if (insights.ready !== true && indexing.complete === false) {
          if (button) {
            button.textContent = `索引中 ${Number(indexing.percent || 0).toFixed(1)}%`;
          }
          insightsPollTimer = window.setTimeout(poll, 1000);
          return;
        }
        if (button) {
          button.disabled = false;
          button.textContent = '重新分析';
        }
      } catch (error) {
        container.innerHTML = `<div class="error" style="display:block">读取习惯分析失败：${escapeHtml(error.message)}</div>`;
        if (button) {
          button.disabled = false;
          button.textContent = '重新分析';
        }
      }
    };
    await poll();
  };
  const renderTraffic = (state) => {
    const container = document.getElementById('traffic-content');
    const countLabel = document.getElementById('traffic-section-count');
    if (!container) return;
    const traffic = state.traffic || {};
    const processes = Array.isArray(traffic.processes) ? traffic.processes : [];
    const totals = traffic.totals || {};
    const thresholds = traffic.thresholds || {};
    if (countLabel) {
      countLabel.textContent = `${processes.length} 个进程 · 近 15 秒 ${formatDataSize(totals.burst_bytes || 0)}`;
    }
    if (traffic.source === 'unavailable') {
      const reason = traffic.reason || '无法读取内核 TCP 计数（INET_DIAG）';
      container.innerHTML = `<div class="empty-state">${escapeHtml(reason)}。异常流量监控暂不可用。</div>`;
      return;
    }
    if (processes.length === 0) {
      container.innerHTML = '<div class="empty-state">当前没有识别到 Codex / Grok / Kimi / DeepSeek Harness 等 code agent 进程。</div>';
      return;
    }
    const warnBytes = Number(thresholds.burst_warn_bytes || 0);
    const rank = { danger: 0, warn: 1 };
    const ordered = [...processes].sort((left, right) => {
      const leftRank = rank[left.alert_level] ?? 2;
      const rightRank = rank[right.alert_level] ?? 2;
      if (leftRank !== rightRank) return leftRank - rightRank;
      return Number(right.burst_bytes || 0) - Number(left.burst_bytes || 0);
    });
    const rows = ordered.map((item) => {
      const level = item.alert_level === 'danger' ? 'danger' : item.alert_level === 'warn' ? 'warn' : 'ok';
      const status = item.alert_level === 'danger' ? '异常大上传' : item.alert_level === 'warn' ? '偏高' : '正常';
      const remotes = (item.connections || []).filter((conn) => !conn.loopback && !conn.service).slice(0, 3).map((conn) => conn.remote).join(' · ') || '无外连';
      return `<tr>
        <td><div>${escapeHtml(item.product_label || item.product)}</div><div class="muted">pid ${escapeHtml(item.pid)}${(item.pids || []).length > 1 ? ` · ${item.pids.length} 个进程` : ''}</div></td>
        <td class="cwd">${escapeHtml(item.cwd || '未知')}</td>
        <td class="usage-number">${escapeHtml(formatDataSize(item.burst_bytes || 0))}<div class="muted">本轮 ${escapeHtml(formatDataSize(item.external_upload_delta || 0))}</div></td>
        <td class="usage-number">${escapeHtml(formatDataSize(item.window_bytes || 0))}<div class="muted">监控累计 ${escapeHtml(formatDataSize(item.observed_external_bytes || 0))}</div></td>
        <td class="traffic-remote">${escapeHtml(remotes)}</td>
        <td><span class="pill ${level}">${status}</span>${warnBytes ? `<div class="muted">阈值 ${escapeHtml(formatDataSize(warnBytes))} / 15s</div>` : ''}</td>
      </tr>`;
    }).join('');
    const platformNote = traffic.source === 'process-only'
      ? `<div class="usage-note">${escapeHtml(traffic.reason || '当前平台没有内核 TCP 计数')}，字节列仅供参考。</div>`
      : '';
    container.innerHTML = `${platformNote}<div class="usage-note">只统计离开本机的 TCP 发送字节。回环和进程自己监听的 Web UI（例如 DeepSeek Harness :3080 推给浏览器的会话）不计入外发告警。首次看到一条连接时只记基线，避免把监控启动前的历史流量当成突发上传。</div>
      <div class="table-wrap traffic-table"><table>
        <thead><tr><th>Agent</th><th>工作目录</th><th>近 15 秒外发</th><th>近 5 分钟 / 累计</th><th>主要对端</th><th>状态</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>`;
  };
  // 告警历史：读取落盘告警，支持筛选、已读和清理。
  let alertHistoryState = null;
  let alertHistoryLimit = 50;
  const alertFilterValue = (id) => {
    const element = document.getElementById(id);
    return element ? String(element.value || '') : '';
  };
  const alertQueryString = () => {
    const params = new URLSearchParams();
    const days = alertFilterValue('alert-range-filter');
    if (days) params.set('days', days);
    const level = alertFilterValue('alert-level-filter');
    if (level) params.set('level', level);
    const kind = alertFilterValue('alert-kind-filter');
    if (kind) params.set('kind', kind);
    const ack = alertFilterValue('alert-ack-filter');
    if (ack) params.set('ack', ack);
    const keyword = alertFilterValue('alert-keyword-filter').trim();
    if (keyword) params.set('q', keyword);
    params.set('limit', String(alertHistoryLimit));
    return params.toString();
  };
  // 告警历史：先给结论（未读/红色/最近），再给可筛选的明细表。
  const renderAlertStats = (stats, payload) => {
    const container = document.getElementById('alert-history-stats');
    if (!container) return;
    const scope = Number((payload.filters && payload.filters.days) || 0);
    const unread = Number(stats.unread || 0);
    container.innerHTML = [
      `<div class="stat"><div class="stat-label">未读告警</div><div class="stat-value ${unread > 0 ? 'warn' : 'ok'}">${escapeHtml(formatNumber(unread))}</div><div class="stat-foot">共 ${escapeHtml(formatNumber(stats.total || 0))} 条匹配记录</div></div>`,
      `<div class="stat"><div class="stat-label">红色告警</div><div class="stat-value ${Number(stats.danger || 0) > 0 ? 'danger' : ''}">${escapeHtml(formatNumber(stats.danger || 0))}</div><div class="stat-foot">黄色 ${escapeHtml(formatNumber(stats.warn || 0))} 条</div></div>`,
      `<div class="stat"><div class="stat-label">最近一次</div><div class="stat-value" style="font-size:15px">${escapeHtml(formatTime(stats.last_alert_at))}</div><div class="stat-foot">${scope ? `统计范围：近 ${escapeHtml(String(scope))} 天` : '统计范围：全部历史'}</div></div>`,
      `<div class="stat"><div class="stat-label">重复合并窗口</div><div class="stat-value">${escapeHtml(String(Number(payload.merge_window_seconds || 0)))} 秒</div><div class="stat-foot">同进程同规则告警合并为一条</div></div>`
    ].join('');
  };
  const renderAlertHistory = (payload) => {
    const container = document.getElementById('alert-history-content');
    const countLabel = document.getElementById('alert-history-count');
    if (!container) return;
    alertHistoryState = payload;
    if (payload.available === false) {
      container.innerHTML = '<div class="empty-state"><span class="empty-title">告警历史不可用</span><span class="empty-hint">当前监控进程未启用落盘，或数据库无法读取。</span></div>';
      if (countLabel) countLabel.textContent = '不可用';
      return;
    }
    const alerts = Array.isArray(payload.alerts) ? payload.alerts : [];
    const stats = payload.stats || {};
    renderAlertStats(stats, payload);
    if (countLabel) {
      countLabel.textContent = `未读 ${Number(stats.unread || 0)} / 共 ${Number(stats.total || 0)} 条`;
    }
    if (alerts.length === 0) {
      container.innerHTML = '<div class="empty-state"><span class="empty-title">当前筛选条件下没有历史告警</span><span class="empty-hint">放宽时间范围、级别或已读状态再试。</span></div>';
      return;
    }
    const rows = alerts.map((alert) => {
      const level = alert.level === 'danger' ? 'danger' : 'warn';
      const levelText = alert.level === 'danger' ? '异常大上传' : '偏高';
      const rule = alert.kind === 'burst' ? '突发窗口' : '累计窗口';
      const repeat = Number(alert.count || 1) > 1 ? `<span class="chip warn">合并 ${Number(alert.count)} 次</span> ` : '';
      const ackAction = alert.acknowledged ? 'unack' : 'ack';
      const ackLabel = alert.acknowledged ? '标为未读' : '标为已读';
      return `<tr class="${alert.acknowledged ? '' : 'row-unread'}">
        <td><div class="cell-main">${escapeHtml(formatTime(alert.last_seen_at))}</div><div class="cell-sub">首次 ${escapeHtml(formatTime(alert.first_seen_at))}</div></td>
        <td><span class="pill ${level}">${levelText}</span><div class="cell-sub">#${escapeHtml(alert.id)}</div></td>
        <td><div class="cell-main">${escapeHtml(alert.product_label || alert.product)}</div><div class="cell-sub">pid ${escapeHtml(alert.pid)}${alert.command ? ` · ${escapeHtml(alert.command)}` : ''}</div></td>
        <td><span class="truncate" title="${escapeHtml(alert.cwd || '')}">${escapeHtml(alert.cwd || '未知目录')}</span></td>
        <td><div class="cell-main">${escapeHtml(formatDataSize(alert.peak_bytes || alert.bytes || 0))}</div><div class="cell-sub">${repeat}${escapeHtml(rule)} · ${escapeHtml(String(Number(alert.window_seconds || 0)))} 秒</div></td>
        <td><span class="mono">${escapeHtml(alert.remote || '—')}</span></td>
        <td><span class="pill ${alert.acknowledged ? 'other' : 'warn'}">${alert.acknowledged ? '已读' : '未读'}</span></td>
        <td><button class="btn mini" type="button" data-alert-action="${ackAction}" data-alert-id="${escapeHtml(alert.id)}">${ackLabel}</button></td>
      </tr>`;
    }).join('');
    const more = payload.has_more
      ? `<div class="table-actions"><button id="alert-load-more-button" class="btn" type="button">加载更多（已显示 ${alerts.length} / ${Number(stats.total || alerts.length)} 条）</button></div>`
      : '';
    container.innerHTML = `<div class="table-wrap"><table class="tight alert-history-table">
        <thead><tr><th>时间</th><th>级别</th><th>Agent / 进程</th><th>工作目录</th><th>外发峰值 / 规则</th><th>主要对端</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>${more}`;
    container.querySelectorAll('[data-alert-action]').forEach((button) => {
      button.addEventListener('click', () => mutateAlerts(button.dataset.alertAction, { ids: [Number(button.dataset.alertId)] }));
    });
    document.getElementById('alert-load-more-button')?.addEventListener('click', () => {
      alertHistoryLimit += 50;
      refreshAlertHistory();
    });
  };
  const refreshAlertHistory = async () => {
    const container = document.getElementById('alert-history-content');
    const button = document.getElementById('alert-history-load-button');
    if (button) {
      button.disabled = true;
      button.classList.add('is-spinning');
    }
    try {
      const response = await fetch(`/api/alerts?${alertQueryString()}`, { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      renderAlertHistory(await response.json());
    } catch (error) {
      if (container) container.innerHTML = `<div class="empty-state">读取告警历史失败：${escapeHtml(error.message)}</div>`;
    } finally {
      if (button) {
        button.disabled = false;
        button.classList.remove('is-spinning');
      }
    }
  };
  const mutateAlerts = async (action, body) => {
    try {
      const response = await fetch('/api/alerts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, ...body })
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await response.json();
      await refreshAlertHistory();
      refresh();
    } catch (error) {
      const container = document.getElementById('alert-history-content');
      if (container) container.innerHTML = `<div class="empty-state">更新告警失败：${escapeHtml(error.message)}</div>`;
    }
  };
  const clearAlertHistory = async () => {
    const days = Number(alertFilterValue('alert-range-filter') || 0);
    const total = Number((alertHistoryState && alertHistoryState.stats && alertHistoryState.stats.total) || 0);
    if (total === 0) {
      window.alert('当前筛选条件下没有可清理的告警。');
      return;
    }
    const scope = days ? `早于近 ${days} 天的历史告警` : '全部历史告警';
    if (!window.confirm(`将删除${scope}（当前筛选条件内约 ${total} 条，含已读与未读）。此操作不可撤销，是否继续？`)) return;
    await mutateAlerts('clear', days ? { before: Date.now() / 1000 - days * 86400 } : { all: true });
  };
  // 用量检索：按日期、模型和会话查询已索引的 token 历史。
  let usageSearchState = null;
  let usageSearchGroup = 'session';
  let usageSearchLimit = 50;
  const usageSearchValue = (id) => {
    const element = document.getElementById(id);
    return element ? String(element.value || '') : '';
  };
  const usageSearchQueryString = () => {
    const params = new URLSearchParams();
    const from = usageSearchValue('usage-search-from');
    const to = usageSearchValue('usage-search-to');
    if (from || to) {
      if (from) params.set('from', from);
      if (to) params.set('to', to);
    } else {
      params.set('days', usageSearchValue('usage-search-range') || '30');
    }
    const model = usageSearchValue('usage-search-model');
    if (model) params.set('model', model);
    const account = usageSearchValue('usage-search-account');
    if (account) params.set('account', account);
    const keyword = usageSearchValue('usage-search-keyword').trim();
    if (keyword) params.set('q', keyword);
    params.set('group', usageSearchGroup);
    params.set('sort', usageSearchValue('usage-search-sort') || 'recent');
    params.set('limit', String(usageSearchLimit));
    return params.toString();
  };
  const fillUsageSearchModels = (models) => {
    const select = document.getElementById('usage-search-model');
    if (!select) return;
    const current = select.value;
    const options = ['<option value="">全部模型</option>'].concat(
      (models || []).map((model) => `<option value="${escapeHtml(model)}"${model === current ? ' selected' : ''}>${escapeHtml(model)}</option>`)
    );
    select.innerHTML = options.join('');
    select.value = current;
  };
  const fillUsageSearchAccounts = (accounts) => {
    const select = document.getElementById('usage-search-account');
    if (!select) return;
    const current = select.value;
    const options = ['<option value="">全部账号</option>'].concat(
      (accounts || []).map((account) => `<option value="${escapeHtml(account)}"${account === current ? ' selected' : ''}>${escapeHtml(account)}</option>`)
    );
    select.innerHTML = options.join('');
    select.value = current;
  };
  const usageSearchNumber = (value) => Number(value || 0).toLocaleString('zh-CN');
  const renderUsageSearchStats = (search, facets) => {
    const container = document.getElementById('usage-search-stats');
    if (!container) return;
    const totals = (search && search.totals) || {};
    const usage = totals.usage || {};
    const cost = totals.cost_usd;
    const scope = facets.records
      ? `索引 ${formatDay(facets.first_at)} ~ ${formatDay(facets.last_at)}`
      : '索引为空';
    container.innerHTML = [
      `<div class="stat"><div class="stat-label">匹配行</div><div class="stat-value">${escapeHtml(formatNumber(search.matched_rows || 0))}</div><div class="stat-foot">${escapeHtml(formatNumber(totals.records || 0))} 条原始记录</div></div>`,
      `<div class="stat"><div class="stat-label">涉及会话</div><div class="stat-value">${escapeHtml(formatNumber(totals.sessions || 0))}</div><div class="stat-foot">${escapeHtml(formatNumber(totals.models || 0))} 个模型</div></div>`,
      `<div class="stat"><div class="stat-label">输入 token</div><div class="stat-value">${escapeHtml(formatTokens(usage.input_tokens || 0))}</div><div class="stat-foot">其中缓存 ${escapeHtml(formatTokens(usage.cached_input_tokens || 0))}</div></div>`,
      `<div class="stat"><div class="stat-label">输出 token</div><div class="stat-value">${escapeHtml(formatTokens(usage.output_tokens || 0))}</div><div class="stat-foot">推理 ${escapeHtml(formatTokens(usage.reasoning_output_tokens || 0))}</div></div>`,
      `<div class="stat"><div class="stat-label">合计 token</div><div class="stat-value">${escapeHtml(formatTokens(totals.total_tokens || 0))}</div><div class="stat-foot">${escapeHtml(scope)}</div></div>`,
      `<div class="stat"><div class="stat-label">API 等价金额</div><div class="stat-value ${cost === null || cost === undefined ? '' : 'ok'}" style="font-size:17px">${cost === null || cost === undefined ? '部分无单价' : escapeHtml(formatUsdCompact(cost))}</div><div class="stat-foot">${escapeHtml(formatNumber(totals.records || 0))} 条记录合计</div></div>`
    ].join('');
  };
  const renderUsageSearch = (payload) => {
    const container = document.getElementById('usage-search-content');
    const countLabel = document.getElementById('usage-search-count');
    if (!container) return;
    usageSearchState = payload;
    const search = payload.search || {};
    const facets = payload.facets || {};
    fillUsageSearchModels(facets.models);
    fillUsageSearchAccounts(facets.accounts);
    renderUsageSearchStats(search, facets);
    if (search.available === false || facets.available === false) {
      const scope = facets.records
        ? `索引覆盖 ${escapeHtml(formatDay(facets.first_at))} ~ ${escapeHtml(formatDay(facets.last_at))}`
        : '用量索引还是空的，先让 daemon 完成一次索引再检索。';
      container.innerHTML = `<div class="empty-state"><span class="empty-title">没有可检索的用量索引</span><span class="empty-hint">${scope}</span></div>`;
      if (countLabel) countLabel.textContent = '索引为空';
      return;
    }
    const rows = Array.isArray(search.rows) ? search.rows : [];
    const totals = search.totals || {};
    if (countLabel) {
      countLabel.textContent = `${formatNumber(search.matched_rows)} 行 · ${formatNumber(totals.total_tokens)} token`;
    }
    if (rows.length === 0) {
      container.innerHTML = '<div class="empty-state"><span class="empty-title">没有符合条件的用量记录</span><span class="empty-hint">若刚产生用量，索引可能仍在写入，可稍后重试。</span></div>';
      return;
    }
    const group = search.group || usageSearchGroup;
    const header = group === 'date'
      ? '<tr><th>日期</th><th>会话 / 模型</th><th class="num">输入（缓存）</th><th class="num">输出</th><th class="num">合计 token</th><th class="num">估算金额</th><th class="num">记录</th></tr>'
      : group === 'model'
        ? '<tr><th>模型</th><th>会话</th><th class="num">输入（缓存）</th><th class="num">输出</th><th class="num">合计 token</th><th class="num">估算金额</th><th class="num">记录</th></tr>'
        : group === 'account'
          ? '<tr><th>账号</th><th>产品 / 模型</th><th class="num">输入（缓存）</th><th class="num">输出</th><th class="num">合计 token</th><th class="num">估算金额</th><th class="num">记录</th></tr>'
          : '<tr><th>时间</th><th>会话</th><th>模型</th><th>项目 / 工作目录</th><th class="num">输入（缓存）</th><th class="num">输出</th><th class="num">合计 token</th><th class="num">估算金额</th><th class="num">记录</th></tr>';
    const body = rows.map((row) => {
      const usage = row.usage || {};
      const cost = row.estimated_cost_usd === null || row.estimated_cost_usd === undefined
        ? '<span class="muted">未计价</span>'
        : escapeHtml(formatUsdCompact(row.estimated_cost_usd));
      const tokens = `${escapeHtml(formatNumber(usage.input_tokens))}<div class="cell-sub">缓存 ${escapeHtml(formatNumber(usage.cached_input_tokens))}</div>`;
      const output = `${escapeHtml(formatNumber(usage.output_tokens))}<div class="cell-sub">推理 ${escapeHtml(formatNumber(usage.reasoning_output_tokens))}</div>`;
      const total = `${escapeHtml(formatNumber(row.total_tokens))}`;
      const models = (row.models || []).map((model) => `<span class="chip">${escapeHtml(model)}</span>`).join(' ');
      if (group === 'date') {
        return `<tr>
          <td><div class="cell-main">${escapeHtml(row.date)}</div><div class="cell-sub">最近 ${escapeHtml(formatTime(row.last_at))}</div></td>
          <td><div class="cell-main">${escapeHtml(formatNumber(row.records))} 条记录</div><div class="cell-sub">${escapeHtml(String((row.models || []).length))} 个模型</div></td>
          <td class="num">${tokens}</td><td class="num">${output}</td><td class="num">${total}</td><td class="num">${cost}</td><td class="num">${escapeHtml(formatNumber(row.records))}</td>
        </tr>`;
      }
      if (group === 'model') {
        return `<tr>
          <td><div class="cell-main">${escapeHtml((row.models || ['未知模型'])[0])}</div><div class="cell-sub">最近 ${escapeHtml(formatTime(row.last_at))}</div></td>
          <td>${escapeHtml(formatNumber(row.records))} 条<div class="cell-sub">${escapeHtml(String((row.models || []).length))} 个会话/模型组合</div></td>
          <td class="num">${tokens}</td><td class="num">${output}</td><td class="num">${total}</td><td class="num">${cost}</td><td class="num">${escapeHtml(formatNumber(row.records))}</td>
        </tr>`;
      }
      if (group === 'account') {
        const products = (row.products || []).map((product) => `<span class="chip">${escapeHtml(product)}</span>`).join(' ');
        return `<tr>
          <td><div class="cell-main">${escapeHtml(row.account || '未知账号')}</div><div class="cell-sub">${escapeHtml(row.account_id || row.account_key || '未记录账号 ID')}</div></td>
          <td><div>${escapeHtml(formatNumber(row.records))} 条记录</div><div class="cell-sub">${products || escapeHtml(String((row.models || []).length)) + ' 个模型'}</div></td>
          <td class="num">${tokens}</td><td class="num">${output}</td><td class="num">${total}</td><td class="num">${cost}</td><td class="num">${escapeHtml(formatNumber(row.records))}</td>
        </tr>`;
      }
      const sessionLabel = row.session_id || (row.session_path || '').split('/').pop() || '未知会话';
      return `<tr>
        <td><div class="cell-main">${escapeHtml(formatTime(row.last_at))}</div><div class="cell-sub">${escapeHtml(row.date)}</div></td>
        <td><button class="chip-button" type="button" data-usage-session="${escapeHtml(row.session_id || '')}" title="下钻到该会话">${escapeHtml(String(sessionLabel).slice(0, 12))}</button><div class="cell-sub">${escapeHtml(row.account ? `${row.account} · ` : '')}${escapeHtml(formatNumber(row.records))} 条</div></td>
        <td>${escapeHtml(row.model || '—')}</td>
        <td><span class="truncate" title="${escapeHtml(row.project || '')}">${escapeHtml(row.project || '未知目录')}</span>${models ? `<div class="cell-sub">${models}</div>` : ''}</td>
        <td class="num">${tokens}</td><td class="num">${output}</td><td class="num">${total}</td><td class="num">${cost}</td><td class="num">${escapeHtml(formatNumber(row.records))}</td>
      </tr>`;
    }).join('');
    const more = search.has_more
      ? `<div class="table-actions"><button id="usage-search-more-button" class="btn" type="button">加载更多（已显示 ${rows.length} / ${formatNumber(search.matched_rows)} 行）</button></div>`
      : '';
    container.innerHTML = `<div class="table-wrap"><table class="tight usage-table">
        <thead>${header}</thead>
        <tbody>${body}</tbody>
      </table></div>${more}`;
    container.querySelectorAll('[data-usage-session]').forEach((button) => {
      button.addEventListener('click', () => {
        const value = button.dataset.usageSession || '';
        if (!value) return;
        const input = document.getElementById('usage-search-keyword');
        if (input) input.value = value;
        usageSearchLimit = 50;
        refreshUsageSearch();
      });
    });
    document.getElementById('usage-search-more-button')?.addEventListener('click', () => {
      usageSearchLimit += 50;
      refreshUsageSearch();
    });
  };
  const refreshUsageSearch = async () => {
    const container = document.getElementById('usage-search-content');
    const button = document.getElementById('usage-search-load-button');
    if (button) {
      button.disabled = true;
      button.classList.add('is-spinning');
    }
    try {
      const response = await fetch(`/api/usage/search?${usageSearchQueryString()}`, { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      renderUsageSearch(await response.json());
    } catch (error) {
      if (container) container.innerHTML = `<div class="empty-state">用量检索失败：${escapeHtml(error.message)}</div>`;
    } finally {
      if (button) {
        button.disabled = false;
        button.classList.remove('is-spinning');
      }
    }
  };
  // 磁盘与会话管理：目录占用、归档与清理。
  let housekeepingState = null;
  const housekeepingValue = (id, fallback) => {
    const element = document.getElementById(id);
    if (!element) return fallback;
    const raw = String(element.value || '').trim();
    if (!raw) return fallback;
    const number = Number(raw);
    return Number.isFinite(number) ? number : fallback;
  };
  const housekeepingCriteria = () => ({
    days: Math.max(1, Math.round(housekeepingValue('housekeeping-days', 30))),
    min_size_mb: Math.max(0, housekeepingValue('housekeeping-min-size', 0))
  });
  const renderHousekeepingStats = (summary, payload) => {
    const container = document.getElementById('housekeeping-stats');
    if (!container) return;
    const totals = summary.totals || {};
    const preview = (payload && payload.preview) || summary.preview || {};
    const archives = (payload && payload.archives) || [];
    const reminderCount = (summary.reminders || []).length;
    const singleWarn = (summary.thresholds || {}).single_warn_bytes || 0;
    container.innerHTML = [
      `<div class="stat"><div class="stat-label">目录合计</div><div class="stat-value">${escapeHtml(formatDataSize(totals.bytes || 0))}</div><div class="stat-foot">${escapeHtml(formatNumber(totals.directories || 0))} 个目录 · ${escapeHtml(formatNumber(totals.files || 0))} 个文件</div></div>`,
      `<div class="stat"><div class="stat-label">会话文件</div><div class="stat-value">${escapeHtml(formatDataSize(totals.session_bytes || 0))}</div><div class="stat-foot">${escapeHtml(formatNumber(totals.session_files || 0))} 个 Codex session</div></div>`,
      `<div class="stat"><div class="stat-label">可归档 / 清理</div><div class="stat-value ${Number(preview.count || 0) > 0 ? 'warn' : 'ok'}">${escapeHtml(formatNumber(preview.count || 0))} 个</div><div class="stat-foot">约 ${escapeHtml(formatDataSize(preview.bytes || 0))} · 跳过活动 ${escapeHtml(formatNumber(preview.skipped_active || 0))} 个</div></div>`,
      `<div class="stat"><div class="stat-label">已有归档</div><div class="stat-value">${escapeHtml(formatNumber(archives.length))} 份</div><div class="stat-foot">${escapeHtml(summary.archive_dir ? String(summary.archive_dir).split('/').slice(-2).join('/') : '未配置归档目录')}</div></div>`,
      `<div class="stat"><div class="stat-label">单目录阈值</div><div class="stat-value ${reminderCount ? 'danger' : 'ok'}" style="font-size:17px">${escapeHtml(formatDataSize(singleWarn || 0))}</div><div class="stat-foot">${reminderCount ? `${escapeHtml(formatNumber(reminderCount))} 条磁盘提醒` : '当前未超阈值'}</div></div>`
    ].join('');
  };
  const renderHousekeeping = (state) => {
    const container = document.getElementById('housekeeping-content');
    const countLabel = document.getElementById('housekeeping-count');
    if (!container) return;
    const summary = (state && state.housekeeping) || {};
    const directories = Array.isArray(summary.directories) ? summary.directories : [];
    const totals = summary.totals || {};
    if (summary.available === false) {
      container.innerHTML = '<div class="empty-state"><span class="empty-title">磁盘统计不可用</span><span class="empty-hint">监控进程未启动磁盘扫描，或状态目录不可读。</span></div>';
      if (countLabel) countLabel.textContent = '不可用';
      return;
    }
    const preview = summary.preview || {};
    if (countLabel) {
      countLabel.textContent = `合计 ${formatDataSize(totals.bytes || 0)} · ${Number(preview.count || 0)} 个可归档`;
    }
    renderHousekeepingStats(summary, housekeepingState);
    const reminderBanners = (summary.reminders || []).map((reminder) => (
      `<div class="alert-banner ${reminder.level === 'danger' ? 'danger' : 'warn'}">${escapeHtml(reminder.message || reminder.title || '')}</div>`
    )).join('');
    const cards = directories.map((item) => {
      const bytes = Number(item.bytes || 0);
      const sessionBytes = Number(item.session_bytes || 0);
      const sessionShare = bytes > 0 ? Math.min(100, Math.round(sessionBytes / bytes * 100)) : 0;
      const ratio = bytes > 0
        ? `<div class="ratio" title="会话文件占 ${sessionShare}%"><span class="ratio-sessions" style="width:${sessionShare}%"></span><span class="ratio-other" style="width:${100 - sessionShare}%"></span></div>`
        : '';
      const children = (item.top_children || []).slice(0, 4).map((child) => (
        `<span class="chip">${escapeHtml(child.name)} ${escapeHtml(formatDataSize(child.bytes || 0))}</span>`
      )).join('');
      return `<article class="mini-card">
        <div class="mini-card-head">
          <div><div class="mini-card-title">${escapeHtml(item.label || '未知目录')}</div><div class="mini-card-path" title="${escapeHtml(item.path || '')}">${escapeHtml(item.path || '')}</div></div>
          <span class="pill ${item.cleanable ? 'ok' : 'other'}">${item.cleanable ? '可归档' : '仅统计'}</span>
        </div>
        <div class="mini-card-metrics">
          <div><div class="metric-label">占用</div><div class="metric-value">${escapeHtml(formatDataSize(bytes))}</div></div>
          <div><div class="metric-label">会话文件</div><div class="metric-value">${escapeHtml(formatDataSize(sessionBytes))}<div class="cell-sub">${escapeHtml(formatNumber(item.session_files || 0))} 个</div></div></div>
          <div><div class="metric-label">文件总数</div><div class="metric-value">${escapeHtml(formatNumber(item.files || 0))}</div></div>
        </div>
        ${ratio}
        ${children ? `<div class="chip-list">${children}</div>` : ''}
      </article>`;
    }).join('');
    const scanNote = summary.observed_at
      ? `上次扫描 ${escapeHtml(formatTime(summary.observed_at))}`
      : '尚未完成扫描';
    container.innerHTML = `${reminderBanners}
      <div class="usage-note">${scanNote}。按当前条件（${escapeHtml(String(preview.criteria ? preview.criteria.older_than_days : 30))} 天前、非活动）可归档或清理 ${escapeHtml(formatNumber(preview.count || 0))} 个文件，约 ${escapeHtml(formatDataSize(preview.bytes || 0))}；过新跳过 ${escapeHtml(formatNumber(preview.skipped_recent || 0))} 个。</div>
      ${cards ? `<div class="card-grid">${cards}</div>` : '<div class="empty-state"><span class="empty-title">没有需要统计的目录</span></div>'}`;
  };
  const renderHousekeepingActions = () => {
    const container = document.getElementById('housekeeping-actions');
    if (!container) return;
    if (!housekeepingState) {
      container.innerHTML = '';
      return;
    }
    const payload = housekeepingState;
    const preview = payload.preview || {};
    const files = Array.isArray(preview.files) ? preview.files : [];
    const archives = Array.isArray(payload.archives) ? payload.archives : [];
    const previewRows = files.slice(0, 8);
    const rows = previewRows.map((item) => `<tr>
      <td><div class="cell-main">${escapeHtml(formatTime(item.modified_at))}</div><div class="cell-sub">${escapeHtml(String(item.session_id || '').slice(0, 12))}</div></td>
      <td><span class="truncate" title="${escapeHtml(item.path || '')}">${escapeHtml(item.path || '')}</span></td>
      <td class="num">${escapeHtml(formatDataSize(item.size || 0))}</td>
    </tr>`).join('');
    const archiveCards = archives.map((item) => `<article class="mini-card">
      <div class="mini-card-head">
        <div><div class="mini-card-title">${escapeHtml(String(item.archive || '').split('/').pop() || '')}</div><div class="mini-card-path">${escapeHtml(item.created_at ? formatTime(item.created_at) : '时间未知')}</div></div>
        <button class="btn mini" type="button" data-housekeeping-restore="${escapeHtml(String(item.archive || '').split('/').pop() || '')}">恢复到原路径</button>
      </div>
      <div class="mini-card-metrics">
        <div><div class="metric-label">文件数</div><div class="metric-value">${escapeHtml(item.count === null || item.count === undefined ? '—' : formatNumber(item.count))}</div></div>
        <div><div class="metric-label">归档大小</div><div class="metric-value">${escapeHtml(formatDataSize(item.bytes || 0))}</div></div>
      </div>
    </article>`).join('');
    container.innerHTML = `<div class="account-subtitle"><span>待处理会话</span><span class="muted">${escapeHtml(formatNumber(preview.count || 0))} 个 · ${escapeHtml(formatDataSize(preview.bytes || 0))}</span></div>
      <div class="usage-note">归档会先打包 tar.gz 并写 manifest，校验通过后才删除原文件，可随时恢复；直接清理不可撤销。活动会话、10 分钟内改动过的文件会始终跳过。</div>
      ${rows ? `<div class="table-wrap"><table class="tight"><thead><tr><th>最后修改</th><th>会话文件</th><th class="num">大小</th></tr></thead><tbody>${rows}</tbody></table></div>${files.length > previewRows.length ? `<div class="usage-note">仅列出前 ${previewRows.length} 个，共 ${escapeHtml(formatNumber(preview.count || files.length))} 个待处理文件。</div>` : ''}` : '<div class="empty-state"><span class="empty-title">当前条件下没有可处理的会话</span><span class="empty-hint">放宽保留天数或降低体积下限再试。</span></div>'}
      ${archives.length ? `<div class="account-subtitle"><span>已有归档</span><span class="muted">${escapeHtml(formatNumber(archives.length))} 份</span></div><div class="card-grid">${archiveCards}</div>` : ''}`;
    container.querySelectorAll('[data-housekeeping-restore]').forEach((button) => {
      button.addEventListener('click', () => {
        const name = button.dataset.housekeepingRestore || '';
        if (!window.confirm(`确认从归档 ${name} 恢复会话文件到原始路径？`)) return;
        mutateHousekeeping('restore', { archive: name });
      });
    });
  };
  const refreshHousekeeping = async () => {
    const criteria = housekeepingCriteria();
    const button = document.getElementById('housekeeping-scan-button');
    if (button) {
      button.disabled = true;
      button.classList.add('is-spinning');
    }
    try {
      const response = await fetch(`/api/housekeeping?days=${criteria.days}&min_size_mb=${criteria.min_size_mb}&refresh=1`, { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      housekeepingState = await response.json();
      if (latestState) renderHousekeeping(latestState);
      renderHousekeepingActions();
      refresh();
    } catch (error) {
      const container = document.getElementById('housekeeping-content');
      if (container) container.innerHTML = `<div class="empty-state"><span class="empty-title">磁盘扫描失败</span><span class="empty-hint">${escapeHtml(error.message)}</span></div>`;
    } finally {
      if (button) {
        button.disabled = false;
        button.classList.remove('is-spinning');
      }
    }
  };
  const housekeepingNote = (text) => {
    const container = document.getElementById('housekeeping-actions');
    if (!container) return null;
    const note = document.createElement('div');
    note.className = 'alert-banner info action-result';
    note.id = 'housekeeping-task-note';
    const previous = document.getElementById('housekeeping-task-note');
    if (previous) {
      previous.replaceWith(note);
    } else {
      container.prepend(note);
    }
    note.textContent = text;
    return note;
  };
  const pollHousekeepingTask = async (taskId, action, report) => {
    const label = action === 'archive' ? '归档' : '清理';
    const show = report || housekeepingNote;
    const deadline = Date.now() + 30 * 60 * 1000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => window.setTimeout(resolve, 800));
      let payload = null;
      try {
        const response = await fetch(`/api/housekeeping?task=${taskId}`, { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        payload = await response.json();
      } catch (error) {
        show(`${label}进度查询失败：${error.message}`, 'danger');
        return;
      }
      const task = payload.task;
      if (!task) {
        await refreshHousekeeping();
        show(`${label}任务已结束（任务记录已被清理）。`, 'info');
        return;
      }
      const progress = task.progress || {};
      if (task.state === 'running') {
        const phase = progress.phase === 'compress' ? '压缩'
          : progress.phase === 'verify' ? '校验归档'
            : progress.phase === 'delete' ? '删除原文件'
              : '准备文件清单';
        const percent = progress.total ? Math.round((progress.done / progress.total) * 100) : 0;
        show(`${label}进行中：${phase} ${progress.done}/${progress.total}（${percent}%），已处理 ${formatDataSize(progress.bytes_done || 0)} / ${formatDataSize(progress.total_bytes || 0)}。`, 'info');
        continue;
      }
      if (task.state === 'failed') {
        await refreshHousekeeping();
        show(`${label}失败：${task.error || '未知原因'}`, 'danger');
        return;
      }
      const result = task.result || {};
      const detail = `${label}完成：${result.count || 0} 个文件，释放 ${formatDataSize(result.bytes || 0)}${result.archive ? `，归档 ${String(result.archive).split('/').pop()}` : ''}。`;
      await refreshHousekeeping();
      show(detail, 'info');
      return;
    }
    await refreshHousekeeping();
    show(`${label}仍在后台执行，可稍后刷新查看结果。`, 'info');
  };
  const mutateHousekeeping = async (action, body) => {
    const criteria = housekeepingCriteria();
    const container = document.getElementById('housekeeping-actions');
    const background = action === 'archive' || action === 'clean';
    try {
      const response = await fetch('/api/housekeeping', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, days: criteria.days, min_size_mb: criteria.min_size_mb, confirm: true, async: background, ...body })
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || `HTTP ${response.status}`);
      if (background && payload.task) {
        housekeepingNote(`${action === 'archive' ? '归档' : '清理'}已提交到后台执行…`);
        await pollHousekeepingTask(payload.task.id, action);
        return;
      }
      const changed = (payload.result && payload.result.count) || 0;
      const freed = (payload.result && payload.result.bytes) || 0;
      housekeepingState = { ...payload };
      if (latestState) renderHousekeeping(latestState);
      renderHousekeepingActions();
      housekeepingNote(
        action === 'restore'
          ? `已恢复 ${payload.result.restored} 个会话文件到 ${payload.result.destination}。`
          : `已完成${action === 'archive' ? '归档' : '清理'}：${changed} 个文件，释放 ${formatDataSize(freed)}。`
      );
    } catch (error) {
      if (container) container.innerHTML = `<div class="empty-state"><span class="empty-title">操作失败</span><span class="empty-hint">${escapeHtml(error.message)}</span></div>`;
    }
  };
  // 单会话归档的进度显示在页面顶部关注区，折叠的磁盘分区里看不到提示。
  const archiveNotice = (text, level) => {
    // 中文注释：单独一个容器，避免被每 5 秒的 renderAlerts 覆盖。
    const container = document.getElementById('session-notice');
    if (!container) return;
    let note = document.getElementById('session-archive-note');
    if (!note) {
      note = document.createElement('div');
      note.id = 'session-archive-note';
      container.append(note);
    }
    note.className = `alert-row ${level === 'danger' ? 'danger' : 'warn'}`;
    note.innerHTML = `<span class="alert-accent" aria-hidden="true"></span><div class="alert-body"><div class="alert-title">${escapeHtml(text)}</div><div class="alert-detail">来自活动会话表的「归档此会话」，归档文件可在磁盘与会话管理里恢复。</div></div><a class="alert-link" href="#housekeeping">查看磁盘与会话管理</a>`;
  };
  const archiveSession = async (path) => {
    archiveNotice('正在归档会话…', 'warn');
    try {
      const response = await fetch('/api/housekeeping', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'archive', session: path, confirm: true, async: true })
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || `HTTP ${response.status}`);
      if (payload.task) {
        await pollHousekeepingTask(payload.task.id, 'archive', archiveNotice);
      }
      refresh();
    } catch (error) {
      archiveNotice(`归档该会话失败：${error.message}`, 'danger');
    }
  };
  const renderAccounts = (state) => {
    const container = document.getElementById('account-list');
    const quotas = Array.isArray(state.quotas) ? state.quotas : [];
    const sessions = Array.isArray(state.sessions) ? state.sessions : [];
    const configuredAccounts = Array.isArray(state.accounts) ? state.accounts : [];
    // 中文注释：本区展示的是额度窗口，所以没有任何额度窗口的订阅（DeepSeek Harness、
    // Claude Code 这类没有额度概念的 provider）整张卡片都不出现，计数也只算显示出来的
    // 订阅——否则会看到一排「0 个窗口」的空卡片。
    const accountsWithQuota = new Set(
      quotas
        .filter((quota) => (quota.windows || []).length > 0)
        .map((quota) => quota.account || 'codex')
    );
    const names = [...new Set([
      ...configuredAccounts.map((account) => account.name),
      ...quotas.map((quota) => quota.account || 'codex'),
      ...sessions.map((session) => session.account || 'codex')
    ])].filter((name) => accountsWithQuota.has(name));
    document.getElementById('quota-source').textContent = names.length ? `${names.length} 个账号` : '暂无额度订阅';
    document.getElementById('account-section-count').textContent = names.length ? `${names.length} 个账号` : '暂无额度订阅';
    if (names.length === 0) {
      container.innerHTML = '<div class="empty-state"><span class="empty-title">没有带额度窗口的订阅</span><span class="empty-hint">没有额度概念的 provider（例如 DeepSeek Harness、Claude Code）不在本区显示，它们的使用量仍计入「用量与成本估算」。</span></div>';
      return;
    }
    container.innerHTML = names.map((name) => {
      const account = configuredAccounts.find((item) => item.name === name) || {};
      const accountQuotas = quotas.filter((quota) => (quota.account || 'codex') === name);
      const accountSessions = sessions.filter((session) => (session.account || 'codex') === name);
      const accountCounts = account.counts || {};
      const profiles = (account.profiles || []).map((profile) => typeof profile === 'string' ? profile : profile.name).filter((profile) => profile).join(' · ');
      const accountId = account.account_id || name;
      const product = accountProductLabel(account);
      const plans = distinctSorted(accountQuotas.map((quota) => planLabel(quota.plan_type)).concat([planLabel(account.plan_type)]));
      const subscription = plans.length ? `${product} · ${plans.join(' / ')}` : product;
      const activeCount = accountCounts.active ?? accountSessions.length;
      const accountSource = accountQuotas.map((quota) => quota.source).filter((source) => source).join(' · ');
      return `<article class="account-block">
        <div class="account-heading">
          <div class="account-identity"><div class="account-avatar" aria-hidden="true" title="${escapeHtml(subscription)}">${escapeHtml(subscriptionInitials(subscription))}</div><div><div class="account-label">订阅</div><h3 class="account-title">${escapeHtml(subscription)}</h3><div class="account-meta">Account ID：<span class="mono">${escapeHtml(accountId)}</span>${profiles ? ` · Profile：${escapeHtml(profiles)}` : ''}${accountSource ? ` · 来源：${escapeHtml(accountSource)}` : ''}</div></div></div>
          <div class="account-side"><span class="account-activity">${escapeHtml(activeCount)} 个活动</span></div>
        </div>
        <div class="account-subtitle"><span>额度窗口</span><span class="muted">${accountQuotas.length} 个窗口</span></div>
        <div class="quota-rows-block">${renderQuotaRows(accountQuotas)}</div>
        <div class="account-subtitle"><span>活动会话</span><span class="muted">${activeCount} 个活动</span></div>
        ${renderSessionTable(accountSessions, name)}
      </article>`;
    }).join('');
    container.querySelectorAll('[data-archive-session]').forEach((button) => {
      button.addEventListener('click', () => {
        const path = button.dataset.archiveSession || '';
        if (!path) return;
        if (!window.confirm(`确认把这个会话压缩归档（tar.gz）并删除原文件？可在「磁盘与会话管理」里恢复。\n${path}`)) return;
        archiveSession(path);
      });
    });
    container.querySelectorAll('[data-session-toggle]').forEach((button) => {
      button.addEventListener('click', () => {
        const key = button.dataset.sessionToggle || '';
        const wrap = button.previousElementSibling;
        const willExpand = !expandedSessionTables.has(key);
        if (willExpand) {
          expandedSessionTables.add(key);
        } else {
          expandedSessionTables.delete(key);
        }
        if (wrap) wrap.style.display = willExpand ? '' : 'none';
        button.setAttribute('aria-expanded', String(willExpand));
        const rows = wrap ? wrap.querySelectorAll('tbody tr').length : 0;
        button.textContent = willExpand ? '收起会话列表' : `展开 ${rows} 个活动会话`;
      });
    });
  };
  // 运行健康徽标：读取 state.health，可点击展开未正常组件的明细。
  const healthStatusMeta = {
    ok: { label: '正常', className: 'ok' },
    starting: { label: '启动中', className: 'starting' },
    degraded: { label: '部分降级', className: 'degraded' },
    failed: { label: '异常', className: 'failed' }
  };
  const healthComponentStatusLabel = { ok: '正常', starting: '启动中', degraded: '数据过期', failed: '失败' };
  const unhealthyComponents = (health) => {
    const components = health && Array.isArray(health.components) ? health.components : [];
    return components.filter((component) => component && component.status !== 'ok');
  };
  const renderHealthPanel = () => {
    const panel = document.getElementById('health-detail');
    if (!panel) return;
    const unhealthy = unhealthyComponents(latestHealthState);
    if (!unhealthy.length) {
      panel.innerHTML = '';
      panel.style.display = 'none';
      return;
    }
    const items = unhealthy.map((component) => {
      const statusLabel = healthComponentStatusLabel[component.status] || component.status || '未知';
      const successAt = component.last_success_at ? `${formatRelativeTime(component.last_success_at)}（${formatTime(component.last_success_at)}）` : '从未成功';
      const errorNote = component.last_error ? `<div class="cell-sub">${escapeHtml(component.last_error)}</div>` : '';
      return `<li><strong>${escapeHtml(component.label || component.key)}</strong> · ${escapeHtml(statusLabel)} · 上次成功 ${escapeHtml(successAt)}${errorNote}</li>`;
    }).join('');
    panel.innerHTML = `<div>以下组件未处于正常状态：</div><ul>${items}</ul>`;
  };
  const renderHealth = (state) => {
    const badge = document.getElementById('health-indicator');
    const panel = document.getElementById('health-detail');
    if (!badge) return;
    latestHealthState = state && state.health && typeof state.health === 'object' ? state.health : null;
    if (!latestHealthState || !latestHealthState.overall) {
      badge.style.display = 'none';
      badge.setAttribute('aria-expanded', 'false');
      if (panel) {
        panel.innerHTML = '';
        panel.style.display = 'none';
      }
      return;
    }
    const meta = healthStatusMeta[latestHealthState.overall] || healthStatusMeta.failed;
    badge.className = `health-badge ${meta.className}`;
    badge.innerHTML = `<span class="health-dot"></span><span>${escapeHtml(meta.label)}</span>`;
    badge.style.display = '';
    badge.title = `运行健康：${meta.label}（点击查看组件明细）`;
    if (panel && panel.style.display !== 'none') {
      renderHealthPanel();
      if (panel.innerHTML) {
        panel.style.display = 'block';
      }
    }
  };
  const refresh = async () => {
    const refreshButton = document.getElementById('refresh-button');
    if (refreshButton) {
      refreshButton.disabled = true;
      refreshButton.classList.add('is-spinning');
    }
    try {
      const response = await fetch('/api/state', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const state = await response.json();
      latestState = state;
      const counts = state.counts || {};
      const accounts = Array.isArray(state.accounts) ? state.accounts : [];
      document.getElementById('active-count').textContent = counts.active ?? 0;
      document.getElementById('process-count').textContent = counts.process_backed ?? 0;
      document.getElementById('quota-window-count').textContent = Array.isArray(state.quotas) ? state.quotas.reduce((total, quota) => total + (Array.isArray(quota.windows) ? quota.windows.length : 0), 0) : 0;
      document.getElementById('accounts-online').textContent = accounts.length;
      if (!latestUsageState) document.getElementById('spend-count').textContent = '按需加载';
      const trafficTotals = (state.traffic && state.traffic.totals) || {};
      const uploadBurst = document.getElementById('upload-burst-count');
      const uploadAlerts = document.getElementById('upload-alert-count');
      if (uploadBurst) uploadBurst.textContent = formatDataSize(trafficTotals.burst_bytes || 0);
      if (uploadAlerts) {
        const alertHistory = state.alert_history || {};
        uploadAlerts.textContent = alertHistory.available ? (alertHistory.unread ?? 0) : (trafficTotals.alert_count ?? 0);
      }
      document.getElementById('service-sync').textContent = `已同步 ${formatTime(state.updated_at)}`;
      document.getElementById('service-state').textContent = '监控服务在线';
      renderHealth(state);
      document.querySelectorAll('.status-dot').forEach((dot) => dot.classList.remove('error'));
      renderAccounts(state);
      renderTraffic(state);
      renderHousekeeping(state);
      renderSectionSummaries(state);
      renderAlerts();
      if (alertHistoryState) refreshAlertHistory();
      document.getElementById('error').style.display = 'none';
    } catch (error) {
      const box = document.getElementById('error');
      box.textContent = `读取监控状态失败：${error.message}`;
      box.style.display = 'block';
      document.getElementById('service-state').textContent = '监控服务连接异常';
      document.getElementById('service-sync').textContent = '同步失败';
      document.querySelectorAll('.status-dot').forEach((dot) => dot.classList.add('error'));
    } finally {
      if (refreshButton) {
        refreshButton.disabled = false;
        refreshButton.classList.remove('is-spinning');
      }
    }
  };
  document.getElementById('refresh-button')?.addEventListener('click', refresh);
  document.getElementById('health-indicator')?.addEventListener('click', () => {
    const badge = document.getElementById('health-indicator');
    const panel = document.getElementById('health-detail');
    if (!badge || !panel) return;
    const expanded = panel.style.display !== 'none';
    if (expanded) {
      panel.style.display = 'none';
      badge.setAttribute('aria-expanded', 'false');
      return;
    }
    renderHealthPanel();
    const visible = Boolean(panel.innerHTML);
    panel.style.display = visible ? 'block' : 'none';
    badge.setAttribute('aria-expanded', String(visible));
  });
  document.getElementById('usage-load-button')?.addEventListener('click', refreshUsage);
  document.getElementById('insights-load-button')?.addEventListener('click', refreshInsights);
  document.getElementById('alert-history-load-button')?.addEventListener('click', () => {
    alertHistoryLimit = 50;
    refreshAlertHistory();
  });
  ['alert-range-filter', 'alert-level-filter', 'alert-kind-filter', 'alert-ack-filter'].forEach((id) => {
    document.getElementById(id)?.addEventListener('change', () => {
      alertHistoryLimit = 50;
      refreshAlertHistory();
    });
  });
  document.getElementById('alert-keyword-filter')?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      alertHistoryLimit = 50;
      refreshAlertHistory();
    }
  });
  document.getElementById('alert-ack-all-button')?.addEventListener('click', () => mutateAlerts('ack', { all: true }));
  document.getElementById('alert-clear-button')?.addEventListener('click', clearAlertHistory);
  document.getElementById('usage-search-load-button')?.addEventListener('click', () => {
    usageSearchLimit = 50;
    refreshUsageSearch();
  });
  document.getElementById('usage-search-refresh-button')?.addEventListener('click', () => {
    usageSearchLimit = 50;
    refreshUsageSearch();
  });
  document.querySelectorAll('[data-usage-search-group]').forEach((tab) => {
    tab.addEventListener('click', () => {
      usageSearchGroup = tab.dataset.usageSearchGroup || 'session';
      document.querySelectorAll('[data-usage-search-group]').forEach((item) => item.classList.toggle('selected', item === tab));
      usageSearchLimit = 50;
      refreshUsageSearch();
    });
  });
  ['usage-search-range', 'usage-search-model', 'usage-search-account', 'usage-search-sort', 'usage-search-from', 'usage-search-to'].forEach((id) => {
    document.getElementById(id)?.addEventListener('change', () => {
      usageSearchLimit = 50;
      refreshUsageSearch();
    });
  });
  document.getElementById('usage-search-keyword')?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      usageSearchLimit = 50;
      refreshUsageSearch();
    }
  });
  document.getElementById('housekeeping-scan-button')?.addEventListener('click', refreshHousekeeping);
  document.getElementById('housekeeping-preview-button')?.addEventListener('click', refreshHousekeeping);
  document.getElementById('housekeeping-archive-button')?.addEventListener('click', () => {
    const criteria = housekeepingCriteria();
    if (!window.confirm(`确认把 ${criteria.days} 天前、非活动的 Codex 会话压缩归档（tar.gz）并删除原文件？归档在后台执行，可在本页恢复。`)) return;
    mutateHousekeeping('archive');
  });
  document.getElementById('housekeeping-clean-button')?.addEventListener('click', () => {
    const criteria = housekeepingCriteria();
    if (!window.confirm(`确认直接删除 ${criteria.days} 天前、非活动的 Codex 会话文件？删除在后台执行且不可撤销，建议先归档。`)) return;
    mutateHousekeeping('clean');
  });
  ['housekeeping-days', 'housekeeping-min-size'].forEach((id) => {
    document.getElementById(id)?.addEventListener('change', refreshHousekeeping);
  });
  document.querySelectorAll('[data-insights-days]').forEach((tab) => {
    tab.addEventListener('click', () => {
      insightsPeriodDays = tab.dataset.insightsDays || '';
      document.querySelectorAll('[data-insights-days]').forEach((item) => item.classList.toggle('selected', item === tab));
      latestInsightsState = null;
      refreshInsights();
    });
  });
  DEFAULT_COLLAPSED.forEach(applySectionState);
  collapsedSections.forEach((id) => applySectionState(id));
  document.querySelectorAll('[data-section-toggle]').forEach((toggle) => {
    toggle.addEventListener('click', () => {
      const id = toggle.dataset.sectionToggle;
      setSectionCollapsed(id, !collapsedSections.has(id));
    });
  });
  navLinks.forEach((link) => {
    link.addEventListener('click', () => {
      const id = link.dataset.navTarget;
      if (collapsedSections.has(id)) setSectionCollapsed(id, false);
    });
  });
__THEME_SCRIPT__
  refresh();
  ['alert-history', 'usage-search', 'housekeeping'].forEach(ensureSectionLoaded);
  window.setInterval(refresh, 5000);
</script>
</body>
</html>
"""
))


# 中文注释：独立设置页与主页共用 _BASE_CSS / _RESPONSIVE_CSS，只追加设置页独有样式；
# 设置子块（如扫描目录）平级放在 #settings-body 内，之后可直接追加新的设置项。
_SETTINGS_HTML = _apply_theme_parts((
    r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
__FAVICON__
__THEME_BOOT__
  <title>设置 - Token Monitor</title>
  <style>
"""
    + _BASE_CSS
    + _SETTINGS_CSS
    + _RESPONSIVE_CSS
    + r"""  </style>
</head>
<body>
<div class="app-shell">
  <aside class="sidebar" aria-label="设置导航">
    <div class="sidebar-brand">
      <div class="brand-mark" aria-hidden="true">__BRAND_MARK__</div>
      <div><div class="brand-name">Token <span>Monitor</span></div><small>本地 code agent 控制台</small></div>
    </div>
    <div class="sidebar-label">工作台</div>
    <nav>
      <a class="sidebar-link" href="/"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 12H4"/><path d="M11 5l-7 7 7 7"/></svg><span>返回 Dashboard</span></a>
    </nav>
    <div class="sidebar-label">设置项</div>
    <nav>
      <a class="sidebar-link active" href="#scan-dirs"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7 7 0 0 0-2-1.2L14 3h-4l-.5 2.6a7 7 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6A7 7 0 0 0 5 12c0 .4 0 .8.1 1.2l-2 1.6 2 3.4 2.4-1a7 7 0 0 0 2 1.2L10 21h4l.5-2.6a7 7 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2z"/></svg><span>扫描目录</span></a>
      <a class="sidebar-link" href="#history-settings"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 7v5l3 2"/><circle cx="12" cy="12" r="8"/></svg><span>历史数据</span></a>
    </nav>
    <div class="sidebar-bottom">
      <div class="sidebar-foot">修改立即生效 · 只记录元数据</div>
    </div>
  </aside>

  <main>
    <header class="topbar">
      <div class="breadcrumb"><span>Token Monitor</span><span class="breadcrumb-separator">/</span><strong>设置</strong></div>
      <div class="topbar-actions">
__THEME_TOGGLE__
        <button id="scan-dirs-refresh-button" class="refresh-button" type="button" aria-label="刷新扫描目录"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14.7-4L4 9"/><path d="M4 4v5h5"/><path d="M4 13a8 8 0 0 0 14.7 4L20 15"/><path d="M20 20v-5h-5"/></svg><span>刷新</span></button>
      </div>
    </header>

    <section class="page-hero">
      <div>
        <div class="eyebrow">Settings</div>
        <h1>设置</h1>
        <p class="hero-description">Dashboard 的在线设置项。当前包含扫描目录管理和历史数据管理；之后的设置项会以平级子块追加到本页。</p>
      </div>
    </section>

    <div id="error" class="error" role="alert"></div>

    <div id="settings-body">
      <section id="scan-dirs" class="panel settings-block">
        <div class="panel-heading">
          <div><div class="section-kicker">Scan directories</div><h2>扫描目录</h2><p class="section-description">管理各 code agent 的扫描目录，优先级：Web 配置 &gt; 命令行参数 &gt; 自动探测。目录需已存在、可读且在主目录内；保存后立即生效并热重载账号。</p></div>
          <div class="section-meta"><span class="section-count" id="scan-dirs-count">等待加载</span></div>
        </div>
        <div id="scan-dirs-content"><div class="empty-state"><span class="empty-title">正在读取扫描目录…</span></div></div>
      </section>
      <section id="history-settings" class="panel settings-block">
        <div class="panel-heading">
          <div><div class="section-kicker">History data</div><h2>历史数据</h2><p class="section-description">管理用量索引、会话历史与告警历史的保留期和手动清理，优先级：Web 配置 &gt; 命令行参数 &gt; 默认值。清理只删过期历史行，活动会话与额度记录始终保留。</p></div>
          <div class="section-meta"><span class="section-count" id="history-count">等待加载</span></div>
        </div>
        <div id="history-content"><div class="empty-state"><span class="empty-title">正在读取历史数据状态…</span></div></div>
        <div id="history-preview-content"></div>
        <div id="history-result"></div>
      </section>
    </div>
  </main>
</div>
<script>
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
  const formatTime = (seconds) => {
    if (seconds === null || seconds === undefined) return '未知';
    return new Date(Number(seconds) * 1000).toLocaleString();
  };
  const formatDataSize = (value) => {
    const amount = Math.max(0, Number(value || 0));
    if (amount < 1024) return `${Math.round(amount)} B`;
    if (amount < 1024 * 1024) return `${(amount / 1024).toFixed(amount >= 10240 ? 0 : 1)} KiB`;
    if (amount < 1024 * 1024 * 1024) return `${(amount / 1024 / 1024).toFixed(amount >= 10 * 1024 * 1024 ? 1 : 2)} MiB`;
    return `${(amount / 1024 / 1024 / 1024).toFixed(2)} GiB`;
  };
  // 扫描目录：展示每个 provider 的生效目录，支持在线添加、移除和恢复默认。
  const scanDirsSourceLabel = (source) => ({ web: 'Web 配置', cli: '命令行', auto: '自动探测' })[source] || source || '未知';
  const scanDirsSourceClass = (source) => source === 'web' ? 'ok' : source === 'cli' ? 'warn' : 'other';
  const renderScanDirs = (payload) => {
    const container = document.getElementById('scan-dirs-content');
    const countLabel = document.getElementById('scan-dirs-count');
    if (!container) return;
    if (!payload || payload.available === false) {
      container.innerHTML = '<div class="empty-state"><span class="empty-title">该运行模式不支持在线管理扫描目录</span><span class="empty-hint">请以 daemon 或 service 模式运行。</span></div>';
      if (countLabel) countLabel.textContent = '不可用';
      return;
    }
    const providers = Array.isArray(payload.providers) ? payload.providers : [];
    const dirCount = providers.reduce((total, provider) => total + (Array.isArray(provider.directories) ? provider.directories.length : 0), 0);
    if (countLabel) countLabel.textContent = `${providers.length} 个来源 · ${dirCount} 个目录`;
    const cards = providers.map((provider) => {
      const directories = Array.isArray(provider.directories) ? provider.directories : [];
      const rows = directories.map((directory) => {
        const statusPill = !directory.ok
          ? '<span class="pill danger">不可读或不存在</span>'
          : directory.structure_ok
            ? '<span class="pill ok">正常</span>'
            : '<span class="pill warn">结构存疑</span>';
        const notes = [...(directory.errors || []), ...(directory.warnings || [])];
        const note = notes.length ? `<div class="cell-sub">${notes.map(escapeHtml).join('；')}</div>` : '';
        return `<tr>
          <td><span class="mono">${escapeHtml(directory.path)}</span>${note}</td>
          <td>${statusPill}</td>
          <td><button class="btn mini danger" type="button" data-scan-dirs-remove="${escapeHtml(provider.key)}" data-scan-dirs-path="${escapeHtml(directory.path)}">移除</button></td>
        </tr>`;
      }).join('');
      const table = directories.length
        ? `<div class="table-wrap"><table class="tight"><thead><tr><th>目录</th><th>状态</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : '<div class="empty-state"><span class="empty-title">当前没有生效的扫描目录</span></div>';
      const disabled = provider.enabled ? '' : ' <span class="pill other">已禁用</span>';
      const reset = Array.isArray(provider.override_dirs)
        ? `<button class="btn mini warn" type="button" data-scan-dirs-reset="${escapeHtml(provider.key)}">恢复默认</button>`
        : '';
      return `<article class="mini-card">
        <div class="mini-card-head">
          <div><div class="mini-card-title">${escapeHtml(provider.name)}${disabled}</div><div class="mini-card-path">默认 ${escapeHtml(provider.default_dir)} · 也可用 ${escapeHtml(provider.cli_option)} 指定</div></div>
          <span class="pill ${scanDirsSourceClass(provider.source)}">${escapeHtml(scanDirsSourceLabel(provider.source))}</span>
        </div>
        ${table}
        <div class="criteria-row">
          <label class="field wide">新增目录<input type="text" data-scan-dirs-input="${escapeHtml(provider.key)}" placeholder="例如 ~/.codex-work"></label>
          <div class="toolbar-actions">
            <button class="btn mini primary" type="button" data-scan-dirs-add="${escapeHtml(provider.key)}">添加</button>
            ${reset}
          </div>
        </div>
      </article>`;
    }).join('');
    container.innerHTML = cards ? `<div class="card-grid">${cards}</div>` : '<div class="empty-state"><span class="empty-title">没有已知的 provider</span></div>';
    container.querySelectorAll('[data-scan-dirs-add]').forEach((button) => {
      button.addEventListener('click', () => {
        const provider = button.dataset.scanDirsAdd || '';
        const input = container.querySelector(`[data-scan-dirs-input="${provider}"]`);
        const path = input ? String(input.value || '').trim() : '';
        if (!path) {
          window.alert('请先填写要添加的目录路径。');
          return;
        }
        mutateScanDirs({ action: 'add', provider, path });
      });
    });
    container.querySelectorAll('[data-scan-dirs-remove]').forEach((button) => {
      button.addEventListener('click', () => {
        const path = button.dataset.scanDirsPath || '';
        if (!window.confirm(`确认从扫描目录中移除？\n${path}`)) return;
        mutateScanDirs({ action: 'remove', provider: button.dataset.scanDirsRemove || '', path, confirm: true });
      });
    });
    container.querySelectorAll('[data-scan-dirs-reset]').forEach((button) => {
      button.addEventListener('click', () => {
        if (!window.confirm('确认恢复该 provider 的默认扫描目录？Web 配置将被清除。')) return;
        mutateScanDirs({ action: 'reset', provider: button.dataset.scanDirsReset || '', confirm: true });
      });
    });
  };
  const refreshScanDirs = async () => {
    const container = document.getElementById('scan-dirs-content');
    const button = document.getElementById('scan-dirs-refresh-button');
    if (button) {
      button.disabled = true;
      button.classList.add('is-spinning');
    }
    try {
      const response = await fetch('/api/scan-dirs', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      renderScanDirs(await response.json());
    } catch (error) {
      if (container) container.innerHTML = `<div class="empty-state"><span class="empty-title">读取扫描目录失败</span><span class="empty-hint">${escapeHtml(error.message)}</span></div>`;
    } finally {
      if (button) {
        button.disabled = false;
        button.classList.remove('is-spinning');
      }
    }
  };
  const mutateScanDirs = async (body) => {
    const container = document.getElementById('scan-dirs-content');
    try {
      const response = await fetch('/api/scan-dirs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || `HTTP ${response.status}`);
      renderScanDirs(payload);
    } catch (error) {
      if (container) container.innerHTML = `<div class="empty-state"><span class="empty-title">操作失败</span><span class="empty-hint">${escapeHtml(error.message)}</span></div>`;
    }
  };
  // 历史数据：保留期配置、索引占用、清理预览与手动清理。
  const historySourceLabel = (source) => ({ web: 'Web 配置', cli: '配置默认' })[source] || source || '未知';
  const historySourceClass = (source) => source === 'web' ? 'ok' : 'other';
  const historyKindLabel = { usage: '用量历史', sessions: '会话历史', alerts: '告警历史' };
  const historyDeletedTotal = (deleted) => ['usage', 'sessions', 'alerts']
    .reduce((sum, key) => sum + Number((deleted || {})[key] || 0), 0);
  const renderHistory = (payload) => {
    const container = document.getElementById('history-content');
    const countLabel = document.getElementById('history-count');
    if (!container) return;
    if (!payload || payload.available === false) {
      container.innerHTML = '<div class="empty-state"><span class="empty-title">该运行模式不支持在线管理历史数据</span><span class="empty-hint">请以 daemon 或 service 模式运行。</span></div>';
      if (countLabel) countLabel.textContent = '不可用';
      return;
    }
    const retention = payload.retention || {};
    const days = payload.retention_days || {};
    const dbs = Array.isArray(payload.dbs) ? payload.dbs : [];
    const totalBytes = dbs.reduce((sum, item) => sum + Number(item.bytes || 0), 0);
    if (countLabel) countLabel.textContent = `索引共 ${formatDataSize(totalBytes)}`;
    const retentionField = (key, label) => {
      const entry = retention[key] || {};
      const value = entry.value ?? days[key] ?? '';
      const badge = entry.source
        ? ` <span class="pill ${historySourceClass(entry.source)}">${escapeHtml(historySourceLabel(entry.source))}</span>`
        : '';
      return `<label class="field wide">${escapeHtml(label)}${badge}<input type="number" min="1" max="3650" step="1" data-history-days="${key}" value="${escapeHtml(String(value))}"></label>`;
    };
    const dbRows = dbs.map((item) => `<tr><td>${escapeHtml(item.label)}</td><td>${escapeHtml(formatDataSize(item.bytes))}</td></tr>`).join('');
    const lastCleanup = payload.last_cleanup;
    const lastCleanupLine = lastCleanup
      ? `上次清理：${escapeHtml(formatTime(lastCleanup.observed_at))} · 删除 ${historyDeletedTotal(lastCleanup.deleted)} 行 · 释放 ${escapeHtml(formatDataSize(lastCleanup.freed_bytes || 0))}${Array.isArray(lastCleanup.errors) && lastCleanup.errors.length ? ' · 存在部分失败' : ''}`
      : '尚未执行过清理';
    container.innerHTML = `
      <div class="account-subtitle"><span>保留天数</span></div>
      <div class="criteria-row">
        ${retentionField('usage_days', '用量历史保留天数')}
        ${retentionField('session_days', '会话历史保留天数')}
        <div class="toolbar-actions">
          <button class="btn mini primary" type="button" id="history-save-button">保存</button>
          <button class="btn mini warn" type="button" id="history-reset-button">恢复默认</button>
        </div>
      </div>
      <div class="usage-note">告警历史保留 ${escapeHtml(String(days.alert_days ?? '—'))} 天（只读，由运行配置决定）。</div>
      <div class="account-subtitle"><span>索引与状态数据占用</span></div>
      <div class="table-wrap"><table class="tight"><thead><tr><th>数据</th><th>占用</th></tr></thead><tbody>${dbRows || '<tr><td colspan="2">暂无数据</td></tr>'}</tbody></table></div>
      <div class="criteria-row">
        <div class="toolbar-actions">
          <button class="btn mini" type="button" id="history-preview-button">预览将清理的数据</button>
          <button class="btn mini danger" type="button" id="history-cleanup-button">立即清理</button>
        </div>
      </div>
      <div class="usage-note" id="history-last-cleanup">${lastCleanupLine}</div>`;
    document.getElementById('history-save-button')?.addEventListener('click', () => {
      const body = { action: 'set-retention' };
      [['usage_days', '[data-history-days="usage_days"]'], ['session_days', '[data-history-days="session_days"]']].forEach(([key, selector]) => {
        const input = container.querySelector(selector);
        const raw = input ? String(input.value || '').trim() : '';
        const value = Number(raw);
        if (raw !== '' && Number.isFinite(value)) body[key] = value;
      });
      if (body.usage_days === undefined && body.session_days === undefined) {
        window.alert('请填写要保存的保留天数。');
        return;
      }
      mutateHistory(body);
    });
    document.getElementById('history-reset-button')?.addEventListener('click', () => {
      if (!window.confirm('确认恢复默认保留天数？Web 配置将被清除。')) return;
      mutateHistory({ action: 'reset-retention', confirm: true });
    });
    document.getElementById('history-preview-button')?.addEventListener('click', refreshHistoryPreview);
    document.getElementById('history-cleanup-button')?.addEventListener('click', () => {
      if (!window.confirm('确认立即清理过期历史数据？删除不可撤销，活动会话和额度恢复记录会保留。')) return;
      mutateHistory({ action: 'cleanup', confirm: true });
    });
  };
  const renderHistoryPreview = (preview) => {
    const container = document.getElementById('history-preview-content');
    if (!container) return;
    const kinds = preview && Array.isArray(preview.kinds) ? preview.kinds : [];
    if (!kinds.length) {
      container.innerHTML = '';
      return;
    }
    const rows = kinds.map((kind) => `<tr>
      <td>${escapeHtml(historyKindLabel[kind.kind] || kind.kind)}</td>
      <td>${escapeHtml(formatTime(kind.cutoff))}</td>
      <td>${Number(kind.rows_to_delete || 0).toLocaleString('zh-CN')} / ${Number(kind.total_rows || 0).toLocaleString('zh-CN')}</td>
      <td>${escapeHtml(formatDataSize(kind.db_bytes))}</td>
      <td>${escapeHtml(formatDataSize(kind.estimated_free_bytes))}（预计）</td>
    </tr>`).join('');
    container.innerHTML = `
      <div class="account-subtitle"><span>将清理的数据（预计）</span></div>
      <div class="table-wrap"><table class="tight"><thead><tr><th>类别</th><th>截止时间</th><th>将删行数 / 总行数</th><th>当前占用</th><th>预计释放</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="usage-note">预计释放合计 ${escapeHtml(formatDataSize(preview.estimated_free_bytes || 0))}，为按行数比例的估算值；实际释放以清理结果为准。</div>`;
  };
  const renderHistoryCleanupResult = (result, errorMessage) => {
    const container = document.getElementById('history-result');
    if (!container) return;
    const deleted = result && result.deleted && typeof result.deleted === 'object' ? result.deleted : {};
    const breakdown = Object.keys(deleted)
      .map((key) => `${escapeHtml(historyKindLabel[key] || key)} ${Number(deleted[key] || 0)} 行`)
      .join(' · ');
    const errors = result && Array.isArray(result.errors) ? result.errors : [];
    const title = errorMessage ? '清理部分失败' : '清理完成';
    container.innerHTML = `<div class="empty-state"><span class="empty-title">${title}：删除 ${historyDeletedTotal(deleted)} 行，释放 ${escapeHtml(formatDataSize(result ? result.freed_bytes || 0 : 0))}</span>${breakdown ? `<span class="empty-hint">${breakdown}</span>` : ''}${errorMessage ? `<span class="empty-hint">${escapeHtml(errorMessage)}</span>` : ''}${errors.length ? `<span class="empty-hint">${errors.map(escapeHtml).join('；')}</span>` : ''}</div>`;
  };
  const refreshHistory = async () => {
    const container = document.getElementById('history-content');
    try {
      const response = await fetch('/api/history', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      renderHistory(await response.json());
    } catch (error) {
      if (container) container.innerHTML = `<div class="empty-state"><span class="empty-title">读取历史数据状态失败</span><span class="empty-hint">${escapeHtml(error.message)}</span></div>`;
    }
  };
  const refreshHistoryPreview = async () => {
    const container = document.getElementById('history-preview-content');
    try {
      const response = await fetch('/api/history?preview=1', { cache: 'no-store' });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || `HTTP ${response.status}`);
      renderHistoryPreview(payload.preview);
    } catch (error) {
      if (container) container.innerHTML = `<div class="empty-state"><span class="empty-title">预览失败</span><span class="empty-hint">${escapeHtml(error.message)}</span></div>`;
    }
  };
  const mutateHistory = async (body) => {
    const resultBox = document.getElementById('history-result');
    try {
      const response = await fetch('/api/history', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const payload = await response.json();
      if (!response.ok) {
        const error = new Error(payload.message || `HTTP ${response.status}`);
        error.payload = payload;
        throw error;
      }
      await refreshHistory();
      if (body.action === 'cleanup') renderHistoryCleanupResult(payload.result, null);
      if (body.action !== 'cleanup' && resultBox) resultBox.innerHTML = '';
    } catch (error) {
      if (body.action === 'cleanup') {
        renderHistoryCleanupResult(error.payload ? error.payload.result : null, error.message);
      } else if (resultBox) {
        resultBox.innerHTML = `<div class="empty-state"><span class="empty-title">操作失败</span><span class="empty-hint">${escapeHtml(error.message)}</span></div>`;
      }
    }
  };
  document.getElementById('scan-dirs-refresh-button')?.addEventListener('click', () => {
    refreshScanDirs();
    refreshHistory();
  });
__THEME_SCRIPT__
  refreshScanDirs();
  refreshHistory();
</script>
</body>
</html>
"""
))


@dataclass(frozen=True)
class DashboardConfig:
    """Dashboard HTTP 服务配置。"""

    host: str = "127.0.0.1"
    port: int = 8765
    budget_usd: float | None = None

    def __post_init__(self) -> None:
        """校验监听地址和端口。"""

        if not self.host.strip():
            raise ValueError("dashboard host 不能为空")
        if not 0 <= self.port <= 65535:
            raise ValueError("dashboard port 必须在 0 到 65535 之间")
        if self.budget_usd is not None and self.budget_usd <= 0:
            raise ValueError("budget_usd 必须大于 0")


class _DashboardHTTPServer(ThreadingHTTPServer):
    """允许快速重启且不让请求线程阻塞主监控退出的 HTTP 服务。"""

    allow_reuse_address = True
    daemon_threads = True


class _AccountSet:
    """Dashboard 运行期间可热替换的注册表与账号元数据集合（线程安全）。"""

    def __init__(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        account_metadata: Mapping[str, Mapping[str, str | None]],
    ) -> None:
        self._lock = Lock()
        self._registries = dict(registries)
        self._account_metadata = dict(account_metadata)

    def snapshot(
        self,
    ) -> tuple[
        dict[str, MultiSessionRegistry],
        dict[str, Mapping[str, str | None]],
    ]:
        """返回当前生效的注册表与元数据副本，保证单次请求读到一致的组合。"""

        with self._lock:
            return dict(self._registries), dict(self._account_metadata)

    def update(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        account_metadata: Mapping[str, Mapping[str, str | None]] | None,
    ) -> None:
        """原子替换注册表与账号元数据；之后的请求立即使用新账号集合。"""

        with self._lock:
            self._registries = dict(registries)
            self._account_metadata = dict(account_metadata or {})


class DashboardServer:
    """提供 Dashboard 状态、用量数据和告警历史的本地 HTTP 服务。"""

    def __init__(
        self,
        registry: MultiSessionRegistry | None = None,
        config: DashboardConfig | None = None,
        logger: logging.Logger | None = None,
        registries: Mapping[str, MultiSessionRegistry] | None = None,
        account_metadata: Mapping[str, Mapping[str, str | None]] | None = None,
        usage_aggregator: UsageAggregator | None = None,
        grok_homes: Sequence[Path] | None = None,
        kimi_homes: Sequence[Path] | None = None,
        dsh_homes: Sequence[Path] | None = None,
        commandcode_homes: Sequence[Path] | None = None,
        claude_homes: Sequence[Path] | None = None,
        traffic_monitor: TrafficMonitor | None = None,
        alert_store: TrafficAlertStore | None = None,
        housekeeping: HousekeepingMonitor | None = None,
        session_thresholds: SessionSwitchThresholds | None = None,
        scan_dirs: ScanDirsController | None = None,
        health: HealthTracker | None = None,
        history: HistoryDataManager | None = None,
        retention: RetentionController | None = None,
    ) -> None:
        if registries is not None and registry is not None:
            raise ValueError("registry 和 registries 只能传入一个")
        if registry is not None:
            registries = {"codex": registry}
        # 中文注释：没有 Codex 账号时允许空注册表，Dashboard 仍然展示
        # Grok / Kimi / DSH / Claude Code / Command Code 的状态。
        self.registries = dict(registries or {})
        self.account_metadata = dict(account_metadata or {})
        self.registry = next(iter(self.registries.values()), None)
        self.config = config or DashboardConfig()
        self.logger = logger or logging.getLogger(__name__)
        self.grok_homes = resolve_grok_homes(grok_homes)
        self.kimi_homes = resolve_kimi_homes(kimi_homes)
        self.dsh_homes = resolve_dsh_homes(dsh_homes)
        self.commandcode_homes = resolve_commandcode_homes(commandcode_homes)
        self.claude_homes = resolve_claude_homes(claude_homes)
        self.traffic_monitor = traffic_monitor
        self.alert_store = alert_store
        self.housekeeping = housekeeping
        self.scan_dirs = scan_dirs
        self.health = health
        self.history = history
        self.retention = retention
        self.session_thresholds = session_thresholds or SessionSwitchThresholds()
        self.usage_aggregator = usage_aggregator or UsageAggregator(
            grok_homes=self.grok_homes,
            kimi_homes=self.kimi_homes,
            dsh_homes=self.dsh_homes,
            claude_homes=self.claude_homes,
        )
        # 中文注释：handler 闭包只持有这个容器；扫描目录变化导致账号增减时
        # 由 update_accounts 热替换内容，无需重启 HTTP 服务。
        self._accounts = _AccountSet(self.registries, self.account_metadata)
        self._server: _DashboardHTTPServer | None = None
        self._thread: Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        """返回实际监听地址；端口为 0 时返回系统分配的端口。"""

        if self._server is None:
            return self.config.host, self.config.port
        raw_host, raw_port = self._server.server_address[:2]
        return str(raw_host), int(raw_port)

    def update_accounts(
        self,
        registries: Mapping[str, MultiSessionRegistry],
        account_metadata: Mapping[str, Mapping[str, str | None]] | None,
    ) -> None:
        """热替换注册表与账号元数据；扫描目录调整后账号集合会随之变化。"""

        self.registries = dict(registries)
        self.account_metadata = dict(account_metadata or {})
        self.registry = next(iter(self.registries.values()), None)
        self._accounts.update(self.registries, self.account_metadata)

    def start(self) -> None:
        """绑定地址并启动 Dashboard 请求线程。"""

        if self._server is not None:
            return
        handler = _make_handler(
            self._accounts,
            self.logger,
            self.usage_aggregator,
            self.grok_homes,
            self.kimi_homes,
            self.dsh_homes,
            self.commandcode_homes,
            self.claude_homes,
            budget_usd=self.config.budget_usd,
            traffic_monitor=self.traffic_monitor,
            alert_store=self.alert_store,
            housekeeping=self.housekeeping,
            session_thresholds=self.session_thresholds,
            scan_dirs=self.scan_dirs,
            health=self.health,
            history=self.history,
            retention=self.retention,
        )
        server = _DashboardHTTPServer(
            (self.config.host, self.config.port),
            handler,
        )
        self._server = server
        self._thread = Thread(
            target=server.serve_forever,
            name="a-token-monitor-dashboard",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        """停止 HTTP 服务并等待请求线程退出。"""

        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
            if thread is not None:
                thread.join(timeout=2)
        self.usage_aggregator.close()

    def __enter__(self) -> "DashboardServer":
        """进入上下文并启动服务。"""

        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        """退出上下文并关闭服务。"""

        self.close()


def _visible_sessions(sessions: Sequence[TrackedSession]) -> list[TrackedSession]:
    """返回会话表要展示的会话：活动会话 + 最近结束但仍可归档的会话。"""

    now = time.time()
    visible: list[TrackedSession] = []
    for session in sessions:
        if session.is_active:
            visible.append(session)
            continue
        if not session.jsonl_path:
            continue
        # 中文注释：刚结束的会话留在表里，方便直接归档单个会话。
        if now - float(session.last_seen_at or 0.0) <= _RECENT_FINISHED_SECONDS:
            visible.append(session)
    return visible


def build_dashboard_state(
    registry: MultiSessionRegistry,
    account_name: str = "codex",
    account_id: str | None = None,
    profile_name: str | None = None,
    codex_home: str | None = None,
) -> dict[str, Any]:
    """读取 SQLite 并构造不包含提示词的 Dashboard 数据。"""

    quota = registry.load_quota()
    all_sessions = registry.list_sessions(active_only=False)
    sessions = _visible_sessions(all_sessions)
    status_counts: dict[str, int] = {}
    for session in sessions:
        status_counts[session.status.value] = (
            status_counts.get(session.status.value, 0) + 1
        )
    active_sessions = [item for item in sessions if item.is_active]
    status_counts["active"] = len(active_sessions)
    status_counts["recent"] = len(sessions) - len(active_sessions)
    status_counts["process_backed"] = sum(
        session.is_process_backed for session in active_sessions
    )
    return {
        "updated_at": time.time(),
        "account": account_name,
        "account_id": account_id,
        "profile_name": profile_name or account_name,
        "codex_home": codex_home,
        "quota": _quota_summary(quota),
        "counts": status_counts,
        "sessions": [
            session_view(
                session,
                account_name,
                account_id=account_id,
                profile_name=profile_name,
                codex_home=codex_home,
            )
            for session in sessions
        ],
    }


def _guard_provider(
    label: str,
    home: Path,
    health: HealthTracker | None = None,
    provider_key: str | None = None,
    error: BaseException | None = None,
) -> None:
    """记录单个 provider 目录的读取失败，继续构建其他 provider 的状态。"""

    if health is not None and provider_key is not None and error is not None:
        health.record_failure(f"provider:{provider_key}", error)
    logging.getLogger(__name__).exception(
        "%s 目录读取失败，已跳过该目录（其他 provider 不受影响）: %s",
        label,
        home,
    )


def _record_provider_success(
    health: HealthTracker | None,
    provider_key: str,
) -> None:
    """provider 读取成功后登记健康状态；无 tracker 时跳过。"""

    if health is not None:
        health.record_success(f"provider:{provider_key}")


def _profile_plan_type(metadata: Mapping[str, object]) -> str | None:
    """返回一个 profile 的订阅类型。

    优先用扫描目录元数据里已有的值；缺失时按 ``CODEX_HOME`` 读一次
    ``auth.json``（读取按 mtime 缓存，不会每 5 秒重复解析）。Grok / Kimi /
    Command Code 等 provider 的订阅类型由各自的额度快照提供。
    """

    codex_home = metadata.get("codex_home")
    if isinstance(codex_home, str) and codex_home.strip():
        # 中文注释：auth.json 是 Codex 自己写入的，优先级高于账号构造时的快照，
        # 这样续费或换号后不必重启 daemon（读取按 mtime 缓存）。
        live = read_codex_plan_type(Path(codex_home))
        if live:
            return live
    declared = metadata.get("plan_type")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    return None


def build_multi_dashboard_state(
    registries: Mapping[str, MultiSessionRegistry],
    account_metadata: Mapping[str, Mapping[str, str | None]] | None = None,
    usage_aggregator: UsageAggregator | None = None,
    grok_homes: Sequence[Path] | None = None,
    kimi_homes: Sequence[Path] | None = None,
    dsh_homes: Sequence[Path] | None = None,
    commandcode_homes: Sequence[Path] | None = None,
    claude_homes: Sequence[Path] | None = None,
    budget_usd: float | None = None,
    traffic: TrafficSnapshot | None = None,
    health: HealthTracker | None = None,
) -> dict[str, Any]:
    """合并多个账号状态，同时保留每个账号独立的额度快照。"""

    metadata_by_profile = account_metadata or {}
    account_states: list[dict[str, Any]] = []
    for profile_name, registry in registries.items():
        profile_metadata = metadata_by_profile.get(profile_name, {})
        account_id = profile_metadata.get("account_id")
        display_name = account_id or profile_name
        plan_type = _profile_plan_type(profile_metadata)
        prepared = build_dashboard_state(
            registry,
            account_name=display_name,
            account_id=account_id,
            profile_name=profile_metadata.get(
                "profile_name",
                profile_name,
            ),
            codex_home=profile_metadata.get("codex_home"),
        )
        # 中文注释：把订阅类型挂在各自的 state 上。第二个循环按账号遍历，
        # 若在这里依赖外层变量，多账号时会全部用成最后一个 profile 的套餐
        # （曾经因此把两个 Codex 账号都显示成同一个套餐）。
        prepared["plan_type"] = plan_type
        account_states.append(prepared)
    quotas: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    accounts_by_key: dict[str, dict[str, Any]] = {}
    quota_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for state in account_states:
        account_name = str(state["account"])
        account_id = state.get("account_id")
        profile_name = str(state.get("profile_name") or account_name)
        codex_home = state.get("codex_home")
        plan_type = state.get("plan_type")
        account_key = str(account_id or f"profile:{profile_name}")
        quota = state.get("quota")
        if isinstance(quota, dict):
            quota_with_account = dict(quota)
            quota_with_account["account"] = account_name
            quota_with_account["account_id"] = account_id
            quota_with_account["profile_name"] = profile_name
            quota_with_account["codex_home"] = codex_home
            if not quota_with_account.get("plan_type") and plan_type:
                quota_with_account["plan_type"] = plan_type
            quota_key = (account_key, "snapshot", "snapshot")
            previous_quota = quota_by_key.get(quota_key)
            if previous_quota is None or float(
                quota_with_account.get("observed_at", 0)
            ) >= float(previous_quota.get("observed_at", 0)):
                quota_by_key[quota_key] = quota_with_account
        state_sessions = state.get("sessions")
        if isinstance(state_sessions, list):
            sessions.extend(item for item in state_sessions if isinstance(item, dict))
        account = accounts_by_key.setdefault(
            account_key,
            {
                "name": account_name,
                "account_id": account_id,
                "plan_type": plan_type,
                "profiles": [],
                "quota": None,
                "counts": {},
            },
        )
        if plan_type and not account.get("plan_type"):
            account["plan_type"] = plan_type
        profile = {
            "name": profile_name,
            "codex_home": codex_home,
        }
        if profile not in account["profiles"]:
            account["profiles"].append(profile)

    # 会话记录中的 account_id 优先级高于当前 profile 的 ID，避免 profile
    # 重新登录后把旧账号的活动会话和额度归到新账号下面。
    for record in sessions:
        record_account_id = _record_account_id(record)
        record_profile_name = str(record.get("profile_name") or "codex")
        record_codex_home = record.get("codex_home")
        account_key = _record_account_key(record)
        account = accounts_by_key.setdefault(
            account_key,
            {
                "name": _record_account_name(record),
                "account_id": record_account_id,
                "profiles": [],
                "quota": None,
                "counts": {},
            },
        )
        profile = {
            "name": record_profile_name,
            "codex_home": record_codex_home,
        }
        if profile not in account["profiles"]:
            account["profiles"].append(profile)

    for grok_home in grok_homes or ():
        if not grok_home.is_dir():
            continue
        try:
            grok_account = read_grok_account(grok_home)
            grok_quota = read_grok_quota(grok_home)
            account_key = grok_account.account_key
            account = accounts_by_key.setdefault(
                account_key,
                {
                    "name": grok_account.display_name,
                    "account_id": grok_account.account_id,
                    "product": "grok",
                    "profiles": [],
                    "quota": None,
                    "counts": {},
                },
            )
            account["product"] = "grok"
            profile = {
                "name": grok_account.profile_name,
                "codex_home": str(grok_home),
            }
            if profile not in account["profiles"]:
                account["profiles"].append(profile)
            if grok_quota is not None:
                quota_with_account = _quota_summary(grok_quota)
                if quota_with_account is not None:
                    quota_with_account["account"] = grok_account.display_name
                    quota_with_account["account_id"] = grok_account.account_id
                    quota_with_account["profile_name"] = grok_account.profile_name
                    quota_with_account["codex_home"] = str(grok_home)
                    quota_with_account["product"] = "grok"
                    quota_by_key[(account_key, "snapshot", "snapshot")] = (
                        quota_with_account
                    )
            for session in list_grok_active_sessions(grok_home):
                sessions.append(
                    session_view(
                        session,
                        account_name=grok_account.display_name,
                        account_id=grok_account.account_id,
                        profile_name=grok_account.profile_name,
                        codex_home=str(grok_home),
                        product="grok",
                    )
                )
            _record_provider_success(health, 'grok')
        except Exception as error:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider(
                'Grok',
                grok_home,
                health=health,
                provider_key='grok',
                error=error,
            )
    # Kimi 配额经官方 /usages 接口读取（带缓存）；失败时账号卡片只展示身份。
    for kimi_home in kimi_homes or ():
        if not kimi_home.is_dir():
            continue
        try:
            kimi_account = read_kimi_account(kimi_home)
            kimi_quota = read_kimi_quota(kimi_home)
            account_key = kimi_account.account_key
            account = accounts_by_key.setdefault(
                account_key,
                {
                    "name": kimi_account.display_name,
                    "account_id": kimi_account.account_id,
                    "product": "kimi",
                    "profiles": [],
                    "quota": None,
                    "counts": {},
                },
            )
            account["product"] = "kimi"
            profile = {
                "name": kimi_account.profile_name,
                "codex_home": str(kimi_home),
            }
            if profile not in account["profiles"]:
                account["profiles"].append(profile)
            if kimi_quota is not None:
                quota_with_account = _quota_summary(kimi_quota)
                if quota_with_account is not None:
                    quota_with_account["account"] = kimi_account.display_name
                    quota_with_account["account_id"] = kimi_account.account_id
                    quota_with_account["profile_name"] = kimi_account.profile_name
                    quota_with_account["codex_home"] = str(kimi_home)
                    quota_with_account["product"] = "kimi"
                    quota_by_key[(account_key, "snapshot", "snapshot")] = (
                        quota_with_account
                    )
            for session in list_kimi_active_sessions(kimi_home):
                sessions.append(
                    session_view(
                        session,
                        account_name=kimi_account.display_name,
                        account_id=kimi_account.account_id,
                        profile_name=kimi_account.profile_name,
                        codex_home=str(kimi_home),
                        product="kimi",
                    )
                )
            _record_provider_success(health, 'kimi')
        except Exception as error:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider(
                'Kimi',
                kimi_home,
                health=health,
                provider_key='kimi',
                error=error,
            )
    for dsh_home in dsh_homes or ():
        if not dsh_home.is_dir():
            continue
        try:
            dsh_account = read_dsh_account(dsh_home)
            dsh_quota = read_dsh_quota(dsh_home)
            account_key = dsh_account.account_key
            account = accounts_by_key.setdefault(
                account_key,
                {
                    "name": dsh_account.display_name,
                    "account_id": dsh_account.account_id,
                    "product": "dsh",
                    "profiles": [],
                    "quota": None,
                    "counts": {},
                },
            )
            account["product"] = "dsh"
            profile = {
                "name": dsh_account.profile_name,
                "codex_home": str(dsh_home),
            }
            if profile not in account["profiles"]:
                account["profiles"].append(profile)
            if dsh_quota is not None:
                quota_with_account = _quota_summary(dsh_quota)
                if quota_with_account is not None:
                    quota_with_account["account"] = dsh_account.display_name
                    quota_with_account["account_id"] = dsh_account.account_id
                    quota_with_account["profile_name"] = dsh_account.profile_name
                    quota_with_account["codex_home"] = str(dsh_home)
                    quota_with_account["product"] = "dsh"
                    quota_by_key[(account_key, "snapshot", "snapshot")] = (
                        quota_with_account
                    )
            for session in list_dsh_active_sessions(dsh_home):
                sessions.append(
                    session_view(
                        session,
                        account_name=dsh_account.display_name,
                        account_id=dsh_account.account_id,
                        profile_name=dsh_account.profile_name,
                        codex_home=str(dsh_home),
                        product="dsh",
                    )
                )
            _record_provider_success(health, 'dsh')
        except Exception as error:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider(
                'DeepSeek Harness',
                dsh_home,
                health=health,
                provider_key='dsh',
                error=error,
            )
    # Claude Code 订阅额度经 OAuth usage 接口读取（带缓存）；失败时只展示账号身份。
    for claude_home in claude_homes or ():
        if not claude_home.is_dir():
            continue
        try:
            claude_account = read_claude_account(claude_home)
            claude_quota = read_claude_quota(claude_home)
            account_key = claude_account.account_key
            account = accounts_by_key.setdefault(
                account_key,
                {
                    "name": claude_account.display_name,
                    "account_id": claude_account.account_id,
                    "product": "claude",
                    "profiles": [],
                    "quota": None,
                    "counts": {},
                },
            )
            account["product"] = "claude"
            profile = {
                "name": claude_account.profile_name,
                "codex_home": str(claude_home),
            }
            if profile not in account["profiles"]:
                account["profiles"].append(profile)
            if claude_quota is not None:
                quota_with_account = _quota_summary(claude_quota)
                if quota_with_account is not None:
                    quota_with_account["account"] = claude_account.display_name
                    quota_with_account["account_id"] = claude_account.account_id
                    quota_with_account["profile_name"] = (
                        claude_account.profile_name
                    )
                    quota_with_account["codex_home"] = str(claude_home)
                    quota_with_account["product"] = "claude"
                    quota_by_key[(account_key, "snapshot", "snapshot")] = (
                        quota_with_account
                    )
            for session in list_claude_active_sessions(claude_home):
                sessions.append(
                    session_view(
                        session,
                        claude_account.display_name,
                        account_id=claude_account.account_id,
                        profile_name=claude_account.profile_name,
                        codex_home=str(claude_home),
                    )
                )
            _record_provider_success(health, 'claude')
        except Exception as error:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider(
                'Claude Code',
                claude_home,
                health=health,
                provider_key='claude',
                error=error,
            )
    # Command Code 订阅额度经官方后台接口读取（带缓存）；失败时只展示账号身份。
    for commandcode_home in commandcode_homes or ():
        if not commandcode_home.is_dir():
            continue
        try:
            commandcode_account = read_commandcode_account(commandcode_home)
            commandcode_quota = read_commandcode_quota(commandcode_home)
            account_key = commandcode_account.account_key
            account = accounts_by_key.setdefault(
                account_key,
                {
                    "name": commandcode_account.display_name,
                    "account_id": commandcode_account.account_id,
                    "product": "command-code",
                    "profiles": [],
                    "quota": None,
                    "counts": {},
                },
            )
            account["product"] = "command-code"
            profile = {
                "name": commandcode_account.profile_name,
                "codex_home": str(commandcode_home),
            }
            if profile not in account["profiles"]:
                account["profiles"].append(profile)
            if commandcode_quota is not None:
                quota_with_account = _quota_summary(commandcode_quota)
                if quota_with_account is not None:
                    quota_with_account["account"] = commandcode_account.display_name
                    quota_with_account["account_id"] = commandcode_account.account_id
                    quota_with_account["profile_name"] = (
                        commandcode_account.profile_name
                    )
                    quota_with_account["codex_home"] = str(commandcode_home)
                    quota_with_account["product"] = "command-code"
                    quota_by_key[(account_key, "snapshot", "snapshot")] = (
                        quota_with_account
                    )
            for session in list_commandcode_active_sessions(commandcode_home):
                sessions.append(
                    session_view(
                        session,
                        account_name=commandcode_account.display_name,
                        account_id=commandcode_account.account_id,
                        profile_name=commandcode_account.profile_name,
                        codex_home=str(commandcode_home),
                        product="command-code",
                    )
                )
            _record_provider_success(health, 'command-code')
        except Exception as error:  # noqa: BLE001 - 单个 provider 失败不影响其他 provider
            _guard_provider(
                'Command Code',
                commandcode_home,
                health=health,
                provider_key='command-code',
                error=error,
            )
    counts: dict[str, int] = {}
    for account in accounts_by_key.values():
        account["counts"] = {}
    for session in sessions:
        account = accounts_by_key[_record_account_key(session)]
        status = session.get("status")
        if isinstance(status, str) and status:
            account["counts"][status] = account["counts"].get(status, 0) + 1
            counts[status] = counts.get(status, 0) + 1
        # 中文注释：会话表里也会列出最近结束的会话，但它们不计入活动数。
        if session.get("active", True):
            account["counts"]["active"] = account["counts"].get("active", 0) + 1
            counts["active"] = counts.get("active", 0) + 1
            if session.get("process_backed"):
                account["counts"]["process_backed"] = (
                    account["counts"].get("process_backed", 0) + 1
                )
                counts["process_backed"] = counts.get("process_backed", 0) + 1
        else:
            account["counts"]["recent"] = account["counts"].get("recent", 0) + 1
            counts["recent"] = counts.get("recent", 0) + 1

    quotas = list(quota_by_key.values())
    accounts = list(accounts_by_key.values())
    for account_key, account in accounts_by_key.items():
        account["quota"] = quota_by_key.get((account_key, "snapshot", "snapshot"))
    usage = (
        usage_aggregator.snapshot(
            registries,
            account_metadata=metadata_by_profile,
        )
        if usage_aggregator is not None
        else UsageAggregator.empty_snapshot()
    )
    return {
        "updated_at": time.time(),
        "accounts": accounts,
        # 保留单账号旧字段，新的页面使用 quotas 以免混淆不同账号。
        "quota": (quotas[0] if len(registries) == 1 and len(quotas) == 1 else None),
        "quotas": quotas,
        "counts": counts,
        "sessions": sessions,
        "usage": usage,
        "budget_usd": budget_usd,
        "traffic": (
            traffic.to_dict()
            if traffic is not None
            else empty_traffic_snapshot().to_dict()
        ),
    }


def _record_account_id(record: Mapping[str, Any]) -> str | None:
    """读取会话摘要中已经持久化的真实账号 ID。"""

    value = record.get("account_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _record_account_key(record: Mapping[str, Any]) -> str:
    """生成会话记录的稳定账号归组键。"""

    account_id = _record_account_id(record)
    if account_id is not None:
        return account_id
    profile_name = record.get("profile_name")
    if isinstance(profile_name, str) and profile_name.strip():
        return f"profile:{profile_name.strip()}"
    account_name = record.get("account")
    if isinstance(account_name, str) and account_name.strip():
        return f"profile:{account_name.strip()}"
    return "profile:codex"


def _record_account_name(record: Mapping[str, Any]) -> str:
    """读取会话摘要适合展示的账号名称。"""

    account_id = _record_account_id(record)
    if account_id is not None:
        return account_id
    account_name = record.get("account")
    if isinstance(account_name, str) and account_name.strip():
        return account_name.strip()
    return "codex"


def _quota_summary(snapshot: QuotaSnapshot | None) -> dict[str, Any] | None:
    """把额度快照转换成网页需要的安全字段。"""

    if snapshot is None:
        return None
    return {
        "observed_at": snapshot.observed_at,
        "plan_type": snapshot.plan_type,
        "source": snapshot.source,
        "metadata": dict(snapshot.metadata),
        "windows": [
            {
                "limit_id": window.limit_id,
                "name": window.name,
                "used_percent": window.used_percent,
                "window_minutes": window.window_minutes,
                "resets_at": window.resets_at,
                "is_exhausted": window.is_exhausted,
                # 中文注释：统一周期口径在这里定死，面板只按 period 画固定行，
                # 不再依赖上游五花八门的窗口名（primary / limit_month_total / 5-hour…）。
                "period": quota_period(window),
                "period_label": quota_period_label(quota_period(window)),
                "duration_label": quota_window_duration(window),
            }
            for window in snapshot.windows
        ],
    }


def _insights_window(query: str) -> tuple[str, int | None, float | None]:
    """解析习惯分析的统计窗口，返回 ``(类型, 天数, 起始时间戳)``。

    ``days=today`` 表示按本地日历日的「今天」（与用量区的今天同口径）；
    ``days=N`` 表示最近 N 天；缺失、``0`` 或非法值都按全部历史处理。
    """

    raw = parse_qs(query).get("days", [None])[0]
    if raw is None:
        return ("all", None, None)
    if raw.strip().lower() == "today":
        return ("today", None, calendar_day_start())
    try:
        days = int(raw)
    except ValueError:
        return ("all", None, None)
    if 0 < days <= 3660:
        return ("days", days, None)
    return ("all", None, None)


def _usage_search_arguments(raw_query: str) -> dict[str, Any]:
    """把用量检索查询串解析成 UsageAggregator.search 参数。"""

    params = parse_qs(raw_query)

    def single(name: str) -> str | None:
        for value in params.get(name) or ():
            text = value.strip()
            if text:
                return text
        return None

    # 中文注释：days=0 表示不限制时间范围，非法值退回默认天数。
    days = DEFAULT_SEARCH_DAYS
    raw_days = single("days")
    if raw_days is not None:
        try:
            parsed_days = int(raw_days)
        except ValueError:
            parsed_days = DEFAULT_SEARCH_DAYS
        days = parsed_days if 0 <= parsed_days <= 3660 else DEFAULT_SEARCH_DAYS
    since: float | None = None
    until: float | None = None
    explicit_from = _day_start(single("from"))
    explicit_to = _day_end(single("to"))
    if explicit_from is not None or explicit_to is not None:
        since, until = explicit_from, explicit_to
    elif days > 0:
        since = search_since_days(days)
    group = (single("group") or "session").lower()
    sort = (single("sort") or "recent").lower()
    return {
        "since": since,
        "until": until,
        "models": tuple(_split_values(single("model"))),
        "session": single("session"),
        "project": single("project"),
        "account": single("account"),
        "keyword": single("q"),
        "group": group,
        "sort": sort,
        "limit": _bounded_int(single("limit"), maximum=500) or 50,
        "offset": _bounded_int(single("offset"), maximum=1_000_000) or 0,
    }


def _day_start(value: str | None) -> float | None:
    """把 YYYY-MM-DD 解析为本地当天零点时间戳。"""

    parsed = _parse_day(value)
    return parsed.timestamp() if parsed is not None else None


def _day_end(value: str | None) -> float | None:
    """把 YYYY-MM-DD 解析为本地当天最后一刻的时间戳。"""

    parsed = _parse_day(value)
    if parsed is None:
        return None
    return parsed.timestamp() + 86400.0 - 1e-6


def _parse_day(value: str | None) -> datetime | None:
    """解析日期参数，非法值按未提供处理。"""

    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError:
        return None


def empty_housekeeping_payload() -> dict[str, Any]:
    """返回未接入磁盘统计时的空报告。"""

    return empty_housekeeping_report()


def _healthz_payload(health: HealthTracker | None) -> tuple[int, dict[str, Any]]:
    """组装 /healthz 响应；主循环停滞（含 degraded 过期）一律视为卡死。"""

    if health is None:
        return 200, {"status": "ok", "updated_at": time.time()}
    main_loop = health.component_status("main-loop")
    if main_loop == "ok":
        return 200, {
            "status": "ok",
            "uptime_seconds": max(0.0, time.time() - health.started_at),
            "updated_at": time.time(),
        }
    return 503, {
        "status": "stuck",
        "main_loop": main_loop,
        "updated_at": time.time(),
    }


def _readyz_payload(health: HealthTracker | None) -> tuple[int, dict[str, Any]]:
    """组装 /readyz 响应；就绪判定只看关键组件，组件明细原样来自快照。"""

    if health is None:
        return 200, {"status": "ok", "updated_at": time.time()}
    snapshot = health.snapshot()
    ready = health.ready()
    return (200 if ready else 503), {
        "status": "ready" if ready else "not_ready",
        "overall": snapshot["overall"],
        "components": snapshot["components"],
        "updated_at": time.time(),
    }


def _history_get_payload(
    history: HistoryDataManager | None,
    retention: RetentionController | None,
    raw_query: str,
) -> tuple[int, dict[str, Any]]:
    """组装 GET /api/history 响应；?preview=1 时附带清理预览。"""

    if history is None:
        return 200, {"updated_at": time.time(), "available": False}
    try:
        dbs = history.db_sizes()
    except OSError:
        # 中文注释：占用统计失败不阻断整个端点，降级为空列表。
        dbs = []
    payload: dict[str, Any] = {
        "updated_at": time.time(),
        "available": True,
        "retention_days": history.retention_days,
        "retention": (
            retention.snapshot()["retention"] if retention is not None else None
        ),
        "dbs": dbs,
        "last_cleanup": history.last_cleanup,
    }
    if parse_qs(raw_query).get("preview") == ["1"]:
        try:
            payload["preview"] = history.preview()
        except OSError as error:
            return 500, {
                "error": "history_preview_failed",
                "message": sanitize_error(error),
                "updated_at": time.time(),
            }
    return 200, payload


def _housekeeping_summary(
    monitor: HousekeepingMonitor | None,
) -> dict[str, Any]:
    """返回 /api/state 使用的紧凑磁盘摘要，避免每 5 秒回传完整目录树。"""

    if monitor is None:
        return {"available": False, "totals": {}, "reminders": [], "preview": None}
    report = monitor.latest()
    if report.get("observed_at") is None:
        # 中文注释：首次访问时补一次扫描；refresh 自带刷新间隔节流。
        try:
            report = monitor.refresh()
        except (OSError, ValueError):
            # 中文注释：磁盘扫描失败不应击穿整个 /api/state，降级为不可用摘要。
            logger = logging.getLogger(__name__)
            logger.exception("Dashboard 磁盘摘要刷新失败，已降级")
            return {
                "available": False,
                "totals": {},
                "reminders": [],
                "preview": None,
            }
    return {
        "available": True,
        "observed_at": report.get("observed_at"),
        "thresholds": report.get("thresholds", {}),
        "archive_dir": report.get("archive_dir"),
        "totals": report.get("totals", {}),
        "reminders": list(report.get("reminders") or []),
        "preview": report.get("preview"),
        "directories": [
            {
                "label": item.get("label"),
                "path": item.get("path"),
                "bytes": item.get("bytes"),
                "files": item.get("files"),
                "session_bytes": item.get("session_bytes"),
                "session_files": item.get("session_files"),
                "cleanable": item.get("cleanable"),
                "top_children": list(item.get("top_children") or [])[:4],
            }
            for item in report.get("directories", [])
        ],
    }


def _usage_index_summary(
    aggregator: UsageAggregator | None,
) -> dict[str, Any]:
    """返回用量索引的紧凑摘要，供折叠状态下的用量检索分区展示。"""

    if aggregator is None:
        return {
            "available": False,
            "records": 0,
            "sessions": 0,
            "models": 0,
            "accounts": 0,
            "first_at": None,
            "last_at": None,
        }
    try:
        facets = aggregator.usage_facets()
    except (OSError, ValueError):
        return {
            "available": False,
            "records": 0,
            "sessions": 0,
            "models": 0,
            "accounts": 0,
            "first_at": None,
            "last_at": None,
        }
    return {
        "available": bool(facets.get("available")),
        "records": int(facets.get("records") or 0),
        "sessions": int(facets.get("sessions") or 0),
        "models": len(facets.get("models") or ()),
        "accounts": len(facets.get("accounts") or ()),
        "first_at": facets.get("first_at"),
        "last_at": facets.get("last_at"),
    }


def _attach_session_archive(
    state: dict[str, Any],
    monitor: HousekeepingMonitor | None,
) -> None:
    """给活动会话标注能否单独归档，供会话表里的「归档」按钮使用。"""

    sessions = state.get("sessions")
    if monitor is None or not isinstance(sessions, list):
        return
    paths = [
        str(item.get("jsonl_path"))
        for item in sessions
        if isinstance(item, dict) and item.get("jsonl_path")
    ]
    if not paths:
        return
    try:
        states = monitor.session_archive_state(paths)
    except (OSError, ValueError):
        return
    for session in sessions:
        if not isinstance(session, dict):
            continue
        raw = session.get("jsonl_path")
        if not raw:
            continue
        entry = states.get(str(raw))
        if entry is not None:
            session["archive"] = entry


def _attach_session_advice(
    state: dict[str, Any],
    aggregator: UsageAggregator | None,
    thresholds: SessionSwitchThresholds,
) -> None:
    """把统一会话视图的 token 与长会话提醒并入 /api/state。"""

    sessions = state.get("sessions")
    views = [
        item for item in sessions if isinstance(item, dict)
    ] if isinstance(sessions, list) else []
    _, reminders = enrich_session_views(views, aggregator, thresholds)
    state["session_advice"] = {
        "thresholds": thresholds.to_dict(),
        "count": len(reminders),
        "sessions": reminders,
    }


def _housekeeping_arguments(raw_query: str) -> dict[str, Any]:
    """解析磁盘/会话管理查询串。"""

    params = parse_qs(raw_query)

    def single(name: str) -> str | None:
        for value in params.get(name) or ():
            text = value.strip()
            if text:
                return text
        return None

    days = _bounded_int(single("days"), maximum=3650) or 30
    min_size_mb = _bounded_float(single("min_size_mb"), maximum=1_000_000) or 0.0
    task = single("task")
    return {
        "days": days,
        "min_bytes": int(min_size_mb * 1024 * 1024),
        "refresh": single("refresh") not in {None, "0", "false"},
        "task": task if task and task.isalnum() else None,
    }


def _archive_path_for(monitor: HousekeepingMonitor, name: str) -> Path:
    """把请求里的归档名限制在归档目录内，避免任意路径读取。"""

    candidate = Path(name).name
    if not candidate or candidate != name:
        raise HousekeepingError("归档名非法")
    archive_dir = monitor.archive_dir
    if archive_dir is None:
        raise HousekeepingError("没有配置归档目录")
    archive = archive_dir / candidate
    if not archive.is_file():
        raise HousekeepingError(f"归档不存在: {candidate}")
    return archive


def empty_alert_history_payload() -> dict[str, Any]:
    """返回未接入告警落盘时的空历史结构。"""

    return {
        "available": False,
        "retention_days": None,
        "merge_window_seconds": None,
        "alerts": [],
        "has_more": False,
        "stats": {
            "total": 0,
            "unread": 0,
            "danger": 0,
            "warn": 0,
            "last_alert_at": None,
        },
    }


def _alert_query_from_url(raw_query: str) -> AlertQuery:
    """把 Dashboard 查询串解析为告警筛选条件。"""

    params = parse_qs(raw_query)

    def single(name: str) -> str | None:
        for value in params.get(name) or ():
            text = value.strip()
            if text:
                return text
        return None

    days = _bounded_int(single("days"), maximum=3660)
    since = time.time() - days * 86400 if days else None
    explicit_since = _bounded_float(single("since"))
    if explicit_since is not None:
        since = explicit_since
    acknowledged: bool | None = None
    ack_value = (single("ack") or "").lower()
    if ack_value == "unread":
        acknowledged = False
    elif ack_value == "read":
        acknowledged = True
    return AlertQuery(
        since=since,
        until=_bounded_float(single("until")),
        levels=tuple(_split_values(single("level"))),
        kinds=tuple(_split_values(single("kind"))),
        products=tuple(_split_values(single("product"))),
        acknowledged=acknowledged,
        keyword=single("q"),
        limit=_bounded_int(single("limit"), maximum=MAX_QUERY_LIMIT) or 50,
        offset=_bounded_int(single("offset"), maximum=1_000_000) or 0,
    )


def _apply_alert_action(
    store: TrafficAlertStore,
    action: str,
    body: Mapping[str, Any],
) -> int:
    """执行一次告警历史修改，返回改动条数。"""

    if action == "ack":
        if body.get("all") is True:
            return store.acknowledge(all_alerts=True)
        return store.acknowledge(_alert_ids(body.get("ids")))
    if action == "unack":
        return store.unacknowledge(_alert_ids(body.get("ids")))
    if action == "clear":
        if body.get("all") is True:
            return store.clear_all()
        before = _bounded_float(body.get("before"))
        if before is not None:
            return store.clear_before(before)
        return store.clear(_alert_ids(body.get("ids")))
    raise AlertStoreError(f"未知告警操作: {action or '(空)'}")


def _alert_ids(value: object) -> tuple[int, ...]:
    """校验告警 ID 列表，拒绝非法值和非正整数。"""

    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > MAX_QUERY_LIMIT:
        raise AlertStoreError("ids 必须是告警 ID 数组")
    ids: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise AlertStoreError("ids 只能包含告警 ID 数字")
        identifier = int(item)
        if identifier <= 0:
            raise AlertStoreError("告警 ID 必须是正整数")
        ids.append(identifier)
    return tuple(ids)


def _bounded_int(value: object, *, maximum: int) -> int | None:
    """解析 1 到 maximum 之间的整数，非法值返回 None。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if 1 <= parsed <= maximum else None


def _bounded_float(value: object, *, maximum: float = 1e12) -> float | None:
    """解析正浮点数，非法值返回 None。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or parsed > maximum:
        return None
    return parsed


def _split_values(value: str | None) -> list[str]:
    """把逗号分隔的筛选值拆成去重后的列表。"""

    if not value:
        return []
    items = [item.strip() for item in value.split(",")]
    return [item for index, item in enumerate(items) if item and item not in items[:index]]


def _make_handler(
    accounts: _AccountSet,
    logger: logging.Logger,
    usage_aggregator: UsageAggregator | None = None,
    grok_homes: Sequence[Path] | None = None,
    kimi_homes: Sequence[Path] | None = None,
    dsh_homes: Sequence[Path] | None = None,
    commandcode_homes: Sequence[Path] | None = None,
    claude_homes: Sequence[Path] | None = None,
    budget_usd: float | None = None,
    traffic_monitor: TrafficMonitor | None = None,
    alert_store: TrafficAlertStore | None = None,
    housekeeping: HousekeepingMonitor | None = None,
    session_thresholds: SessionSwitchThresholds | None = None,
    scan_dirs: ScanDirsController | None = None,
    health: HealthTracker | None = None,
    history: HistoryDataManager | None = None,
    retention: RetentionController | None = None,
) -> type[BaseHTTPRequestHandler]:
    """为多个注册表创建隔离的 HTTP 请求处理器类型。

    中文注释：注册表与账号元数据经由 accounts 容器读取，DashboardServer
    可以在运行中热替换账号集合而无需重启 HTTP 服务。
    """

    thresholds_in_use = session_thresholds or SessionSwitchThresholds()

    def alert_stats() -> dict[str, Any]:
        """返回落盘告警的统计；数据库不可用时降级为空统计。"""

        if alert_store is None:
            return {
                "available": False,
                "total": 0,
                "unread": 0,
                "danger": 0,
                "warn": 0,
                "last_alert_at": None,
            }
        try:
            return {"available": True, **alert_store.stats()}
        except AlertStoreError:
            logger.exception("Dashboard 读取告警统计失败")
            return {
                "available": False,
                "total": 0,
                "unread": 0,
                "danger": 0,
                "warn": 0,
                "last_alert_at": None,
            }

    class DashboardRequestHandler(BaseHTTPRequestHandler):
        """处理 Dashboard 页面、只读状态和告警历史请求。"""

        server_version = "ATokenMonitorDashboard/0.9"

        def do_GET(self) -> None:
            """返回静态页面或当前监控状态。"""

            path = urlsplit(self.path).path
            if path == "/":
                page = _localize_page(_DASHBOARD_HTML, self._request_language())
                self._send_bytes(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=page.encode("utf-8"),
                )
                return
            if path == "/settings":
                self._send_bytes(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=_localize_page(
                        _SETTINGS_HTML,
                        self._request_language(),
                    ).encode("utf-8"),
                )
                return
            favicon = favicon_response(path)
            if favicon is not None:
                self._send_bytes(
                    status=200,
                    content_type=favicon[0],
                    body=favicon[1],
                    cache_seconds=_FAVICON_CACHE_SECONDS,
                )
                return
            if path == "/healthz":
                status, payload = _healthz_payload(health)
                self._send_json(status=status, payload=payload)
                return
            if path == "/readyz":
                status, payload = _readyz_payload(health)
                self._send_json(status=status, payload=payload)
                return
            if path == "/api/state":
                current_registries, current_metadata = accounts.snapshot()
                try:
                    state = build_multi_dashboard_state(
                        current_registries,
                        account_metadata=current_metadata,
                        grok_homes=grok_homes,
                        kimi_homes=kimi_homes,
                        dsh_homes=dsh_homes,
                        commandcode_homes=commandcode_homes,
                        claude_homes=claude_homes,
                        budget_usd=budget_usd,
                        traffic=(
                            traffic_monitor.latest()
                            if traffic_monitor is not None
                            else None
                        ),
                        health=health,
                    )
                except RegistryError:
                    logger.exception("Dashboard 读取状态失败")
                    self._send_json(
                        status=503,
                        payload={"error": "monitor_state_unavailable"},
                    )
                    return
                state["alert_history"] = alert_stats()
                _attach_session_advice(
                    state,
                    usage_aggregator,
                    thresholds_in_use,
                )
                _attach_session_archive(state, housekeeping)
                state["housekeeping"] = _housekeeping_summary(housekeeping)
                state["usage_index"] = _usage_index_summary(usage_aggregator)
                state["health"] = health.snapshot() if health is not None else None
                self._send_json(status=200, payload=state)
                return
            if path == "/api/alerts":
                if alert_store is None:
                    self._send_json(
                        status=200,
                        payload={
                            "updated_at": time.time(),
                            **empty_alert_history_payload(),
                        },
                    )
                    return
                try:
                    criteria = _alert_query_from_url(urlsplit(self.path).query)
                    alerts, has_more = alert_store.query_page(criteria)
                    stats = alert_store.stats(since=criteria.since)
                except AlertStoreError as error:
                    self._send_json(
                        status=400,
                        payload={
                            "error": "invalid_alert_query",
                            "message": str(error),
                        },
                    )
                    return
                self._send_json(
                    status=200,
                    payload={
                        "updated_at": time.time(),
                        "available": True,
                        "retention_days": alert_store.retention_days,
                        "merge_window_seconds": alert_store.merge_window_seconds,
                        "alerts": [item.to_dict() for item in alerts],
                        "stats": stats,
                        "has_more": has_more,
                        "limit": criteria.limit,
                        "offset": criteria.offset,
                    },
                )
                return
            if path == "/api/scan-dirs":
                if scan_dirs is None:
                    self._send_json(
                        status=200,
                        payload={
                            "updated_at": time.time(),
                            "available": False,
                            "providers": [],
                        },
                    )
                    return
                self._send_json(
                    status=200,
                    payload={
                        "updated_at": time.time(),
                        "available": True,
                        **scan_dirs.snapshot(),
                    },
                )
                return
            if path == "/api/history":
                status, payload = _history_get_payload(
                    history,
                    retention,
                    urlsplit(self.path).query,
                )
                self._send_json(status=status, payload=payload)
                return
            if path == "/api/usage":
                current_registries, current_metadata = accounts.snapshot()
                try:
                    usage = (
                        usage_aggregator.snapshot(
                            current_registries,
                            account_metadata=current_metadata,
                        )
                        if usage_aggregator is not None
                        else UsageAggregator.empty_snapshot()
                    )
                except RegistryError:
                    logger.exception("Dashboard 读取用量失败")
                    self._send_json(
                        status=503,
                        payload={"error": "usage_state_unavailable"},
                    )
                    return
                self._send_json(
                    status=200,
                    payload={"updated_at": time.time(), "usage": usage},
                )
                return
            if path == "/api/usage/search":
                try:
                    arguments = _usage_search_arguments(urlsplit(self.path).query)
                    result = (
                        usage_aggregator.search(**arguments)
                        if usage_aggregator is not None
                        else UsageAggregator.empty_search(
                            group=arguments["group"],
                            sort=arguments["sort"],
                            limit=arguments["limit"],
                            offset=arguments["offset"],
                        )
                    )
                    facets = (
                        usage_aggregator.usage_facets()
                        if usage_aggregator is not None
                        else {
                            "available": False,
                            "records": 0,
                            "sessions": 0,
                            "models": [],
                            "accounts": [],
                            "first_at": None,
                            "last_at": None,
                        }
                    )
                except ValueError as error:
                    self._send_json(
                        status=400,
                        payload={
                            "error": "invalid_usage_search",
                            "message": str(error),
                        },
                    )
                    return
                self._send_json(
                    status=200,
                    payload={
                        "updated_at": time.time(),
                        "search": result,
                        "facets": facets,
                    },
                )
                return
            if path == "/api/housekeeping":
                if housekeeping is None:
                    self._send_json(
                        status=200,
                        payload={
                            "updated_at": time.time(),
                            "available": False,
                            "report": empty_housekeeping_payload(),
                            "preview": None,
                            "archives": [],
                        },
                    )
                    return
                try:
                    arguments = _housekeeping_arguments(urlsplit(self.path).query)
                    if arguments["task"]:
                        self._send_json(
                            status=200,
                            payload={
                                "updated_at": time.time(),
                                "available": True,
                                "task": housekeeping.task(arguments["task"]),
                                "tasks": list(housekeeping.tasks()),
                            },
                        )
                        return
                    criteria = CleanupCriteria(
                        older_than_days=arguments["days"],
                        min_bytes=arguments["min_bytes"],
                    )
                    report = housekeeping.refresh(force=arguments["refresh"])
                    preview = housekeeping.preview(criteria)
                except (HousekeepingError, ValueError) as error:
                    self._send_json(
                        status=400,
                        payload={
                            "error": "invalid_housekeeping_query",
                            "message": str(error),
                        },
                    )
                    return
                self._send_json(
                    status=200,
                    payload={
                        "updated_at": time.time(),
                        "available": True,
                        "report": report,
                        "preview": preview,
                        "archives": list(housekeeping.restores()),
                        "tasks": list(housekeeping.tasks()),
                    },
                )
                return
            if path == "/api/insights":
                window_kind, window_days, window_since = _insights_window(
                    urlsplit(self.path).query
                )
                current_registries, current_metadata = accounts.snapshot()
                try:
                    insights = (
                        usage_aggregator.insights(
                            current_registries,
                            account_metadata=current_metadata,
                            since_days=window_days,
                            since=window_since,
                            window_kind=window_kind,
                        )
                        if usage_aggregator is not None
                        else UsageAggregator.empty_insights()
                    )
                except RegistryError:
                    logger.exception("Dashboard 读取习惯分析失败")
                    self._send_json(
                        status=503,
                        payload={"error": "insights_state_unavailable"},
                    )
                    return
                self._send_json(
                    status=200,
                    payload={"updated_at": time.time(), "insights": insights},
                )
                return
            self._send_json(status=404, payload={"error": "not_found"})

        def do_HEAD(self) -> None:
            """返回 GET 的响应头，便于使用 curl 做健康检查。"""

            path = urlsplit(self.path).path
            if path == "/":
                self._send_bytes(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=_localize_page(
                        _DASHBOARD_HTML,
                        self._request_language(),
                    ).encode("utf-8"),
                    include_body=False,
                )
                return
            if path == "/settings":
                self._send_bytes(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=_localize_page(
                        _SETTINGS_HTML,
                        self._request_language(),
                    ).encode("utf-8"),
                    include_body=False,
                )
                return
            favicon = favicon_response(path)
            if favicon is not None:
                self._send_bytes(
                    status=200,
                    content_type=favicon[0],
                    body=favicon[1],
                    include_body=False,
                    cache_seconds=_FAVICON_CACHE_SECONDS,
                )
                return
            if path == "/healthz":
                status, payload = _healthz_payload(health)
                self._send_json(status=status, payload=payload, include_body=False)
                return
            if path == "/readyz":
                status, payload = _readyz_payload(health)
                self._send_json(status=status, payload=payload, include_body=False)
                return
            if path == "/api/state":
                current_registries, current_metadata = accounts.snapshot()
                try:
                    state = build_multi_dashboard_state(
                        current_registries,
                        account_metadata=current_metadata,
                        grok_homes=grok_homes,
                        kimi_homes=kimi_homes,
                        dsh_homes=dsh_homes,
                        commandcode_homes=commandcode_homes,
                        claude_homes=claude_homes,
                        budget_usd=budget_usd,
                        traffic=(
                            traffic_monitor.latest()
                            if traffic_monitor is not None
                            else None
                        ),
                        health=health,
                    )
                except RegistryError:
                    logger.exception("Dashboard 读取状态失败")
                    self._send_json(
                        status=503,
                        payload={"error": "monitor_state_unavailable"},
                        include_body=False,
                    )
                    return
                state["alert_history"] = alert_stats()
                _attach_session_advice(
                    state,
                    usage_aggregator,
                    thresholds_in_use,
                )
                _attach_session_archive(state, housekeeping)
                state["housekeeping"] = _housekeeping_summary(housekeeping)
                state["usage_index"] = _usage_index_summary(usage_aggregator)
                state["health"] = health.snapshot() if health is not None else None
                self._send_json(
                    status=200,
                    payload=state,
                    include_body=False,
                )
                return
            if path == "/api/alerts":
                # 中文注释：HEAD 只用于健康检查，不返回告警明细。
                self._send_json(
                    status=200,
                    payload={
                        "updated_at": time.time(),
                        "available": alert_store is not None,
                        "stats": alert_stats(),
                    },
                    include_body=False,
                )
                return
            if path == "/api/scan-dirs":
                # 中文注释：HEAD 只用于健康检查，不做目录校验。
                self._send_json(
                    status=200,
                    payload={
                        "updated_at": time.time(),
                        "available": scan_dirs is not None,
                    },
                    include_body=False,
                )
                return
            if path == "/api/history":
                # 中文注释：HEAD 只用于健康检查，不触发预览统计。
                self._send_json(
                    status=200,
                    payload={
                        "updated_at": time.time(),
                        "available": history is not None,
                    },
                    include_body=False,
                )
                return
            if path == "/api/usage":
                usage = (
                    usage_aggregator.cached_snapshot()
                    if usage_aggregator is not None
                    else UsageAggregator.empty_snapshot()
                )
                self._send_json(
                    status=200,
                    payload={"updated_at": time.time(), "usage": usage},
                    include_body=False,
                )
                return
            if path == "/api/usage/search":
                # 中文注释：HEAD 只用于健康检查，不触发一次完整用量检索。
                self._send_json(
                    status=200,
                    payload={
                        "updated_at": time.time(),
                        "search": UsageAggregator.empty_search(),
                        "facets": {
                            "available": usage_aggregator is not None,
                            "records": 0,
                            "sessions": 0,
                            "models": [],
                            "accounts": [],
                            "first_at": None,
                            "last_at": None,
                        },
                    },
                    include_body=False,
                )
                return
            if path == "/api/housekeeping":
                # 中文注释：HEAD 只用于健康检查，不触发一次目录扫描。
                self._send_json(
                    status=200,
                    payload={
                        "updated_at": time.time(),
                        "available": housekeeping is not None,
                        "report": (
                            housekeeping.latest()
                            if housekeeping is not None
                            else empty_housekeeping_payload()
                        ),
                        "preview": None,
                        "archives": [],
                    },
                    include_body=False,
                )
                return
            if path == "/api/insights":
                # 中文注释：HEAD 只用于健康检查，不触发一次完整习惯分析。
                self._send_json(
                    status=200,
                    payload={
                        "updated_at": time.time(),
                        "insights": UsageAggregator.empty_insights(),
                    },
                    include_body=False,
                )
                return
            self._send_json(
                status=404,
                payload={"error": "not_found"},
                include_body=False,
            )

        def do_POST(self) -> None:
            """处理告警历史、磁盘管理、扫描目录和历史数据的写请求，其余路径仍为只读。"""

            path = urlsplit(self.path).path
            if path == "/api/housekeeping":
                self._handle_housekeeping_post(housekeeping)
                return
            if path == "/api/scan-dirs":
                self._handle_scan_dirs_post(scan_dirs)
                return
            if path == "/api/history":
                self._handle_history_post(history, retention)
                return
            if path != "/api/alerts":
                self._send_json(status=405, payload={"error": "read_only"})
                return
            if alert_store is None:
                self._send_json(
                    status=503,
                    payload={"error": "alert_history_unavailable"},
                )
                return
            try:
                body = self._read_json_body()
                action = str(body.get("action") or "").strip()
                changed = _apply_alert_action(alert_store, action, body)
            except AlertStoreError as error:
                self._send_json(
                    status=400,
                    payload={"error": "invalid_alert_action", "message": str(error)},
                )
                return
            self._send_json(
                status=200,
                payload={
                    "ok": True,
                    "action": action,
                    "changed": changed,
                    "stats": alert_stats(),
                },
            )

        def _handle_housekeeping_post(
            self,
            monitor: HousekeepingMonitor | None,
        ) -> None:
            """处理会话归档、清理和恢复请求；必须显式确认。"""

            if monitor is None:
                self._send_json(
                    status=503,
                    payload={"error": "housekeeping_unavailable"},
                )
                return
            try:
                body = self._read_json_body()
                action = str(body.get("action") or "").strip()
                days = _bounded_int(body.get("days"), maximum=3650) or 30
                min_bytes = int(
                    float(body.get("min_size_mb") or 0) * 1024 * 1024
                )
                session_path = str(body.get("session") or "").strip()
                if session_path:
                    # 中文注释：单个会话先确认它确实在自己的可归档目录里，
                    # 不接受网页传来的任意路径。
                    state = monitor.session_archive_state([session_path])
                    entry = state.get(str(Path(session_path).expanduser()))
                    if entry is None or not entry.get("eligible"):
                        reason = (entry or {}).get("reason") or "会话不存在"
                        raise HousekeepingError(f"该会话当前不能归档：{reason}")
                    criteria = CleanupCriteria(paths=(session_path,))
                else:
                    criteria = CleanupCriteria(
                        older_than_days=days,
                        min_bytes=max(0, min_bytes),
                    )
                if action == "archive" and body.get("async") is True:
                    if body.get("confirm") is not True:
                        raise HousekeepingError("归档需要确认")
                    task = monitor.start_task("archive", criteria)
                    self._send_json(
                        status=200,
                        payload={
                            "ok": True,
                            "action": action,
                            "task": task,
                            "report": monitor.latest(),
                            "preview": monitor.preview(criteria),
                            "archives": list(monitor.restores()),
                        },
                    )
                    return
                if action == "clean" and body.get("async") is True:
                    if body.get("confirm") is not True:
                        raise HousekeepingError("清理需要确认")
                    task = monitor.start_task("clean", criteria)
                    self._send_json(
                        status=200,
                        payload={
                            "ok": True,
                            "action": action,
                            "task": task,
                            "report": monitor.latest(),
                            "preview": monitor.preview(criteria),
                            "archives": list(monitor.restores()),
                        },
                    )
                    return
                if action == "archive":
                    if body.get("confirm") is not True:
                        raise HousekeepingError("归档需要确认")
                    result = monitor.archive(criteria, confirm=True)
                elif action == "clean":
                    if body.get("confirm") is not True:
                        raise HousekeepingError("清理需要确认")
                    result = monitor.clean(criteria, confirm=True)
                elif action == "restore":
                    archive = _archive_path_for(
                        monitor,
                        str(body.get("archive") or ""),
                    )
                    result = monitor.restore(archive)
                else:
                    raise HousekeepingError(f"未知操作: {action or '(空)'}")
            except (HousekeepingError, ValueError) as error:
                self._send_json(
                    status=400,
                    payload={"error": "invalid_housekeeping_action", "message": str(error)},
                )
                return
            report = monitor.refresh(force=True)
            self._send_json(
                status=200,
                payload={
                    "ok": True,
                    "action": action,
                    "result": result,
                    "report": report,
                    "preview": monitor.preview(criteria),
                    "archives": list(monitor.restores()),
                },
            )

        def _handle_scan_dirs_post(
            self,
            controller: ScanDirsController | None,
        ) -> None:
            """处理扫描目录的添加、移除和恢复默认；移除与恢复需显式确认。"""

            if controller is None:
                self._send_json(
                    status=503,
                    payload={"error": "scan_dirs_unavailable"},
                )
                return
            try:
                body = self._read_json_body()
            except AlertStoreError as error:
                self._send_json(
                    status=400,
                    payload={
                        "error": "invalid_scan_dir_action",
                        "message": str(error),
                    },
                )
                return
            action = str(body.get("action") or "").strip()
            if action not in {"add", "remove", "reset"}:
                self._send_json(
                    status=400,
                    payload={
                        "error": "invalid_scan_dir_action",
                        "message": f"未知操作: {action or '(空)'}",
                    },
                )
                return
            provider = body.get("provider")
            if not isinstance(provider, str) or provider not in PROVIDER_SPECS:
                self._send_json(
                    status=400,
                    payload={
                        "error": "invalid_scan_dir_action",
                        "message": "未知或未提供的 provider",
                    },
                )
                return
            if action in {"remove", "reset"} and body.get("confirm") is not True:
                self._send_json(
                    status=400,
                    payload={
                        "error": "invalid_scan_dir_action",
                        "message": "移除和恢复默认需要 confirm: true",
                    },
                )
                return
            path: Path | None = None
            if action in {"add", "remove"}:
                raw_path = body.get("path")
                if not isinstance(raw_path, str) or not raw_path.strip():
                    self._send_json(
                        status=400,
                        payload={
                            "error": "invalid_scan_dir_action",
                            "message": f"{action} 操作需要字符串形式的 path",
                        },
                    )
                    return
                path = Path(raw_path)
            try:
                snapshot = controller.apply(action, provider, path)
            except ScanDirsError as error:
                self._send_json(
                    status=400,
                    payload={"error": "invalid_scan_dir", "message": str(error)},
                )
                return
            self._send_json(
                status=200,
                payload={"ok": True, "action": action, **snapshot},
            )

        def _handle_history_post(
            self,
            manager: HistoryDataManager | None,
            controller: RetentionController | None,
        ) -> None:
            """处理历史数据清理和保留期配置；清理与恢复默认需显式确认。"""

            if manager is None:
                self._send_json(
                    status=503,
                    payload={"error": "history_unavailable"},
                )
                return
            try:
                body = self._read_json_body()
            except AlertStoreError as error:
                self._send_json(
                    status=400,
                    payload={
                        "error": "invalid_history_action",
                        "message": str(error),
                    },
                )
                return
            action = str(body.get("action") or "").strip()
            if action == "cleanup":
                if body.get("confirm") is not True:
                    self._send_json(
                        status=400,
                        payload={
                            "error": "invalid_history_action",
                            "message": "清理需要 confirm: true",
                        },
                    )
                    return
                try:
                    result = manager.cleanup()
                except RetentionError as error:
                    # 中文注释：部分库清理失败时仍回传已完成的部分结果。
                    self._send_json(
                        status=500,
                        payload={
                            "error": "history_cleanup_failed",
                            "message": sanitize_error(error),
                            "result": manager.last_cleanup,
                        },
                    )
                    return
                self._send_json(
                    status=200,
                    payload={"ok": True, "action": action, "result": result},
                )
                return
            if action in {"set-retention", "reset-retention"}:
                if controller is None:
                    self._send_json(
                        status=503,
                        payload={"error": "retention_unavailable"},
                    )
                    return
                if action == "reset-retention":
                    if body.get("confirm") is not True:
                        self._send_json(
                            status=400,
                            payload={
                                "error": "invalid_history_action",
                                "message": "恢复默认需要 confirm: true",
                            },
                        )
                        return
                    try:
                        snapshot = controller.apply("reset")
                    except RetentionError as error:
                        self._send_json(
                            status=400,
                            payload={
                                "error": "invalid_retention",
                                "message": str(error),
                            },
                        )
                        return
                    self._send_json(
                        status=200,
                        payload={"ok": True, "action": action, **snapshot},
                    )
                    return
                usage_days = body.get("usage_days")
                session_days = body.get("session_days")
                if usage_days is None and session_days is None:
                    self._send_json(
                        status=400,
                        payload={
                            "error": "invalid_retention",
                            "message": "至少提供 usage_days 或 session_days 之一",
                        },
                    )
                    return
                for name, value in (
                    ("usage_days", usage_days),
                    ("session_days", session_days),
                ):
                    if value is not None and (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                    ):
                        self._send_json(
                            status=400,
                            payload={
                                "error": "invalid_retention",
                                "message": f"{name} 必须是数值",
                            },
                        )
                        return
                try:
                    snapshot = controller.apply(
                        "set",
                        usage_days=usage_days,
                        session_days=session_days,
                    )
                except RetentionError as error:
                    self._send_json(
                        status=400,
                        payload={"error": "invalid_retention", "message": str(error)},
                    )
                    return
                self._send_json(
                    status=200,
                    payload={"ok": True, "action": action, **snapshot},
                )
                return
            self._send_json(
                status=400,
                payload={
                    "error": "invalid_history_action",
                    "message": f"未知操作: {action or '(空)'}",
                },
            )

        def _read_json_body(self) -> dict[str, Any]:
            """读取并校验 JSON 请求体，限制大小和内容类型。"""

            content_type = (
                (self.headers.get("Content-Type") or "")
                .split(";")[0]
                .strip()
                .lower()
            )
            if content_type != "application/json":
                raise AlertStoreError("请求体必须是 application/json")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as error:
                raise AlertStoreError("Content-Length 非法") from error
            if length <= 0 or length > _MAX_REQUEST_BYTES:
                raise AlertStoreError("请求体大小非法")
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise AlertStoreError(f"请求体不是合法 JSON: {error}") from error
            if not isinstance(payload, dict):
                raise AlertStoreError("请求体顶层必须是 JSON 对象")
            return payload

        def log_message(self, format: str, *args: object) -> None:
            """把 HTTP 访问日志交给监控器日志，不污染标准输出。"""

            logger.debug("Dashboard HTTP " + format, *args)

        def _request_language(self) -> str:
            """当前请求的语言。

            优先级：显式 ``?lang=`` > 顶栏开关写入的 cookie > ``Accept-Language`` > 中文。
            cookie 让「手动切换」对后续接口请求同样生效，不必给每个 fetch 加参数。
            """

            try:
                query = parse_qs(urlsplit(self.path).query)
            except ValueError:
                query = {}
            override = (query.get("lang") or [None])[0]
            header = (
                self.headers.get("Accept-Language")
                if hasattr(self, "headers")
                else None
            )
            cookie = self._language_cookie() if override is None else None
            return resolve_language(
                accept_language=header,
                override=override or cookie,
            )

        def _language_cookie(self) -> str | None:
            """读取顶栏语言开关写入的 cookie。"""

            header = self.headers.get("Cookie") if hasattr(self, "headers") else None
            if not header:
                return None
            for item in header.split(";"):
                name, _, value = item.strip().partition("=")
                if name == _LANGUAGE_COOKIE_NAME:
                    return value.strip() or None
            return None

        def _localized(self, payload: object) -> object:
            """按请求语言本地化负载里的字符串值（字典键与结构不动）。"""

            language = self._request_language()
            return localize_payload(payload, language)

        def _send_json(
            self,
            status: int,
            payload: object,
            include_body: bool = True,
        ) -> None:
            """发送 JSON 响应（英文请求会把负载里的文案换成英文）。"""

            body = json.dumps(
                self._localized(payload),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self._send_bytes(
                status=status,
                content_type="application/json; charset=utf-8",
                body=body,
                include_body=include_body,
            )

        def _send_bytes(
            self,
            status: int,
            content_type: str,
            body: bytes,
            include_body: bool = True,
            cache_seconds: int | None = None,
        ) -> None:
            """发送带有本地安全响应头的字节响应。

            ``cache_seconds`` 只给内容寻址的静态资源（例如带版本号的 favicon）用；
            监控状态与页面默认一律 ``no-store``，避免浏览器缓存出过期的监控数据。
            """

            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if cache_seconds is None:
                self.send_header("Cache-Control", "no-store")
            else:
                self.send_header(
                    "Cache-Control",
                    f"public, max-age={int(cache_seconds)}",
                )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; connect-src 'self'; "
                "base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            if include_body:
                self.wfile.write(body)

    return DashboardRequestHandler
