"""本地只读 Dashboard：展示额度、活动会话和本地用量。"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from .dsh import (
    list_dsh_active_sessions,
    read_dsh_account,
    read_dsh_quota,
    resolve_dsh_homes,
)
from .grok import read_grok_account, read_grok_quota, resolve_grok_homes
from .kimi import (
    list_kimi_active_sessions,
    read_kimi_account,
    read_kimi_quota,
    resolve_kimi_homes,
)
from .multi_models import TrackedSession
from .quota import QuotaSnapshot
from .registry import MultiSessionRegistry, RegistryError
from .traffic import TrafficMonitor, TrafficSnapshot, empty_traffic_snapshot
from .usage import UsageAggregator


_DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <title>Codex Reset Monitor</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #10141c;
      --panel: #181f2b;
      --panel-soft: #202a39;
      --text: #edf2f7;
      --muted: #9eabbc;
      --line: #303d50;
      --green: #55d187;
      --yellow: #f5c451;
      --red: #f47f7f;
      --blue: #82b7ff;
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
    #overview, #accounts, #usage { scroll-margin-top: 24px; }
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
      background: #3a2028;
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
    .quota-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 12px;
    }
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
    .account-block .quota-grid { grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
    .account-block .table-wrap { margin: 0 -8px -8px; }
    .quota-card { padding: 16px; }
    .quota-title { display: flex; justify-content: space-between; gap: 8px; }
    .quota-name { font-weight: 700; }
    .quota-percent { font-size: 20px; font-weight: 700; }
    .bar { height: 8px; margin: 12px 0; border-radius: 99px; background: var(--panel-soft); overflow: hidden; }
    .bar > span { display: block; height: 100%; border-radius: inherit; background: var(--green); }
    .bar > span.warn { background: var(--yellow); }
    .bar > span.danger { background: var(--red); }
    .quota-meta { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; color: var(--muted); font-size: 12px; }
    .quota-meta dd { margin: 0; color: var(--text); text-align: right; }
    .panel { margin-top: 24px; padding: 18px; overflow: hidden; }
    .panel-heading { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 900px; }
    th, td { padding: 10px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: 12px; font-weight: 600; white-space: nowrap; }
    td { font-size: 13px; }
    .session-id { color: var(--blue); font-family: ui-monospace, SFMono-Regular, monospace; }
    .cwd, .event, .error-text { max-width: 340px; overflow-wrap: anywhere; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 99px; font-size: 12px; white-space: nowrap; }
    .pill.running { color: var(--green); background: #163725; }
    .pill.limit_blocked { color: var(--yellow); background: #3b3218; }
    .pill.waiting_for_approval { color: var(--blue); background: #1b304c; }
    .pill.failed, .pill.orphaned { color: var(--red); background: #3a2028; }
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
    .usage-tab.selected { border-color: var(--blue); color: var(--text); background: #1b304c; }
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
    .usage-model { color: var(--blue); font-family: ui-monospace, SFMono-Regular, monospace; }
    .usage-number { white-space: nowrap; }
    @media (max-width: 900px) {
      .app-shell { display: block; }
      .sidebar { position: static; min-height: 0; margin-bottom: 16px; }
      .sidebar-brand { padding-bottom: 10px; }
      .sidebar nav { display: flex; overflow-x: auto; }
      .sidebar-link { flex: 0 0 auto; white-space: nowrap; }
      .sidebar-foot { display: none; }
    }
    @media (max-width: 760px) {
      body { padding: 14px; }
      header { align-items: start; flex-direction: column; }
      .cards { grid-template-columns: repeat(2, minmax(130px, 1fr)); }
    }

    /* Stitch 运维控制台视觉：用深色 graphite、violet 主色和 cyan 状态色重排信息层级。 */
    :root {
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
    }
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
    .brand-mark {
      display: grid;
      width: 36px;
      height: 36px;
      flex: 0 0 auto;
      place-items: center;
      border: 1px solid #a78bfa;
      border-radius: 10px;
      color: white;
      background: var(--violet);
      box-shadow: 0 8px 20px rgba(139, 92, 246, .24);
    }
    .brand-mark svg { width: 20px; height: 20px; }
    .brand-name { font-size: 15px; font-weight: 750; letter-spacing: -.02em; }
    .brand-name span { color: #b9a4ff; }
    .sidebar-brand small { display: block; margin-top: 2px; color: var(--muted); font-size: 11px; font-weight: 400; }
    .sidebar-label { padding: 0 11px 9px; color: #64778b; font-size: 10px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
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
    .sidebar-link.active { border-color: #3b2b69; color: #f4f0ff; background: var(--violet-soft); box-shadow: inset 2px 0 0 var(--violet); }
    .sidebar-link.active svg { color: #b9a4ff; opacity: 1; }
    .nav-count { min-width: 22px; margin-left: auto; padding: 1px 6px; border: 1px solid var(--line); border-radius: 99px; color: var(--muted-strong); font-size: 11px; text-align: center; }
    .sidebar-bottom { display: grid; gap: 8px; margin-top: auto; padding: 14px 11px 0; border-top: 1px solid var(--line-soft); }
    .service-state, .live-indicator { display: inline-flex; align-items: center; gap: 7px; color: var(--muted-strong); font-size: 12px; }
    .status-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--green); box-shadow: 0 0 0 4px rgba(52, 211, 153, .1); }
    .status-dot.error { background: var(--red); box-shadow: 0 0 0 4px rgba(251, 113, 133, .1); }
    .sidebar-foot { margin: 0; color: #66798d; font-size: 11px; }
    #overview, #accounts, #usage, #insights, #traffic { scroll-margin-top: 88px; }
    .topbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 72px; margin-bottom: 34px; border-bottom: 1px solid var(--line-soft); }
    .breadcrumb { display: flex; align-items: center; gap: 9px; color: var(--muted); font-size: 12px; }
    .breadcrumb strong { color: var(--muted-strong); font-weight: 600; }
    .breadcrumb-separator { color: #425366; }
    .topbar-actions { display: flex; align-items: center; gap: 15px; }
    .refresh-button { display: inline-flex; align-items: center; gap: 7px; padding: 7px 10px; border: 1px solid var(--line); border-radius: 7px; color: var(--muted-strong); background: var(--panel); cursor: pointer; transition: border-color .15s ease, color .15s ease, background .15s ease; }
    .refresh-button:hover { border-color: var(--violet); color: var(--text); background: var(--surface-raised); }
    .refresh-button:disabled { cursor: wait; opacity: .7; }
    .refresh-button svg { width: 15px; height: 15px; }
    .refresh-button.is-spinning svg { animation: spin .8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .page-hero { display: flex; align-items: end; justify-content: space-between; gap: 24px; margin-bottom: 25px; }
    .eyebrow, .section-kicker { margin-bottom: 7px; color: var(--cyan); font-size: 10px; font-weight: 750; letter-spacing: .13em; text-transform: uppercase; }
    h1 { margin: 0; font-size: 32px; letter-spacing: -.04em; line-height: 1.15; }
    h2 { margin: 0; font-size: 18px; letter-spacing: -.025em; }
    h3 { margin: 0; }
    .hero-description { margin: 8px 0 0; color: var(--muted); }
    .hero-context { display: flex; align-items: center; gap: 10px; padding-bottom: 3px; }
    .scope-badge { display: inline-flex; align-items: center; gap: 8px; padding: 7px 10px; border: 1px solid var(--line); border-radius: 7px; color: var(--muted-strong); background: var(--panel); font-size: 12px; }
    .scope-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--cyan); }
    .muted { color: var(--muted); }
    .error { display: none; margin: 0 0 20px; padding: 12px 14px; border: 1px solid #6c2e43; border-radius: 9px; color: #fda4af; background: var(--red-soft); }
    .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 13px; margin-bottom: 34px; }
    .card, .panel, .kpi-card { border: 1px solid var(--line); border-radius: 12px; background: var(--panel); }
    .kpi-card { position: relative; min-height: 142px; padding: 17px 17px 14px; overflow: hidden; box-shadow: var(--shadow); }
    .kpi-card::after { position: absolute; top: 0; right: 20px; left: 20px; height: 2px; background: var(--accent); content: ''; opacity: .82; }
    .kpi-card.accent-violet { --accent: var(--violet); }
    .kpi-card.accent-yellow { --accent: var(--yellow); }
    .kpi-card.accent-cyan { --accent: var(--cyan); }
    .kpi-card.accent-green { --accent: var(--green); }
    .kpi-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .kpi-label { color: var(--muted); font-size: 12px; }
    .kpi-icon { display: grid; width: 28px; height: 28px; place-items: center; border-radius: 7px; color: var(--accent); background: rgba(255, 255, 255, .045); }
    .kpi-icon svg { width: 16px; height: 16px; }
    .kpi-value { margin-top: 8px; font-size: 28px; font-weight: 750; letter-spacing: -.045em; }
    .kpi-foot { display: flex; align-items: center; gap: 5px; margin-top: 9px; color: var(--muted); font-size: 11px; }
    .kpi-foot strong { color: var(--muted-strong); font-weight: 600; }
    .section-block { margin-top: 24px; padding: 21px; overflow: hidden; }
    .panel-heading { display: flex; align-items: end; justify-content: space-between; gap: 18px; margin-bottom: 19px; }
    .section-description { margin: 5px 0 0; color: var(--muted); font-size: 12px; }
    .section-meta { display: flex; align-items: center; gap: 12px; color: var(--muted); font-size: 12px; }
    .section-count { padding: 4px 8px; border: 1px solid var(--line); border-radius: 99px; color: var(--muted-strong); white-space: nowrap; }
    .account-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 15px; align-items: start; }
    .account-block { padding: 20px 21px 21px; border: 1px solid var(--line); border-radius: 11px; background: var(--surface-raised); }
    .account-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; padding-bottom: 17px; border-bottom: 1px solid var(--line-soft); }
    .account-identity { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .account-avatar { display: grid; width: 38px; height: 38px; flex: 0 0 auto; place-items: center; border: 1px solid #3d3170; border-radius: 10px; color: #c4b5fd; background: var(--violet-soft); font-size: 13px; font-weight: 750; }
    .account-label { color: var(--cyan); font-size: 10px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; }
    .account-title { margin: 2px 0 3px; font-size: 17px; letter-spacing: -.02em; overflow-wrap: anywhere; }
    .account-meta { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
    .account-side { display: grid; justify-items: end; gap: 7px; text-align: right; }
    .plan-badge { padding: 4px 8px; border: 1px solid #345064; border-radius: 6px; color: var(--cyan); background: var(--cyan-soft); font-size: 11px; }
    .account-activity { color: var(--muted); font-size: 11px; }
    .account-subtitle { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 18px 0 10px; color: var(--muted-strong); font-size: 12px; font-weight: 650; }
    .account-block .account-subtitle:first-of-type { margin-top: 17px; }
    .quota-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(245px, 1fr)); gap: 11px; }
    .quota-card { padding: 15px; border: 1px solid var(--line); border-radius: 9px; background: var(--panel); }
    .quota-title { display: flex; align-items: start; justify-content: space-between; gap: 10px; }
    .quota-name { color: var(--muted-strong); font-size: 12px; font-weight: 650; overflow-wrap: anywhere; }
    .quota-percent { color: var(--text); font-size: 20px; font-weight: 750; letter-spacing: -.04em; }
    .bar { height: 6px; margin: 13px 0 12px; border-radius: 99px; background: #202e3d; overflow: hidden; }
    .bar > span { display: block; height: 100%; border-radius: inherit; background: var(--green); }
    .bar > span.warn { background: var(--yellow); }
    .bar > span.danger { background: var(--red); }
    .quota-meta { display: grid; grid-template-columns: auto 1fr; gap: 4px 10px; margin: 0; color: var(--muted); font-size: 11px; }
    .quota-meta dt, .quota-meta dd { margin: 0; }
    .quota-meta dd { color: var(--muted-strong); text-align: right; overflow-wrap: anywhere; }
    .table-wrap { overflow-x: auto; border: 1px solid var(--line-soft); border-radius: 9px; background: var(--panel); }
    table { width: 100%; min-width: 900px; border-collapse: collapse; }
    th, td { padding: 11px 12px; border-bottom: 1px solid var(--line-soft); text-align: left; vertical-align: top; }
    tbody tr:last-child td { border-bottom: 0; }
    tbody tr:hover { background: rgba(255, 255, 255, .018); }
    th { color: #71859a; font-size: 10px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; white-space: nowrap; }
    td { color: var(--muted-strong); font-size: 12px; }
    .session-table table { min-width: 1020px; }
    .session-id { color: var(--cyan); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; overflow-wrap: anywhere; }
    .cwd, .event, .error-text { max-width: 340px; overflow-wrap: anywhere; }
    td .muted { margin-top: 2px; font-size: 11px; }
    .pill { display: inline-block; padding: 3px 8px; border-radius: 99px; font-size: 11px; white-space: nowrap; }
    .pill.running { color: var(--green); background: var(--green-soft); }
    .pill.limit_blocked { color: var(--yellow); background: var(--yellow-soft); }
    .pill.waiting_for_approval { color: var(--blue); background: var(--blue-soft); }
    .pill.failed, .pill.orphaned, .pill.danger { color: var(--red); background: var(--red-soft); }
    .pill.warn { color: var(--yellow); background: var(--yellow-soft); }
    .pill.ok { color: var(--green); background: var(--green-soft); }
    .pill.other { color: var(--muted); background: var(--panel-soft); }
    .traffic-table table { min-width: 1080px; }
    .traffic-remote { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; overflow-wrap: anywhere; }
    .usage-tabs { display: flex; flex-wrap: wrap; gap: 7px; margin: 0 0 14px; }
    .usage-tab { padding: 7px 12px; border: 1px solid var(--line); border-radius: 7px; color: var(--muted); background: var(--surface-raised); cursor: pointer; }
    .usage-tab:hover { border-color: #5541a0; color: var(--muted-strong); }
    .usage-tab.selected { border-color: #654bc0; color: #eee9ff; background: var(--violet-soft); }
    .session-toggle { margin: 10px 0 0; padding: 6px 10px; border: 1px solid var(--line); border-radius: 7px; color: var(--muted-strong); background: var(--surface-raised); cursor: pointer; font-size: 12px; }
    .session-toggle:hover { border-color: #5541a0; background: var(--surface-hover); }
    .usage-filters { display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 14px; padding: 12px; border: 1px solid var(--line-soft); border-radius: 9px; background: var(--surface-raised); }
    .usage-filter { display: grid; gap: 5px; color: var(--muted); font-size: 11px; }
    .usage-filter select { min-width: 220px; padding: 8px 30px 8px 10px; border: 1px solid var(--line); border-radius: 7px; color: var(--text); background: var(--panel); cursor: pointer; }
    .usage-filter select:focus, .refresh-button:focus-visible, .usage-tab:focus-visible, .sidebar-link:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
    .usage-summary { display: grid; grid-template-columns: repeat(3, minmax(130px, 1fr)); gap: 10px; margin-bottom: 14px; }
    .usage-summary-item { padding: 12px 13px; border: 1px solid var(--line-soft); border-radius: 8px; background: var(--surface-raised); }
    .usage-summary-label { color: var(--muted); font-size: 11px; }
    .usage-summary-value { margin-top: 3px; color: var(--text); font-size: 17px; font-weight: 700; }
    .usage-note { margin: 0 0 13px; color: var(--muted); font-size: 11px; }
    .usage-table table { min-width: 1180px; }
    .usage-models { display: grid; gap: 3px; min-width: 180px; }
    .usage-model { color: var(--cyan); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
    .usage-number { white-space: nowrap; }
    #alert-list { display: grid; gap: 8px; margin-bottom: 20px; }
    #alert-list:empty { display: none; }
    .alert-list { display: grid; gap: 8px; }
    .alert-banner { padding: 11px 13px; border: 1px solid var(--line); border-radius: 9px; color: var(--muted-strong); background: var(--surface-raised); font-size: 12px; }
    .alert-banner.warn { border-color: #7a5410; color: #fcd34d; background: var(--yellow-soft); }
    .alert-banner.danger { border-color: #6c2e43; color: #fda4af; background: var(--red-soft); }
    .alert-banner.tip { border-color: #2c4a78; color: #93c5fd; background: var(--blue-soft); }
    .alert-banner.info { border-color: var(--line); color: var(--muted-strong); background: var(--surface-raised); }
    .suggestion-saving { float: right; margin-left: 10px; color: var(--green); font-weight: 700; white-space: nowrap; }
    .insights-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; margin-bottom: 14px; }
    .observation-list { margin: 0; padding-left: 18px; color: var(--muted-strong); font-size: 12px; }
    .observation-list li { padding: 2px 0; }
    .usage-trend, .usage-top-projects { margin: 0 0 14px; padding: 13px; border: 1px solid var(--line-soft); border-radius: 9px; background: var(--surface-raised); }
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
    .empty-state { padding: 26px 12px; color: var(--muted); text-align: center; }
    @media (max-width: 1100px) {
      main { padding-right: 28px; padding-left: 28px; }
      .sidebar { width: 208px; }
      .app-shell { grid-template-columns: 208px minmax(0, 1fr); }
      .kpi-grid { grid-template-columns: repeat(2, minmax(150px, 1fr)); }
    }
    @media (max-width: 900px) {
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
      .usage-summary { grid-template-columns: 1fr; }
      .usage-filter, .usage-filter select { width: 100%; min-width: 0; }
    }
    @media (max-width: 430px) {
      .kpi-grid { grid-template-columns: 1fr; }
      .breadcrumb { font-size: 11px; }
      .refresh-button span { display: none; }
    }
  </style>
</head>
<body>
<div class="app-shell">
  <aside class="sidebar" aria-label="Dashboard 导航">
    <div class="sidebar-brand">
      <div class="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 4.5h10v15H7z"/><path d="M10 8h4M10 12h4M10 16h2"/></svg>
      </div>
      <div><div class="brand-name">Codex <span>Monitor</span></div><small>本地运维控制台</small></div>
    </div>
    <div class="sidebar-label">工作台</div>
    <nav>
      <a class="sidebar-link" data-nav-target="traffic" href="#traffic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12h16"/><path d="M13 5l7 7-7 7"/></svg><span>异常流量监控</span></a>
      <a class="sidebar-link active" data-nav-target="overview" href="#overview"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><rect x="14" y="14" width="6" height="6" rx="1"/></svg><span>总览</span></a>
      <a class="sidebar-link" data-nav-target="accounts" href="#accounts"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="3"/><path d="M5 20c.8-3.4 3.1-5 7-5s6.2 1.6 7 5"/></svg><span>账号与额度</span></a>
      <a class="sidebar-link" data-nav-target="usage" href="#usage"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 19V9M12 19V5M19 19v-7"/><path d="M3 19h18"/></svg><span>用量与费用</span></a>
      <a class="sidebar-link" data-nav-target="insights" href="#insights"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18h6M10 21.5h4"/><path d="M12 3a6 6 0 0 0-3.3 11.1c.8.5 1.3 1.3 1.3 2.2v.7h4v-.7c0-.9.5-1.7 1.3-2.2A6 6 0 0 0 12 3z"/></svg><span>习惯分析</span></a>
    </nav>
    <div class="sidebar-bottom">
      <div class="service-state"><span id="service-dot" class="status-dot"></span><span id="service-state">监控服务在线</span></div>
      <div class="sidebar-foot">状态每 5 秒同步 · 只读模式</div>
    </div>
  </aside>

  <main>
    <header class="topbar">
      <div class="breadcrumb"><span>Codex Monitor</span><span class="breadcrumb-separator">/</span><strong>Dashboard</strong></div>
      <div class="topbar-actions">
        <div class="live-indicator"><span class="status-dot"></span><span id="service-sync">等待首次同步</span></div>
        <button id="refresh-button" class="refresh-button" type="button" aria-label="立即刷新状态"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14.7-4L4 9"/><path d="M4 4v5h5"/><path d="M4 13a8 8 0 0 0 14.7 4L20 15"/><path d="M20 20v-5h-5"/></svg><span>刷新</span></button>
      </div>
    </header>

    <section id="overview" class="page-hero">
      <div>
        <div class="eyebrow">实时监控 · Codex Plus</div>
        <h1>运行概览</h1>
        <p class="hero-description">实时查看账号额度、活动会话、本地用量，以及异常流量监控。</p>
      </div>
      <div class="hero-context"><span class="scope-badge"><span class="scope-dot"></span><span id="quota-source">正在识别账号</span></span></div>
    </section>

    <div id="error" class="error" role="alert"></div>
    <div id="alert-list"></div>

    <section class="kpi-grid" aria-label="监控摘要">
      <article class="kpi-card accent-cyan"><div class="kpi-top"><span class="kpi-label">近 15 秒外发</span><span class="kpi-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12h16M13 5l7 7-7 7"/></svg></span></div><div id="upload-burst-count" class="kpi-value">—</div><div class="kpi-foot"><strong id="upload-alert-count">—</strong> 条异常流量告警</div></article>
      <article class="kpi-card accent-violet"><div class="kpi-top"><span class="kpi-label">活动会话</span><span class="kpi-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="3"/><path d="M5 20c.8-3.4 3.1-5 7-5s6.2 1.6 7 5"/></svg></span></div><div id="active-count" class="kpi-value">—</div><div class="kpi-foot"><strong id="process-count">—</strong> 个有进程证据</div></article>
      <article class="kpi-card accent-yellow"><div class="kpi-top"><span class="kpi-label">额度窗口</span><span class="kpi-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 10h8M8 14h5"/></svg></span></div><div id="quota-window-count" class="kpi-value">—</div><div class="kpi-foot">当前账号可见的额度窗口</div></article>
      <article class="kpi-card accent-cyan"><div class="kpi-top"><span class="kpi-label">监控账号</span><span class="kpi-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 10h8M8 14h5"/></svg></span></div><div id="accounts-online" class="kpi-value">—</div><div class="kpi-foot">按真实账号 ID 归组</div></article>
      <article class="kpi-card accent-green"><div class="kpi-top"><span class="kpi-label">今日 API 等价金额</span><span class="kpi-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M14.8 9.5c-.6-.7-1.5-1-2.7-1-1.4 0-2.3.7-2.3 1.7 0 2.5 5 1.2 5 3.8 0 1-.9 1.7-2.5 1.7-1.2 0-2.2-.4-2.9-1.2M12 6.7v1.8M12 15.7v1.8"/></svg></span></div><div id="spend-count" class="kpi-value">—</div><div class="kpi-foot">非 Plus 实际账单；索引完成后显示</div></article>
    </section>

    <section id="traffic" class="panel section-block">
      <div class="panel-heading">
        <div><div class="section-kicker">Traffic anomaly</div><h2>异常流量监控</h2><p class="section-description">按进程统计 Codex / Grok / Kimi / DeepSeek Harness 等 CLI 的 TCP 外发增量，发现异常大数据上传。回环流量不计入告警；不读取连接内容。</p></div>
        <div class="section-meta"><span class="section-count" id="traffic-section-count">等待扫描</span></div>
      </div>
      <div id="traffic-content"><div class="empty-state">正在扫描本机 code agent 异常流量…</div></div>
    </section>

    <section id="accounts" class="panel section-block">
      <div class="panel-heading">
        <div><div class="section-kicker">Account health</div><h2>账号与额度</h2><p class="section-description">看板卡片按真实账号 ID 分组，profile 混合登录也不会串额；活动会话默认折叠，可点击再展开。</p></div>
        <div class="section-meta"><span class="section-count" id="account-section-count">— 个账号</span></div>
      </div>
      <div id="account-list" class="account-list"><div class="empty-state">正在读取账号状态…</div></div>
    </section>

    <section id="usage" class="panel section-block">
      <div class="panel-heading">
        <div><div class="section-kicker">Usage analytics</div><h2>用量与成本估算</h2><p class="section-description">按 Codex、Grok、Kimi 和 DeepSeek Harness 账号、模型、项目 / 工作目录汇总本地用量。</p></div>
        <div class="section-meta"><span class="section-count">按需统计 · 缓存 5 分钟</span><button id="usage-load-button" class="refresh-button" type="button">加载用量</button></div>
      </div>
      <div id="usage-content"><div class="empty-state">为避免周期读取大量历史 JSONL，用量统计改为按需加载。</div></div>
    </section>
    <section id="insights" class="panel section-block">
      <div class="panel-heading">
        <div><div class="section-kicker">Insights</div><h2>习惯分析与省 token 建议</h2><p class="section-description">基于已索引对话的 token 元数据提炼使用习惯，可按时间段切换；不读取对话内容。</p></div>
        <div class="section-meta"><span class="section-count">按需分析 · 基于用量索引</span><button id="insights-load-button" class="refresh-button" type="button">生成分析</button></div>
      </div>
      <div class="usage-tabs" id="insights-period-tabs">
        <button class="usage-tab selected" type="button" data-insights-days="">全部</button>
        <button class="usage-tab" type="button" data-insights-days="7">近 7 天</button>
        <button class="usage-tab" type="button" data-insights-days="30">近 30 天</button>
      </div>
      <div id="insights-content"><div class="empty-state">点击「生成分析」，对已索引的对话做使用习惯画像并给出省 token 建议。</div></div>
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
  const formatReset = (seconds) => {
    if (seconds === null || seconds === undefined) return '未知';
    const remaining = Math.round(Number(seconds) - Date.now() / 1000);
    const suffix = remaining > 0 ? `（${Math.ceil(remaining / 60)} 分钟后）` : '（已到时间）';
    return `${formatTime(seconds)} ${suffix}`;
  };
  const formatPercent = (value) => value === null || value === undefined ? '未知' : `${Number(value).toFixed(1)}%`;
  const formatNumber = (value) => Number(value || 0).toLocaleString('zh-CN');
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
  const formatUsdCompact = (value) => value === null || value === undefined ? '未计价' : `$${Number(value).toFixed(2)}`;
  const accountInitials = (value) => String(value || '?').trim().split(/\s+/).map((part) => part[0]).join('').slice(0, 2).toUpperCase() || '?';
  const statusClass = (status) => ['running', 'limit_blocked', 'waiting_for_approval', 'failed', 'orphaned'].includes(status) ? status : 'other';
  const renderQuotaCards = (quotas) => {
    if (!Array.isArray(quotas) || quotas.length === 0) {
      return '<div class="muted">暂无有效额度窗口</div>';
    }
    return quotas.flatMap((quota) => (quota.windows || []).map((window) => {
      const percent = window.used_percent === null || window.used_percent === undefined ? 0 : Math.max(0, Math.min(100, Number(window.used_percent)));
      const level = window.is_exhausted ? 'danger' : percent >= 80 ? 'warn' : '';
      const state = window.is_exhausted ? '已耗尽' : '可用';
      return `<article class="card quota-card">
        <div class="quota-title"><span class="quota-name">${escapeHtml(window.limit_id)}/${escapeHtml(window.name)}</span><span class="quota-percent">${escapeHtml(formatPercent(window.used_percent))}</span></div>
        <div class="bar"><span class="${level}" style="width:${percent}%"></span></div>
        <dl class="quota-meta"><dt>状态</dt><dd>${state}</dd><dt>窗口</dt><dd>${window.window_minutes !== null && window.window_minutes !== undefined ? escapeHtml(String(window.window_minutes)) + ' 分钟' : '—'}</dd><dt>重置</dt><dd>${escapeHtml(formatReset(window.resets_at))}</dd></dl>
      </article>`;
    })).join('');
  };
  // 活动会话整块默认折叠，点击按钮再展开；折叠状态在 5 秒自动刷新之间保持。
  const expandedSessionTables = new Set();
  const renderSessionTable = (sessions, accountKey) => {
    if (!sessions || sessions.length === 0) {
      return '<div class="empty-state">没有活动会话</div>';
    }
    const renderRow = (session) => {
      const status = escapeHtml(statusNames[session.status] || session.status || '未知');
      const cssStatus = statusClass(session.status);
      const pids = session.pids && session.pids.length ? session.pids.join(', ') : '无';
      const detail = session.last_error || '—';
      return `<tr>
        <td><div class="session-id">${escapeHtml(session.session_id || session.thread_id)}</div><div class="muted">${escapeHtml(session.source)}</div></td>
        <td><span class="pill ${cssStatus}">${status}</span></td>
        <td>${escapeHtml(pids)}<div class="muted">${session.process_backed ? '已绑定 JSONL' : '无进程证据'}</div></td>
        <td class="cwd">${escapeHtml(session.cwd || '未知')}</td>
        <td class="event">${escapeHtml(session.last_event_type || '未知')}<div class="muted">${escapeHtml(formatTime(session.last_event_at))}</div></td>
        <td class="error-text">${escapeHtml(detail)}</td>
      </tr>`;
    };
    const expanded = expandedSessionTables.has(accountKey);
    return `<div class="session-collapsible"${expanded ? '' : ' style="display:none"'}><div class="table-wrap session-table"><table>
      <thead><tr><th>会话</th><th>状态</th><th>进程</th><th>工作目录</th><th>最近事件</th><th>说明</th></tr></thead>
      <tbody>${sessions.map(renderRow).join('')}</tbody>
    </table></div></div>
    <button class="session-toggle" type="button" data-session-toggle="${escapeHtml(accountKey)}" aria-expanded="${expanded}">${expanded ? '收起会话列表' : `展开 ${sessions.length} 个活动会话`}</button>`;
  };
  let selectedUsagePeriod = 'today';
  let selectedUsageModel = '';
  let selectedUsageProject = '';
  let latestState = null;
  let latestUsageState = null;
  let usagePollTimer = 0;
  let latestInsightsState = null;
  let insightsPollTimer = 0;
  let insightsPeriodDays = '';
  const navLinks = [...document.querySelectorAll('[data-nav-target]')];
  const updateActiveNav = () => {
    const current = ['insights', 'usage', 'accounts', 'overview'].find((id) => {
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
  const filteredUsage = (account) => {
    let base = account;
    if (selectedUsageProject) {
      base = (account.projects || []).find((project) => project.project === selectedUsageProject);
      if (!base) return null;
    }
    const models = Array.isArray(base.models) ? base.models : [];
    if (!selectedUsageModel) return base;
    const selectedModels = models.filter((model) => model.model === selectedUsageModel);
    return selectedModels.length ? sumUsageModels(selectedModels) : null;
  };
  const usageFilterControls = (periods) => {
    const models = distinctSorted(periods.flatMap((period) => (period.accounts || []).flatMap((account) => (account.models || []).map((model) => model.model))));
    const projects = distinctSorted(periods.flatMap((period) => (period.accounts || []).flatMap((account) => (account.projects || []).map((project) => project.project))));
    if (!models.includes(selectedUsageModel)) selectedUsageModel = '';
    if (!projects.includes(selectedUsageProject)) selectedUsageProject = '';
    const modelOptions = [`<option value="">全部模型</option>`, ...models.map((model) => `<option value="${escapeHtml(model)}"${model === selectedUsageModel ? ' selected' : ''}>${escapeHtml(model)}</option>`)].join('');
    const projectOptions = [`<option value="">全部项目</option>`, ...projects.map((project) => `<option value="${escapeHtml(project)}"${project === selectedUsageProject ? ' selected' : ''}>${escapeHtml(project)}</option>`)].join('');
    return `<div class="usage-filters">
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
          alerts.push({ level: 'danger', text: `${label} 已耗尽，重置时间 ${formatReset(window.resets_at)}` });
        } else if (window.used_percent !== null && window.used_percent !== undefined && !Number.isNaN(percent) && percent >= 90) {
          alerts.push({ level: 'warn', text: `${label} 已使用 ${percent.toFixed(1)}%，接近上限` });
        }
      });
    });
    const traffic = latestState.traffic || {};
    (traffic.alerts || []).forEach((alert) => {
      if (!alert || !alert.message) return;
      alerts.push({ level: alert.level === 'danger' ? 'danger' : 'warn', text: alert.message });
    });
    const budget = budgetUsd();
    if (budget !== null && latestUsageState && latestUsageState.usage) {
      const spent = monthCost(latestUsageState.usage.periods);
      if (spent !== null) {
        const ratio = (spent / budget) * 100;
        if (ratio >= 100) {
          alerts.push({ level: 'danger', text: `本月 API 等价金额 ${formatUsdCompact(spent)} 已超过月预算 ${formatUsdCompact(budget)}（${ratio.toFixed(0)}%）` });
        } else if (ratio >= 80) {
          alerts.push({ level: 'warn', text: `本月 API 等价金额 ${formatUsdCompact(spent)} 已达月预算 ${formatUsdCompact(budget)} 的 ${ratio.toFixed(0)}%` });
        }
      }
    }
    container.innerHTML = alerts.map((alert) => `<div class="alert-banner ${alert.level}" role="status">${escapeHtml(alert.text)}</div>`).join('');
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
  const renderTopProjects = (accounts) => {
    const rows = [];
    (accounts || []).forEach((account) => {
      const accountName = account.account || account.account_id || '未知账号';
      (account.projects || []).forEach((project) => {
        if (project.estimated_cost_usd === null || project.estimated_cost_usd === undefined) return;
        rows.push({ project: project.project || '未知项目', account: accountName, cost: Number(project.estimated_cost_usd) });
      });
    });
    rows.sort((a, b) => b.cost - a.cost);
    const top = rows.slice(0, 5);
    if (!top.length) return '';
    const max = top[0].cost > 0 ? top[0].cost : 1;
    const items = top.map((item) => {
      const width = Math.max(3, Math.round((item.cost / max) * 100));
      return `<div class="top-project"><div class="top-project-row"><span class="top-project-label">${escapeHtml(item.project)}<span class="muted"> · ${escapeHtml(item.account)}</span></span><span class="top-project-value">${escapeHtml(formatUsdCompact(item.cost))}</span></div><div class="bar"><span style="width:${width}%"></span></div></div>`;
    }).join('');
    return `<div class="usage-top-projects"><div class="usage-trend-head"><span>项目成本排行 Top ${top.length}</span><span class="muted">当前时间范围 · 按 API 等价金额</span></div>${items}</div>`;
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
    const filteredAccounts = accounts.map((account) => ({
      account,
      stats: filteredUsage(account),
    })).filter((item) => item.stats);
    const statsList = filteredAccounts.map((item) => item.stats);
    const totalTokens = statsList.reduce((sum, stats) => sum + Number(stats.total_tokens || 0), 0);
    const totalCredits = sumNullable(statsList, 'estimated_credits');
    const totalUsd = sumNullable(statsList, 'estimated_cost_usd');
    const totalCacheSavings = sumNullable(statsList, 'cache_savings_usd');
    const savingsItem = totalCacheSavings !== null && totalCacheSavings > 0
      ? `<div class="usage-summary-item"><div class="usage-summary-label">缓存节省（等价）</div><div class="usage-summary-value">≈ ${escapeHtml(formatUsdCompact(totalCacheSavings))}</div></div>`
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
      <div class="usage-summary-item"><div class="usage-summary-label">总 token</div><div class="usage-summary-value">${formatNumber(totalTokens)}</div></div>
      <div class="usage-summary-item"><div class="usage-summary-label">API 等价金额</div><div class="usage-summary-value">${escapeHtml(formatUsd(totalUsd))}</div></div>
      ${savingsItem}${budgetItem}
    </div>`;
    const rows = filteredAccounts.map(({ account, stats }) => {
        const models = (stats.models || []).map((model) => `<div><span class="usage-model">${escapeHtml(model.model)}</span> · ${formatNumber(model.total_tokens)} tokens</div>`).join('');
        const profiles = (account.profiles || []).join(' · ') || '未知 profile';
        const pricingWarning = stats.unpriced_models && stats.unpriced_models.length
          ? `<div class="muted">未定价（未计入金额）：${escapeHtml(stats.unpriced_models.join(', '))}</div>`
          : '';
        return `<tr>
          <td><div>${escapeHtml(account.account || account.account_id || '未知账号')}</div><div class="muted">${escapeHtml(profiles)}</div></td>
          <td class="usage-models">${models || '<span class="muted">暂无模型</span>'}${pricingWarning}</td>
          <td class="usage-number">${formatNumber(stats.input_tokens)}</td>
          <td class="usage-number">${formatNumber(stats.cached_input_tokens)}</td>
          <td class="usage-number">${formatNumber(stats.cache_write_input_tokens)}</td>
          <td class="usage-number">${formatNumber(stats.output_tokens)}</td>
          <td class="usage-number">${formatNumber(stats.reasoning_output_tokens)}</td>
          <td class="usage-number">${formatNumber(stats.total_tokens)}</td>
          <td class="usage-number">${formatCredits(stats.estimated_credits)}</td>
          <td class="usage-number">${formatUsd(stats.estimated_cost_usd)}</td>
        </tr>`;
      }).join('');
    container.innerHTML = `${renderTrend(usage.daily)}<div class="usage-tabs">${tabs}</div>
      ${filterControls}${summary}${renderTopProjects(accounts)}<div class="usage-note">${indexingNote}${escapeHtml(note)} 时间范围：${escapeHtml(formatTime(period.start_at))} 至 ${escapeHtml(formatTime(period.end_at))} · credits：${escapeHtml(formatCredits(totalCredits))}</div>${renderKimiReconciliation(periods)}
      <div class="table-wrap usage-table"><table>
        <thead><tr><th>账号 / Profile</th><th>调用模型</th><th>输入 token</th><th>缓存输入</th><th>缓存写入</th><th>输出 token</th><th>推理输出</th><th>总 token</th><th>Plus credits（不可反推）</th><th>API 等价金额</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="10" class="empty-state">这个时间范围没有匹配的模型或项目</td></tr>'}</tbody>
      </table></div>`;
    container.querySelectorAll('[data-usage-period]').forEach((button) => {
      button.addEventListener('click', () => {
        selectedUsagePeriod = button.dataset.usagePeriod || 'today';
        renderUsage(state);
      });
    });
    const modelFilter = document.getElementById('usage-model-filter');
    const projectFilter = document.getElementById('usage-project-filter');
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
    const windowLabel = windowDays > 0 ? `近 ${windowDays} 天` : '全部历史';
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
    const statRow = (label, valueText, width, hint) => `<div class="top-project"><div class="top-project-row"><span class="top-project-label">${escapeHtml(label)}${hint ? `<span class="muted"> · ${escapeHtml(hint)}</span>` : ''}</span><span class="top-project-value">${escapeHtml(valueText)}</span></div><div class="bar"><span style="width:${width}%"></span></div></div>`;
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
    const topRows = topConversations.map((item) => statRow(
      item.label,
      `${formatUsdCompact(item.estimated_cost_usd)} · ${formatNumber(item.total_tokens)} tokens · ${formatNumber(item.turns)} 轮`,
      maxTopCost > 0 ? Math.max(3, Math.round(Number(item.estimated_cost_usd || 0) / maxTopCost * 100)) : 3,
      `${item.account || '未知账号'}${item.project ? ` · ${item.project}` : ''}${item.has_unpriced ? ' · 含未定价模型' : ''}`
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
      container.innerHTML = '<div class="empty-state">无法读取内核 TCP 计数（INET_DIAG）。异常流量监控暂不可用。</div>';
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
    container.innerHTML = `<div class="usage-note">只统计离开本机的 TCP 发送字节。回环和进程自己监听的 Web UI（例如 DeepSeek Harness :3080 推给浏览器的会话）不计入外发告警。首次看到一条连接时只记基线，避免把监控启动前的历史流量当成突发上传。</div>
      <div class="table-wrap traffic-table"><table>
        <thead><tr><th>Agent</th><th>工作目录</th><th>近 15 秒外发</th><th>近 5 分钟 / 累计</th><th>主要对端</th><th>状态</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>`;
  };
  const renderAccounts = (state) => {
    const container = document.getElementById('account-list');
    const quotas = Array.isArray(state.quotas) ? state.quotas : [];
    const sessions = Array.isArray(state.sessions) ? state.sessions : [];
    const configuredAccounts = Array.isArray(state.accounts) ? state.accounts : [];
    const names = [...new Set([
      ...configuredAccounts.map((account) => account.name),
      ...quotas.map((quota) => quota.account || 'codex'),
      ...sessions.map((session) => session.account || 'codex')
    ])];
    document.getElementById('quota-source').textContent = names.length ? `${names.length} 个账号` : '暂无账号';
    document.getElementById('account-section-count').textContent = `${names.length} 个账号`;
    if (names.length === 0) {
      container.innerHTML = '<div class="empty-state">暂无配置账号</div>';
      return;
    }
    container.innerHTML = names.map((name) => {
      const account = configuredAccounts.find((item) => item.name === name) || {};
      const accountQuotas = quotas.filter((quota) => (quota.account || 'codex') === name);
      const accountSessions = sessions.filter((session) => (session.account || 'codex') === name);
      const accountCounts = account.counts || {};
      const profiles = (account.profiles || []).map((profile) => typeof profile === 'string' ? profile : profile.name).filter((profile) => profile).join(' · ');
      const accountId = account.account_id || name;
      const product = account.product === 'kimi' || (account.profiles || []).some((profile) => (typeof profile === 'string' ? profile : profile.name) === 'kimi') ? 'Kimi' : account.product === 'grok' || (account.profiles || []).some((profile) => (typeof profile === 'string' ? profile : profile.name) === 'grok') ? 'Grok' : account.product === 'dsh' || (account.profiles || []).some((profile) => (typeof profile === 'string' ? profile : profile.name) === 'dsh') ? 'DeepSeek Harness' : 'Codex';
      const plan = accountQuotas.length ? accountQuotas.map((quota) => quota.plan_type || '未知').join(' · ') : '未知';
      const activeCount = accountCounts.active ?? accountSessions.length;
      const accountSource = accountQuotas.map((quota) => quota.source).filter((source) => source).join(' · ');
      return `<article class="account-block">
        <div class="account-heading">
          <div class="account-identity"><div class="account-avatar" aria-hidden="true">${escapeHtml(accountInitials(accountId))}</div><div><div class="account-label">Account ID</div><h3 class="account-title">${escapeHtml(accountId)}</h3><div class="account-meta">Profile：${escapeHtml(profiles || name)}${accountSource ? ` · 来源：${escapeHtml(accountSource)}` : ''}</div></div></div>
          <div class="account-side"><span class="plan-badge">${escapeHtml(product)} · ${escapeHtml(plan)}</span><span class="account-activity">${escapeHtml(activeCount)} 个活动</span></div>
        </div>
        <div class="account-subtitle"><span>额度窗口</span><span class="muted">${accountQuotas.length} 个窗口</span></div>
        <div class="quota-grid">${renderQuotaCards(accountQuotas)}</div>
        <div class="account-subtitle"><span>活动会话</span><span class="muted">${activeCount} 个活动</span></div>
        ${renderSessionTable(accountSessions, name)}
      </article>`;
    }).join('');
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
      if (uploadAlerts) uploadAlerts.textContent = trafficTotals.alert_count ?? 0;
      document.getElementById('service-sync').textContent = `已同步 ${formatTime(state.updated_at)}`;
      document.getElementById('service-state').textContent = '监控服务在线';
      document.querySelectorAll('.status-dot').forEach((dot) => dot.classList.remove('error'));
      renderAccounts(state);
      renderTraffic(state);
      renderAlerts();
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
  document.getElementById('usage-load-button')?.addEventListener('click', refreshUsage);
  document.getElementById('insights-load-button')?.addEventListener('click', refreshInsights);
  document.querySelectorAll('[data-insights-days]').forEach((tab) => {
    tab.addEventListener('click', () => {
      insightsPeriodDays = tab.dataset.insightsDays || '';
      document.querySelectorAll('[data-insights-days]').forEach((item) => item.classList.toggle('selected', item === tab));
      latestInsightsState = null;
      refreshInsights();
    });
  });
  refresh();
  window.setInterval(refresh, 5000);
</script>
</body>
</html>
"""


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


