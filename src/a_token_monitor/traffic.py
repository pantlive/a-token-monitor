"""异常流量监控：跟踪 code agent 进程的 TCP 外发字节，并识别异常大上传。

字节计数来自内核 INET_DIAG 的 ``tcp_info.tcpi_bytes_sent``，按 socket inode
归属到进程。回环连接和进程自己监听的服务端口（例如 DeepSeek Harness
Web UI ``:3080`` 把会话推给浏览器）单独统计，不作为外发告警依据。
只记录地址和大小，不读取连接载荷。
"""

from __future__ import annotations

import ctypes
import ipaddress
import logging
import os
import re
import socket
import struct
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .agents import identify_agent, product_label
from .process_backend import (
    ObservedProcess,
    netlink_reason,
    process_root,
    scan_process_connections,
    scan_processes,
)


_LOGGER = logging.getLogger(__name__)
_SOCKET_INODE_PATTERN = re.compile(r"^socket:\[(\d+)\]$")
_MIB = 1024 * 1024
_MAX_ALERTS = 20
_TCP_ESTABLISHED = 1
_TCP_LISTEN = 10
_ALERT_SORT_RANK = {"danger": 0, "warn": 1}
# 中文注释：即使没扫到 LISTEN inode，这些产品的 Web UI 端口也不算上传。
_UI_PORTS_BY_PRODUCT = {
    "dsh": frozenset({3080}),
}

_SOCK_DIAG_BY_FAMILY = 20
_INET_DIAG_INFO = 2
_NLMSG_DONE = 3
_NLMSG_ERROR = 2
_NLM_F_REQUEST = 0x01
_NLM_F_DUMP = 0x300
_INET_DIAG_NOCOOKIE = 0xFFFFFFFF
_TCPF_ALL = 0xFFFFFFFF
_NETLINK_SOCK_DIAG = 4


class _TcpInfo(ctypes.Structure):
    """与 Linux ``struct tcp_info`` 对齐的只读视图，用于取发送字节。"""

    _fields_ = [
        ("state", ctypes.c_uint8),
        ("ca_state", ctypes.c_uint8),
        ("retransmits", ctypes.c_uint8),
        ("probes", ctypes.c_uint8),
        ("backoff", ctypes.c_uint8),
        ("options", ctypes.c_uint8),
        ("wscale", ctypes.c_uint8),
        ("app_limited", ctypes.c_uint8),
        ("rto", ctypes.c_uint32),
        ("ato", ctypes.c_uint32),
        ("snd_mss", ctypes.c_uint32),
        ("rcv_mss", ctypes.c_uint32),
        ("unacked", ctypes.c_uint32),
        ("sacked", ctypes.c_uint32),
        ("lost", ctypes.c_uint32),
        ("retrans", ctypes.c_uint32),
        ("fackets", ctypes.c_uint32),
        ("last_data_sent", ctypes.c_uint32),
        ("last_ack_sent", ctypes.c_uint32),
        ("last_data_recv", ctypes.c_uint32),
        ("last_ack_recv", ctypes.c_uint32),
        ("pmtu", ctypes.c_uint32),
        ("rcv_ssthresh", ctypes.c_uint32),
        ("rtt", ctypes.c_uint32),
        ("rttvar", ctypes.c_uint32),
        ("snd_ssthresh", ctypes.c_uint32),
        ("snd_cwnd", ctypes.c_uint32),
        ("advmss", ctypes.c_uint32),
        ("reordering", ctypes.c_uint32),
        ("rcv_rtt", ctypes.c_uint32),
        ("rcv_space", ctypes.c_uint32),
        ("total_retrans", ctypes.c_uint32),
        ("pacing_rate", ctypes.c_uint64),
        ("max_pacing_rate", ctypes.c_uint64),
        ("bytes_acked", ctypes.c_uint64),
        ("bytes_received", ctypes.c_uint64),
        ("segs_out", ctypes.c_uint32),
        ("segs_in", ctypes.c_uint32),
        ("notsent_bytes", ctypes.c_uint32),
        ("min_rtt", ctypes.c_uint32),
        ("data_segs_in", ctypes.c_uint32),
        ("data_segs_out", ctypes.c_uint32),
        ("delivery_rate", ctypes.c_uint64),
        ("busy_time", ctypes.c_uint64),
        ("rwnd_limited", ctypes.c_uint64),
        ("sndbuf_limited", ctypes.c_uint64),
        ("delivered", ctypes.c_uint32),
        ("delivered_ce", ctypes.c_uint32),
        ("bytes_sent", ctypes.c_uint64),
        ("bytes_retrans", ctypes.c_uint64),
    ]


