"""traffic 子命令：扫描 code agent 进程的异常流量。"""

from __future__ import annotations

import argparse
import sys
import time

from ..traffic import TrafficMonitor, TrafficThresholds, format_bytes
from .common import (
    _dump_json,
)


def _show_traffic(args: argparse.Namespace) -> int:
    """采样两次 TCP 外发增量，用于异常流量监控。"""

    monitor = TrafficMonitor(
        thresholds=TrafficThresholds.from_mb(
            burst_warn_mb=args.upload_warn_mb,
            burst_danger_mb=args.upload_alert_mb,
            window_warn_mb=args.upload_window_warn_mb,
            window_danger_mb=args.upload_window_alert_mb,
        )
    )
    monitor.poll()
    time.sleep(args.sample_seconds)
    snapshot = monitor.poll()
    payload = snapshot.to_dict()
    if args.json:
        sys.stdout.write(f"{_dump_json(payload)}\n")
        return 1 if snapshot.alerts else 0
    process_only = snapshot.source == "process-only"
    if snapshot.source == "unavailable":
        reason = snapshot.reason or "无法读取内核 TCP 计数"
        sys.stdout.write(f"{reason}；异常流量监控不可用。\n")
        return 2
    if process_only:
        sys.stdout.write(
            f"{snapshot.reason or '当前平台没有内核 TCP 计数'}"
            "；下面只列出 agent 进程与远端连接。\n"
        )
    if not snapshot.processes:
        sys.stdout.write("没有发现正在运行的 code agent 进程。\n")
        return 0
    if not process_only:
        sys.stdout.write(
            f"采样间隔 {snapshot.interval_seconds:.1f} 秒 · "
            f"近 15 秒合计外发 {format_bytes(payload['totals']['burst_bytes'])}\n"
        )
    for process in snapshot.processes:
        remotes = ", ".join(
            item.remote
            for item in process.connections
            if not item.loopback and not item.service
        ) or "无外连"
        status = process.alert_level or "ok"
        if process_only:
            sys.stdout.write(
                f"{process.product} pid {process.pid} | {status} | "
                f"{process.cwd or '目录未知'} | {remotes}\n"
            )
            continue
        sys.stdout.write(
            f"{process.product} pid {process.pid} | {status} | "
            f"15s {format_bytes(process.burst_bytes)} | "
            f"5min {format_bytes(process.window_bytes)} | "
            f"{process.cwd or '目录未知'} | {remotes}\n"
        )
    for alert in snapshot.alerts:
        sys.stdout.write(f"{alert.level}: {alert.message}\n")
    return 1 if snapshot.alerts else 0