class DashboardServer:
    """提供只读 Dashboard 状态和用量数据的后台 HTTP 服务。"""

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
        traffic_monitor: TrafficMonitor | None = None,
    ) -> None:
        if registries is not None and registry is not None:
            raise ValueError("registry 和 registries 只能传入一个")
        if registries is None:
            if registry is None:
                raise ValueError("至少需要一个 Dashboard 注册表")
            registries = {"codex": registry}
        if not registries:
            raise ValueError("至少需要一个 Dashboard 注册表")
        self.registries = dict(registries)
        self.account_metadata = dict(account_metadata or {})
        self.registry = next(iter(self.registries.values()))
        self.config = config or DashboardConfig()
        self.logger = logger or logging.getLogger(__name__)
        self.grok_homes = resolve_grok_homes(grok_homes)
        self.kimi_homes = resolve_kimi_homes(kimi_homes)
        self.dsh_homes = resolve_dsh_homes(dsh_homes)
        self.traffic_monitor = traffic_monitor
        self.usage_aggregator = usage_aggregator or UsageAggregator(
            grok_homes=self.grok_homes,
            kimi_homes=self.kimi_homes,
            dsh_homes=self.dsh_homes,
        )
        self._server: _DashboardHTTPServer | None = None
        self._thread: Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        """返回实际监听地址；端口为 0 时返回系统分配的端口。"""

        if self._server is None:
            return self.config.host, self.config.port
        raw_host, raw_port = self._server.server_address[:2]
        return str(raw_host), int(raw_port)

    def start(self) -> None:
        """绑定地址并启动 Dashboard 请求线程。"""

        if self._server is not None:
            return
        handler = _make_handler(
            self.registries,
            self.logger,
            self.account_metadata,
            self.usage_aggregator,
            self.grok_homes,
            self.kimi_homes,
            self.dsh_homes,
            budget_usd=self.config.budget_usd,
            traffic_monitor=self.traffic_monitor,
        )
        server = _DashboardHTTPServer(
            (self.config.host, self.config.port),
            handler,
        )
        self._server = server
        self._thread = Thread(
            target=server.serve_forever,
            name="codex-dashboard",
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


def build_dashboard_state(
    registry: MultiSessionRegistry,
    account_name: str = "codex",
    account_id: str | None = None,
    profile_name: str | None = None,
    codex_home: str | None = None,
) -> dict[str, Any]:
    """读取 SQLite 并构造不包含提示词的 Dashboard 数据。"""

    quota = registry.load_quota()
    sessions = registry.list_sessions(active_only=True)
    status_counts: dict[str, int] = {}
    for session in sessions:
        status_counts[session.status.value] = (
            status_counts.get(session.status.value, 0) + 1
        )
    status_counts["active"] = len(sessions)
    status_counts["process_backed"] = sum(
        session.is_process_backed for session in sessions
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
            _session_summary(
                session,
                account_name,
                account_id=account_id,
                profile_name=profile_name,
                codex_home=codex_home,
            )
            for session in sessions
        ],
    }