SocketStatsReader = Callable[[], Mapping[int, "SocketCounters"]]


@dataclass(frozen=True)
class TrafficThresholds:
    """外发流量告警阈值，字节和时间单位分别为 B 与秒。"""

    burst_window_seconds: float = 15.0
    burst_warn_bytes: int = 8 * _MIB
    burst_danger_bytes: int = 32 * _MIB
    window_seconds: float = 300.0
    window_warn_bytes: int = 64 * _MIB
    window_danger_bytes: int = 256 * _MIB
    alert_cooldown_seconds: float = 60.0

    def __post_init__(self) -> None:
        """拒绝无意义或相互矛盾的阈值。"""

        if self.burst_window_seconds <= 0:
            raise ValueError("burst_window_seconds 必须大于 0")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds 必须大于 0")
        if self.alert_cooldown_seconds < 0:
            raise ValueError("alert_cooldown_seconds 不能小于 0")
        for name in (
            "burst_warn_bytes",
            "burst_danger_bytes",
            "window_warn_bytes",
            "window_danger_bytes",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须大于 0")
        if self.burst_danger_bytes < self.burst_warn_bytes:
            raise ValueError("burst_danger_bytes 不能小于 burst_warn_bytes")
        if self.window_danger_bytes < self.window_warn_bytes:
            raise ValueError("window_danger_bytes 不能小于 window_warn_bytes")

    @classmethod
    def from_mb(
        cls,
        burst_warn_mb: float = 8.0,
        burst_danger_mb: float = 32.0,
        window_warn_mb: float = 64.0,
        window_danger_mb: float = 256.0,
        burst_window_seconds: float = 15.0,
        window_seconds: float = 300.0,
        alert_cooldown_seconds: float = 60.0,
    ) -> "TrafficThresholds":
        """从 MiB 配置构造阈值。"""

        return cls(
            burst_window_seconds=burst_window_seconds,
            burst_warn_bytes=_mb_to_bytes(burst_warn_mb),
            burst_danger_bytes=_mb_to_bytes(burst_danger_mb),
            window_seconds=window_seconds,
            window_warn_bytes=_mb_to_bytes(window_warn_mb),
            window_danger_bytes=_mb_to_bytes(window_danger_mb),
            alert_cooldown_seconds=alert_cooldown_seconds,
        )

    def to_dict(self) -> dict[str, float | int]:
        """返回 Dashboard / CLI 可展示的阈值。"""

        return {
            "burst_window_seconds": self.burst_window_seconds,
            "burst_warn_bytes": self.burst_warn_bytes,
            "burst_danger_bytes": self.burst_danger_bytes,
            "window_seconds": self.window_seconds,
            "window_warn_bytes": self.window_warn_bytes,
            "window_danger_bytes": self.window_danger_bytes,
        }


@dataclass(frozen=True)
class SocketCounters:
    """一条已建立 TCP 连接的发送/接收累计字节。"""

    inode: int
    local_ip: str
    local_port: int
    remote_ip: str
    remote_port: int
    bytes_sent: int
    bytes_received: int
    listening: bool = False

    @property
    def remote(self) -> str:
        """返回 ``ip:port`` 形式的对端地址。"""

        return _endpoint(self.remote_ip, self.remote_port)

    @property
    def loopback(self) -> bool:
        """判断对端是否为本机回环。"""

        return is_loopback(self.remote_ip)


@dataclass(frozen=True)
class ConnectionTraffic:
    """一次采样中归属到某个 agent 的连接增量。"""

    remote: str
    bytes_sent: int
    bytes_received: int
    upload_delta: int
    loopback: bool
    service: bool = False


@dataclass(frozen=True)
class ProcessTraffic:
    """一个 code agent（含同产品子进程）的外发流量。"""

    product: str
    pid: int
    start_token: str
    command: str
    cwd: str | None
    pids: tuple[int, ...]
    external_upload_delta: int
    loopback_upload_delta: int
    observed_external_bytes: int
    burst_bytes: int
    window_bytes: int
    upload_bps: float
    alert_level: str | None
    connections: tuple[ConnectionTraffic, ...]

    @property
    def process_key(self) -> str:
        """进程身份键，避免 PID 复用。"""

        return f"{self.product}:{self.pid}:{self.start_token}"

    def to_dict(self) -> dict[str, Any]:
        """转换为不含载荷的 JSON 字段。"""

        return {
            "product": self.product,
            "product_label": product_label(self.product),
            "pid": self.pid,
            "start_token": self.start_token,
            "command": self.command,
            "cwd": self.cwd,
            "pids": list(self.pids),
            "external_upload_delta": self.external_upload_delta,
            "loopback_upload_delta": self.loopback_upload_delta,
            "observed_external_bytes": self.observed_external_bytes,
            "burst_bytes": self.burst_bytes,
            "window_bytes": self.window_bytes,
            "upload_bps": self.upload_bps,
            "alert_level": self.alert_level,
            "connections": [
                {
                    "remote": item.remote,
                    "bytes_sent": item.bytes_sent,
                    "bytes_received": item.bytes_received,
                    "upload_delta": item.upload_delta,
                    "loopback": item.loopback,
                    "service": item.service,
                }
                for item in self.connections
            ],
        }


@dataclass(frozen=True)
class TrafficAlert:
    """一次外发超量告警。"""

    level: str
    product: str
    pid: int
    kind: str
    bytes: int
    window_seconds: float
    message: str
    observed_at: float
    remote: str | None = None
    process_key: str = ""
    command: str | None = None
    cwd: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为 Dashboard 告警字段。"""

        return {
            "level": self.level,
            "product": self.product,
            "product_label": product_label(self.product),
            "pid": self.pid,
            "kind": self.kind,
            "bytes": self.bytes,
            "window_seconds": self.window_seconds,
            "message": self.message,
            "observed_at": self.observed_at,
            "remote": self.remote,
            "process_key": self.process_key,
            "command": self.command,
            "cwd": self.cwd,
        }


@dataclass(frozen=True)
class TrafficSnapshot:
    """一轮流量扫描的安全摘要。"""

    observed_at: float
    source: str
    thresholds: TrafficThresholds
    processes: tuple[ProcessTraffic, ...]
    alerts: tuple[TrafficAlert, ...]
    interval_seconds: float = 0.0
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为 Dashboard / CLI JSON。"""

        external_delta = sum(
            item.external_upload_delta for item in self.processes
        )
        burst_bytes = sum(item.burst_bytes for item in self.processes)
        window_bytes = sum(item.window_bytes for item in self.processes)
        observed = sum(item.observed_external_bytes for item in self.processes)
        return {
            "observed_at": self.observed_at,
            "source": self.source,
            "reason": self.reason,
            "interval_seconds": self.interval_seconds,
            "thresholds": self.thresholds.to_dict(),
            "totals": {
                "process_count": len(self.processes),
                "connection_count": sum(
                    len(item.connections) for item in self.processes
                ),
                "external_upload_delta": external_delta,
                "burst_bytes": burst_bytes,
                "window_bytes": window_bytes,
                "observed_external_bytes": observed,
                "alert_count": len(self.alerts),
            },
            "processes": [item.to_dict() for item in self.processes],
            "alerts": [item.to_dict() for item in self.alerts],
        }


@dataclass
class _ConnState:
    bytes_sent: int
    bytes_received: int
    loopback: bool


@dataclass
class _ProcessState:
    product: str
    pid: int
    start_token: str
    connections: dict[int, _ConnState] = field(default_factory=dict)
    samples: deque[tuple[float, int]] = field(default_factory=deque)
    observed_external_bytes: int = 0
    last_alert_at: dict[tuple[str, str], float] = field(default_factory=dict)


class TrafficMonitor:
    """扫描 code agent 进程的 TCP 外发增量并产生告警。"""

    def __init__(
        self,
        thresholds: TrafficThresholds | None = None,
        proc_root: Path | None = None,
        ignore_pids: Sequence[int] | None = None,
        socket_reader: SocketStatsReader | None = None,
        alert_sink: Callable[[Sequence[TrafficAlert]], None] | None = None,
    ) -> None:
        self.thresholds = thresholds or TrafficThresholds()
        self.proc_root = proc_root
        self.ignore_roots = set(
            ignore_pids if ignore_pids is not None else (os.getpid(),)
        )
        self._socket_reader = socket_reader or read_tcp_socket_counters
        self._alert_sink = alert_sink
        self._lock = threading.Lock()
        self._states: dict[str, _ProcessState] = {}
        self._alerts: deque[TrafficAlert] = deque(maxlen=_MAX_ALERTS)
        self._snapshot: TrafficSnapshot | None = None
        self._last_poll_at: float | None = None

    def latest(self) -> TrafficSnapshot:
        """返回最近一次扫描结果；尚未扫描时返回空快照。"""

        with self._lock:
            if self._snapshot is not None:
                return self._snapshot
            return empty_traffic_snapshot(self.thresholds)

    def poll(self, now: float | None = None) -> TrafficSnapshot:
        """扫描一次进程和 TCP 计数，更新增量和告警。"""

        observed_at = time.time() if now is None else float(now)
        processes, source, created = self._collect(observed_at)
        unavailable_reason = netlink_reason()
        with self._lock:
            interval = (
                max(0.0, observed_at - self._last_poll_at)
                if self._last_poll_at is not None
                else 0.0
            )
            snapshot = TrafficSnapshot(
                observed_at=observed_at,
                source=source,
                thresholds=self.thresholds,
                processes=processes,
                alerts=tuple(self._alerts),
                interval_seconds=interval,
                reason=unavailable_reason if source != "inet-diag" else None,
            )
            self._snapshot = snapshot
            self._last_poll_at = observed_at
        if created:
            self._publish(created)
        return snapshot

    def _publish(self, alerts: Sequence[TrafficAlert]) -> None:
        """把本轮新产生的告警交给落盘回调；回调失败不影响监控主循环。"""

        sink = self._alert_sink
        if sink is None:
            return
        try:
            sink(alerts)
        except Exception:  # noqa: BLE001 - 落盘失败不能中断流量监控
            _LOGGER.exception("异常流量告警落盘回调失败")

    def _collect(
        self, now: float
    ) -> tuple[tuple[ProcessTraffic, ...], str, tuple[TrafficAlert, ...]]:
        """读取进程与 socket 计数，计算每个 agent 的外发增量。

        没有 netlink（macOS）时退化成 ``process-only``：只列 agent 进程与
        ``lsof`` 看到的远端连接，不做字节统计，也不产生流量告警。
        """

        proc_map = scan_processes(self.proc_root)
        ignored = _descendant_set(proc_map, self.ignore_roots)
        identified: dict[int, str] = {}
        for pid, info in proc_map.items():
            if pid in ignored:
                continue
            product = identify_agent(info.command, info.comm)
            if product is not None:
                identified[pid] = product
        owners = _attribute_owners(proc_map, identified)
        if netlink_reason() is not None:
            results, created = self._collect_process_only(
                proc_map,
                identified,
                owners,
                now,
            )
            return results, "process-only", created
        sockets_by_pid = _scan_socket_inodes(
            process_root(self.proc_root),
            tuple(pid for pid, owner in owners.items() if owner is not None),
        )
        try:
            counters = dict(self._socket_reader())
            source = "inet-diag"
        except (OSError, AttributeError):
            # 中文注释：注入的 reader 可能仍然假设 Linux，失败时按不可用处理。
            counters = {}
            source = "unavailable"

        grouped: dict[int, list[int]] = defaultdict(list)
        for pid, owner in owners.items():
            if owner is None:
                continue
            grouped[owner].append(pid)

        results: list[ProcessTraffic] = []
        created: list[TrafficAlert] = []
        seen_keys: set[str] = set()
        with self._lock:
            for owner_pid in sorted(grouped):
                info = proc_map[owner_pid]
                product = identified[owner_pid]
                key = f"{product}:{owner_pid}:{info.start_token}"
                seen_keys.add(key)
                state = self._states.get(key)
                if state is None:
                    state = _ProcessState(
                        product=product,
                        pid=owner_pid,
                        start_token=info.start_token,
                    )
                    self._states[key] = state
                member_pids = tuple(sorted(grouped[owner_pid]))
                traffic, new_alerts = self._update_process(
                    state=state,
                    info=info,
                    member_pids=member_pids,
                    sockets_by_pid=sockets_by_pid,
                    counters=counters,
                    now=now,
                )
                results.append(traffic)
                created.extend(new_alerts)
                self._alerts.extend(new_alerts)
            stale = [key for key in self._states if key not in seen_keys]
            for key in stale:
                state = self._states[key]
                _trim_samples(state.samples, now, self.thresholds.window_seconds)
                if not state.samples:
                    del self._states[key]
        results.sort(
            key=lambda item: (
                _ALERT_SORT_RANK.get(item.alert_level or "", 2),
                -item.burst_bytes,
                -item.external_upload_delta,
            )
        )
        return tuple(results), source, tuple(created)

    def _collect_process_only(
        self,
        proc_map: Mapping[int, ObservedProcess],
        identified: Mapping[int, str],
        owners: Mapping[int, int | None],
        now: float,
    ) -> tuple[tuple[ProcessTraffic, ...], tuple[TrafficAlert, ...]]:
        """macOS 等没有 netlink 的平台：只报进程和远端连接。"""

        grouped: dict[int, list[int]] = defaultdict(list)
        for pid, owner in owners.items():
            if owner is None:
                continue
            grouped[owner].append(pid)
        member_pids = tuple(
            sorted({pid for pids in grouped.values() for pid in pids})
        )
        observed_connections = scan_process_connections(
            member_pids,
            self.proc_root,
        )
        results: list[ProcessTraffic] = []
        seen_keys: set[str] = set()
        with self._lock:
            for owner_pid in sorted(grouped):
                info = proc_map[owner_pid]
                product = identified[owner_pid]
                key = f"{product}:{owner_pid}:{info.start_token}"
                seen_keys.add(key)
                state = self._states.get(key)
                if state is None:
                    state = _ProcessState(
                        product=product,
                        pid=owner_pid,
                        start_token=info.start_token,
                    )
                    self._states[key] = state
                connections: list[ConnectionTraffic] = []
                seen_remotes: set[str] = set()
                for pid in grouped[owner_pid]:
                    for item in observed_connections.get(pid, ()):
                        remote = item.remote
                        if remote is None or remote in seen_remotes:
                            continue
                        seen_remotes.add(remote)
                        connections.append(
                            ConnectionTraffic(
                                remote=remote,
                                bytes_sent=0,
                                bytes_received=0,
                                upload_delta=0,
                                loopback=item.loopback,
                                service=item.local_port
                                in _UI_PORTS_BY_PRODUCT.get(product, ()),
                            )
                        )
                connections.sort(
                    key=lambda entry: (
                        1 if entry.loopback or entry.service else 0,
                        entry.remote,
                    )
                )
                display_command = (
                    _basename(info.command[0]) if info.command else info.comm
                )
                results.append(
                    ProcessTraffic(
                        product=product,
                        pid=owner_pid,
                        start_token=info.start_token,
                        command=display_command,
                        cwd=str(info.cwd) if info.cwd is not None else None,
                        pids=tuple(sorted(grouped[owner_pid])),
                        external_upload_delta=0,
                        loopback_upload_delta=0,
                        observed_external_bytes=0,
                        burst_bytes=0,
                        window_bytes=0,
                        upload_bps=0.0,
                        alert_level=None,
                        connections=tuple(connections),
                    )
                )
            for key in [key for key in self._states if key not in seen_keys]:
                del self._states[key]
        results.sort(key=lambda item: (item.product, item.pid))
        return tuple(results), ()

    def _update_process(
        self,
        state: _ProcessState,
        info: ObservedProcess,
        member_pids: tuple[int, ...],
        sockets_by_pid: Mapping[int, tuple[int, ...]],
        counters: Mapping[int, SocketCounters],
        now: float,
    ) -> tuple[ProcessTraffic, tuple[TrafficAlert, ...]]:
        """把本轮 socket 计数转成增量和告警。"""

        live: dict[int, SocketCounters] = {}
        for pid in member_pids:
            for inode in sockets_by_pid.get(pid, ()):
                counter = counters.get(inode)
                if counter is None:
                    continue
                live[inode] = counter
        listen_ports = {
            counter.local_port for counter in live.values() if counter.listening
        }
        listen_ports |= set(_UI_PORTS_BY_PRODUCT.get(state.product, ()))

        connections: list[ConnectionTraffic] = []
        external_delta = 0
        loopback_delta = 0
        for inode, counter in live.items():
            if counter.listening:
                continue
            previous = state.connections.get(inode)
            if previous is None:
                delta = 0
            elif counter.bytes_sent < previous.bytes_sent:
                delta = 0
            else:
                delta = counter.bytes_sent - previous.bytes_sent
            service = counter.local_port in listen_ports
            if counter.loopback or service:
                loopback_delta += delta
            else:
                external_delta += delta
            connections.append(
                ConnectionTraffic(
                    remote=counter.remote,
                    bytes_sent=counter.bytes_sent,
                    bytes_received=counter.bytes_received,
                    upload_delta=delta,
                    loopback=counter.loopback,
                    service=service,
                )
            )
            state.connections[inode] = _ConnState(
                bytes_sent=counter.bytes_sent,
                bytes_received=counter.bytes_received,
                loopback=counter.loopback,
            )
        for inode in tuple(state.connections):
            if inode not in live:
                del state.connections[inode]

        state.observed_external_bytes += external_delta
        state.samples.append((now, external_delta))
        _trim_samples(state.samples, now, self.thresholds.window_seconds)
        burst_bytes = _sum_samples(
            state.samples, now, self.thresholds.burst_window_seconds
        )
        window_bytes = _sum_samples(
            state.samples, now, self.thresholds.window_seconds
        )
        span = min(
            self.thresholds.burst_window_seconds,
            max(
                now - state.samples[0][0] if state.samples else 0.0,
                1e-6,
            ),
        )
        upload_bps = burst_bytes / span if span > 0 else 0.0
        alert_level, new_alerts = self._evaluate_alerts(
            state=state,
            info=info,
            burst_bytes=burst_bytes,
            window_bytes=window_bytes,
            connections=connections,
            now=now,
        )
        connections.sort(
            key=lambda item: (
                1 if item.loopback or item.service else 0,
                -item.upload_delta,
            )
        )
        display_command = _basename(info.command[0]) if info.command else info.comm
        return (
            ProcessTraffic(
                product=state.product,
                pid=state.pid,
                start_token=state.start_token,
                command=display_command,
                cwd=str(info.cwd) if info.cwd is not None else None,
                pids=member_pids,
                external_upload_delta=external_delta,
                loopback_upload_delta=loopback_delta,
                observed_external_bytes=state.observed_external_bytes,
                burst_bytes=burst_bytes,
                window_bytes=window_bytes,
                upload_bps=upload_bps,
                alert_level=alert_level,
                connections=tuple(connections[:12]),
            ),
            new_alerts,
        )

    def _evaluate_alerts(
        self,
        state: _ProcessState,
        info: ObservedProcess,
        burst_bytes: int,
        window_bytes: int,
        connections: Sequence[ConnectionTraffic],
        now: float,
    ) -> tuple[str | None, tuple[TrafficAlert, ...]]:
        """按突发窗口和 5 分钟窗口判定 warn/danger。"""

        burst_level = _level_for(
            burst_bytes,
            self.thresholds.burst_warn_bytes,
            self.thresholds.burst_danger_bytes,
        )
        window_level = _level_for(
            window_bytes,
            self.thresholds.window_warn_bytes,
            self.thresholds.window_danger_bytes,
        )
        alert_level = _worse_level(burst_level, window_level)
        remote = _top_external_remote(connections)
        label = product_label(state.product)
        cwd_note = f"，目录 {info.cwd}" if info.cwd is not None else ""
        remote_note = f"，主要对端 {remote}" if remote else ""
        command = _basename(info.command[0]) if info.command else info.comm
        created: list[TrafficAlert] = []
        for kind, level, amount, window in (
            (
                "burst",
                burst_level,
                burst_bytes,
                self.thresholds.burst_window_seconds,
            ),
            (
                "window",
                window_level,
                window_bytes,
                self.thresholds.window_seconds,
            ),
        ):
            if level is None:
                continue
            last_at = state.last_alert_at.get((kind, level), 0.0)
            if now - last_at < self.thresholds.alert_cooldown_seconds:
                continue
            state.last_alert_at[(kind, level)] = now
            if kind == "burst":
                message = (
                    f"{label} (pid {state.pid}) 在 {window:g} 秒内向外发送 "
                    f"{format_bytes(amount)}{cwd_note}{remote_note}"
                )
            else:
                message = (
                    f"{label} (pid {state.pid}) 在 {window:g} 秒内累计外发 "
                    f"{format_bytes(amount)}{cwd_note}{remote_note}"
                )
            created.append(
                TrafficAlert(
                    level=level,
                    product=state.product,
                    pid=state.pid,
                    kind=kind,
                    bytes=amount,
                    window_seconds=window,
                    message=message,
                    observed_at=now,
                    remote=remote,
                    process_key=f"{state.product}:{state.pid}:{state.start_token}",
                    command=command or None,
                    cwd=str(info.cwd) if info.cwd is not None else None,
                )
            )
        return alert_level, tuple(created)


def empty_traffic_snapshot(
    thresholds: TrafficThresholds | None = None,
    now: float | None = None,
) -> TrafficSnapshot:
    """返回尚未扫描时仍可给 Dashboard 使用的空结构。"""

    return TrafficSnapshot(
        observed_at=time.time() if now is None else float(now),
        source="none",
        thresholds=thresholds or TrafficThresholds(),
        processes=(),
        alerts=(),
    )


def is_loopback(ip: str) -> bool:
    """判断地址是否为本机回环（含 IPv4-mapped IPv6）。"""

    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return ip.startswith("127.") or ip in {":1", "::1"}
    return parsed.is_loopback


def format_bytes(value: int) -> str:
    """把字节数格式化为 KiB/MiB/GiB。"""

    amount = float(max(0, value))
    if amount < 1024:
        return f"{int(amount)} B"
    for unit, size in (("KiB", 1024.0), ("MiB", float(_MIB)), ("GiB", float(_MIB) * 1024)):
        scaled = amount / size
        if scaled < 1024 or unit == "GiB":
            if scaled >= 100:
                return f"{scaled:.0f} {unit}"
            if scaled >= 10:
                return f"{scaled:.1f} {unit}"
            return f"{scaled:.2f} {unit}"
    return f"{int(amount)} B"


def read_tcp_socket_counters() -> dict[int, SocketCounters]:
    """通过 NETLINK SOCK_DIAG 读取已建立 TCP 连接的发送字节。

    macOS 等平台没有 ``AF_NETLINK``：这里直接返回空表，让上层退化成
    ``process-only``，而不是抛 ``AttributeError``。
    """

    if not hasattr(socket, "AF_NETLINK"):
        return {}
    counters: dict[int, SocketCounters] = {}
    for family in (socket.AF_INET, socket.AF_INET6):
        for item in _dump_inet_diag(family):
            counters[item.inode] = item
    return counters


def _dump_inet_diag(family: int) -> tuple[SocketCounters, ...]:
    """请求一个地址族的 TCP 诊断信息。"""

    if not hasattr(socket, "AF_NETLINK"):
        return ()
    sockid = struct.pack(
        "@HH16s16sI2I",
        0,
        0,
        b"\0" * 16,
        b"\0" * 16,
        0,
        _INET_DIAG_NOCOOKIE,
        _INET_DIAG_NOCOOKIE,
    )
    ext = 1 << (_INET_DIAG_INFO - 1)
    request = (
        struct.pack("@BBBBI", family, socket.IPPROTO_TCP, ext, 0, _TCPF_ALL)
        + sockid
    )
    header = struct.pack(
        "@IHHII",
        16 + len(request),
        _SOCK_DIAG_BY_FAMILY,
        _NLM_F_REQUEST | _NLM_F_DUMP,
        1,
        0,
    )
    try:
        handle = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, _NETLINK_SOCK_DIAG)
    except OSError:
        return ()
    try:
        handle.bind((0, 0))
        handle.settimeout(1.0)
        handle.send(header + request)
        data = b""
        while True:
            try:
                chunk = handle.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
            if _netlink_last_type(chunk) in {_NLMSG_DONE, _NLMSG_ERROR}:
                break
    except OSError:
        return ()
    finally:
        handle.close()
    return _parse_inet_diag(data)