def build_multi_dashboard_state(
    registries: Mapping[str, MultiSessionRegistry],
    account_metadata: Mapping[str, Mapping[str, str | None]] | None = None,
    usage_aggregator: UsageAggregator | None = None,
    grok_homes: Sequence[Path] | None = None,
    kimi_homes: Sequence[Path] | None = None,
    dsh_homes: Sequence[Path] | None = None,
    budget_usd: float | None = None,
    traffic: TrafficSnapshot | None = None,
) -> dict[str, Any]:
    """合并多个账号状态，同时保留每个账号独立的额度快照。"""

    metadata_by_profile = account_metadata or {}
    account_states: list[dict[str, Any]] = []
    for profile_name, registry in registries.items():
        profile_metadata = metadata_by_profile.get(profile_name, {})
        account_id = profile_metadata.get("account_id")
        display_name = account_id or profile_name
        account_states.append(
            build_dashboard_state(
                registry,
                account_name=display_name,
                account_id=account_id,
                profile_name=profile_metadata.get(
                    "profile_name",
                    profile_name,
                ),
                codex_home=profile_metadata.get("codex_home"),
            )
        )
    quotas: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    accounts_by_key: dict[str, dict[str, Any]] = {}
    quota_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for state in account_states:
        account_name = str(state["account"])
        account_id = state.get("account_id")
        profile_name = str(state.get("profile_name") or account_name)
        codex_home = state.get("codex_home")
        account_key = str(account_id or f"profile:{profile_name}")
        quota = state.get("quota")
        if isinstance(quota, dict):
            quota_with_account = dict(quota)
            quota_with_account["account"] = account_name
            quota_with_account["account_id"] = account_id
            quota_with_account["profile_name"] = profile_name
            quota_with_account["codex_home"] = codex_home
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
                "profiles": [],
                "quota": None,
                "counts": {},
            },
        )
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

    # Kimi 配额经官方 /usages 接口读取（带缓存）；失败时账号卡片只展示身份。
    for kimi_home in kimi_homes or ():
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
                _session_summary(
                    session,
                    account_name=kimi_account.display_name,
                    account_id=kimi_account.account_id,
                    profile_name=kimi_account.profile_name,
                    codex_home=str(kimi_home),
                    product="kimi",
                )
            )

    for dsh_home in dsh_homes or ():
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
                _session_summary(
                    session,
                    account_name=dsh_account.display_name,
                    account_id=dsh_account.account_id,
                    profile_name=dsh_account.profile_name,
                    codex_home=str(dsh_home),
                    product="dsh",
                )
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
        account["counts"]["active"] = account["counts"].get("active", 0) + 1
        counts["active"] = counts.get("active", 0) + 1
        if session.get("process_backed"):
            account["counts"]["process_backed"] = (
                account["counts"].get("process_backed", 0) + 1
            )
            counts["process_backed"] = counts.get("process_backed", 0) + 1

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
            }
            for window in snapshot.windows
        ],
    }


def _session_summary(
    session: TrackedSession,
    account_name: str = "codex",
    account_id: str | None = None,
    profile_name: str | None = None,
    codex_home: str | None = None,
    product: str | None = None,
) -> dict[str, Any]:
    """把会话转换成不包含原始提示词的网页字段。"""

    session_account_id = session.account_id or account_id
    effective_account_name = session.account_id or account_name
    return {
        "account": effective_account_name,
        "account_id": session_account_id,
        "profile_name": profile_name or account_name,
        "codex_home": codex_home,
        "thread_id": session.thread_id,
        "session_id": session.session_id,
        "cwd": session.cwd,
        "source": session.source,
        "status": session.status.value,
        "confidence": session.confidence.value,
        "pids": list(session.pids),
        "process_backed": session.is_process_backed,
        "last_event_at": session.last_event_at,
        "last_event_type": session.last_event_type,
        "last_error": _display_session_error(session.last_error),
        "quota_reset_at": session.quota_reset_at,
        "product": product,
    }


def _display_session_error(value: str | None) -> str | None:
    """隐藏旧版恢复记录，避免历史错误文本重新出现在网页中。"""

    if not value:
        return None
    lowered = value.lower()
    legacy_terms = ("自动恢复", "不自动恢复", "续跑", "resume", "recovery")
    if any(term in lowered or term in value for term in legacy_terms):
        if "额度" in value or "quota" in lowered:
            return "额度限制事件"
        return "历史会话状态"
    return value