def _netlink_last_type(chunk: bytes) -> int | None:
    """返回一段 netlink 缓冲中最后一条消息的类型。"""

    offset = 0
    last: int | None = None
    while offset + 16 <= len(chunk):
        length, msg_type = struct.unpack_from("@IH", chunk, offset)[:2]
        if length < 16:
            break
        last = msg_type
        offset += (length + 3) & ~3
    return last


def _parse_inet_diag(data: bytes) -> tuple[SocketCounters, ...]:
    """解析 INET_DIAG 消息中的 inode 与 tcp_info 字节计数。"""

    items: list[SocketCounters] = []
    offset = 0
    info_size = ctypes.sizeof(_TcpInfo)
    while offset + 16 <= len(data):
        length, msg_type = struct.unpack_from("@IHHII", data, offset)[:2]
        if length < 16:
            break
        if msg_type == _NLMSG_DONE:
            break
        if msg_type == _NLMSG_ERROR:
            break
        payload = data[offset + 16 : offset + length]
        offset += (length + 3) & ~3
        if len(payload) < 72:
            continue
        family = payload[0]
        state = payload[1]
        if state not in {_TCP_ESTABLISHED, _TCP_LISTEN}:
            continue
        local_port, remote_port = struct.unpack_from(">HH", payload, 4)
        inode = struct.unpack_from("@I", payload, 68)[0]
        if inode <= 0:
            continue
        try:
            if family == socket.AF_INET:
                local_ip = socket.inet_ntop(socket.AF_INET, payload[8:12])
                remote_ip = socket.inet_ntop(socket.AF_INET, payload[24:28])
            elif family == socket.AF_INET6:
                local_ip = socket.inet_ntop(socket.AF_INET6, payload[8:24])
                remote_ip = socket.inet_ntop(socket.AF_INET6, payload[24:40])
            else:
                continue
        except OSError:
            continue
        bytes_sent = 0
        bytes_received = 0
        attr_offset = 72
        while attr_offset + 4 <= len(payload):
            attr_len, attr_type = struct.unpack_from("@HH", payload, attr_offset)
            if attr_len < 4:
                break
            value = payload[attr_offset + 4 : attr_offset + attr_len]
            if attr_type == _INET_DIAG_INFO and value:
                padded = value[:info_size] + b"\0" * max(0, info_size - len(value))
                info = _TcpInfo.from_buffer_copy(padded[:info_size])
                bytes_sent = int(info.bytes_sent or info.bytes_acked)
                bytes_received = int(info.bytes_received)
            attr_offset += (attr_len + 3) & ~3
        items.append(
            SocketCounters(
                inode=inode,
                local_ip=local_ip,
                local_port=int(local_port),
                remote_ip=remote_ip,
                remote_port=int(remote_port),
                bytes_sent=bytes_sent,
                bytes_received=bytes_received,
                listening=state == _TCP_LISTEN,
            )
        )
    return tuple(items)


def _scan_socket_inodes(
    proc_root: Path,
    pids: Sequence[int],
) -> dict[int, tuple[int, ...]]:
    """读取指定进程打开的 TCP/Unix socket inode；Unix inode 对不上 TCP 表会被忽略。"""

    sockets: dict[int, tuple[int, ...]] = {}
    for pid in pids:
        fd_directory = proc_root / str(pid) / "fd"
        inodes: list[int] = []
        try:
            descriptors = tuple(fd_directory.iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            inode = _socket_inode(descriptor)
            if inode is not None:
                inodes.append(inode)
        if inodes:
            sockets[pid] = tuple(inodes)
    return sockets


def _socket_inode(path: Path) -> int | None:
    """从 fd 符号链接解析 socket inode。"""

    try:
        target = path.readlink()
    except OSError:
        return None
    match = _SOCKET_INODE_PATTERN.match(str(target))
    if match is None:
        return None
    return int(match.group(1))


def _descendant_set(
    processes: Mapping[int, ObservedProcess],
    roots: Sequence[int] | set[int],
) -> set[int]:
    """返回根进程及其全部子孙 PID。"""

    children: dict[int, list[int]] = defaultdict(list)
    for pid, info in processes.items():
        children[info.ppid].append(pid)
    ignored: set[int] = set()
    stack = [pid for pid in roots if pid in processes or pid]
    while stack:
        pid = stack.pop()
        if pid in ignored:
            continue
        ignored.add(pid)
        stack.extend(children.get(pid, ()))
    return ignored


def _attribute_owners(
    processes: Mapping[int, ObservedProcess],
    identified: Mapping[int, str],
) -> dict[int, int | None]:
    """把进程归到最近的同产品 agent 祖先。"""

    owners: dict[int, int | None] = {}
    for pid in processes:
        owners[pid] = _owner_pid(pid, processes, identified)
    return owners


def _owner_pid(
    pid: int,
    processes: Mapping[int, ObservedProcess],
    identified: Mapping[int, str],
) -> int | None:
    """沿 ppid 向上找到应归账的 agent 根进程。"""

    chain: list[int] = []
    current = pid
    seen: set[int] = set()
    while current in processes and current not in seen:
        chain.append(current)
        seen.add(current)
        current = processes[current].ppid
    agents = [item for item in chain if item in identified]
    if not agents:
        return None
    nearest = agents[0]
    top = agents[-1]
    if identified[nearest] != identified[top]:
        return nearest
    return top


def _trim_samples(
    samples: deque[tuple[float, int]],
    now: float,
    window_seconds: float,
) -> None:
    """丢掉超出最长统计窗口的增量样本。"""

    cutoff = now - window_seconds
    while samples and samples[0][0] < cutoff:
        samples.popleft()


def _sum_samples(
    samples: Sequence[tuple[float, int]],
    now: float,
    window_seconds: float,
) -> int:
    """对窗口内增量求和。"""

    cutoff = now - window_seconds
    return sum(delta for at, delta in samples if at >= cutoff)


def _level_for(amount: int, warn_bytes: int, danger_bytes: int) -> str | None:
    """按字节数返回 danger / warn / None。"""

    if amount >= danger_bytes:
        return "danger"
    if amount >= warn_bytes:
        return "warn"
    return None


def _worse_level(first: str | None, second: str | None) -> str | None:
    """合并两个告警级别，danger 优先。"""

    if first == "danger" or second == "danger":
        return "danger"
    if first == "warn" or second == "warn":
        return "warn"
    return None


def _top_external_remote(connections: Sequence[ConnectionTraffic]) -> str | None:
    """返回本轮外发增量最大的非回环对端。"""

    external = [
        item for item in connections if not item.loopback and not item.service
    ]
    if not external:
        return None
    top = max(external, key=lambda item: item.upload_delta)
    if top.upload_delta <= 0:
        return top.remote
    return top.remote


def _endpoint(ip: str, port: int) -> str:
    """格式化 IP 与端口；IPv6 加方括号。"""

    if ":" in ip and not ip.startswith("["):
        return f"[{ip}]:{port}"
    return f"{ip}:{port}"


def _basename(value: str) -> str:
    """命令展示名只保留文件名。"""

    return Path(value).name or value


def _mb_to_bytes(value: float) -> int:
    """把 MiB 转为整数字节。"""

    return int(float(value) * _MIB)