def _insights_window_days(query: str) -> int | None:
    """解析习惯分析的天数窗口参数；缺失或非法值按全部历史处理。"""

    raw = parse_qs(query).get("days", [None])[0]
    if raw is None:
        return None
    try:
        days = int(raw)
    except ValueError:
        return None
    return days if 0 < days <= 3660 else None


def _make_handler(
    registries: Mapping[str, MultiSessionRegistry],
    logger: logging.Logger,
    account_metadata: Mapping[str, Mapping[str, str | None]] | None = None,
    usage_aggregator: UsageAggregator | None = None,
    grok_homes: Sequence[Path] | None = None,
    kimi_homes: Sequence[Path] | None = None,
    dsh_homes: Sequence[Path] | None = None,
    budget_usd: float | None = None,
    traffic_monitor: TrafficMonitor | None = None,
) -> type[BaseHTTPRequestHandler]:
    """为多个注册表创建隔离的 HTTP 请求处理器类型。"""

    class DashboardRequestHandler(BaseHTTPRequestHandler):
        """处理 Dashboard 页面和只读状态请求。"""

        server_version = "CodexResetMonitorDashboard/0.9"

        def do_GET(self) -> None:
            """返回静态页面或当前监控状态。"""

            path = urlsplit(self.path).path
            if path == "/":
                self._send_bytes(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=_DASHBOARD_HTML.encode("utf-8"),
                )
                return
            if path == "/api/state":
                try:
                    state = build_multi_dashboard_state(
                        registries,
                        account_metadata=account_metadata,
                        grok_homes=grok_homes,
                        kimi_homes=kimi_homes,
                        dsh_homes=dsh_homes,
                        budget_usd=budget_usd,
                        traffic=(
                            traffic_monitor.latest()
                            if traffic_monitor is not None
                            else None
                        ),
                    )
                except RegistryError:
                    logger.exception("Dashboard 读取状态失败")
                    self._send_json(
                        status=503,
                        payload={"error": "monitor_state_unavailable"},
                    )
                    return
                self._send_json(status=200, payload=state)
                return
            if path == "/api/usage":
                try:
                    usage = (
                        usage_aggregator.snapshot(
                            registries,
                            account_metadata=account_metadata,
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
            if path == "/api/insights":
                window_days = _insights_window_days(urlsplit(self.path).query)
                try:
                    insights = (
                        usage_aggregator.insights(
                            registries,
                            account_metadata=account_metadata,
                            since_days=window_days,
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
                    body=_DASHBOARD_HTML.encode("utf-8"),
                    include_body=False,
                )
                return
            if path == "/api/state":
                try:
                    state = build_multi_dashboard_state(
                        registries,
                        account_metadata=account_metadata,
                        grok_homes=grok_homes,
                        kimi_homes=kimi_homes,
                        dsh_homes=dsh_homes,
                        budget_usd=budget_usd,
                        traffic=(
                            traffic_monitor.latest()
                            if traffic_monitor is not None
                            else None
                        ),
                    )
                except RegistryError:
                    logger.exception("Dashboard 读取状态失败")
                    self._send_json(
                        status=503,
                        payload={"error": "monitor_state_unavailable"},
                        include_body=False,
                    )
                    return
                self._send_json(
                    status=200,
                    payload=state,
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
            """Dashboard 只读，不接受任何状态修改请求。"""

            self._send_json(status=405, payload={"error": "read_only"})

        def log_message(self, format: str, *args: object) -> None:
            """把 HTTP 访问日志交给监控器日志，不污染标准输出。"""

            logger.debug("Dashboard HTTP " + format, *args)

        def _send_json(
            self,
            status: int,
            payload: object,
            include_body: bool = True,
        ) -> None:
            """发送 JSON 响应。"""

            body = json.dumps(
                payload,
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
        ) -> None:
            """发送带有本地安全响应头的字节响应。"""

            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
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
