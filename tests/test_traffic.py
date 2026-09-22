"""code agent 异常流量监控测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from token_monitor.traffic import (
    SocketCounters,
    TrafficMonitor,
    TrafficThresholds,
    format_bytes,
    is_loopback,
)


_MIB = 1024 * 1024


class TrafficMonitorTests(unittest.TestCase):
    """验证外发增量告警，且回环和大基线不会误报。"""

    def test_first_sample_is_baseline_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write_process(
                root,
                pid=10,
                comm="grok",
                command=("grok",),
                sockets=(1001,),
            )
            counters = {
                1001: _external_socket(1001, bytes_sent=80 * _MIB),
            }
            monitor = TrafficMonitor(
                thresholds=_small_thresholds(),
                proc_root=root,
                ignore_pids=(),
                socket_reader=lambda: counters,
            )

            first = monitor.poll(now=1_000.0)

        self.assertEqual(len(first.processes), 1)
        process = first.processes[0]
        self.assertEqual(process.product, "grok")
        self.assertEqual(process.external_upload_delta, 0)
        self.assertEqual(process.observed_external_bytes, 0)
        self.assertEqual(first.alerts, ())

    def test_large_external_burst_raises_danger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write_process(
                root,
                pid=11,
                comm="dsh",
                command=("node", "/usr/bin/dsh", "web"),
                sockets=(2002,),
                cwd=root / "project",
            )
            (root / "project").mkdir()
            counters = {
                2002: _external_socket(2002, bytes_sent=1 * _MIB),
            }
            monitor = TrafficMonitor(
                thresholds=_small_thresholds(),
                proc_root=root,
                ignore_pids=(),
                socket_reader=lambda: counters,
            )
            monitor.poll(now=1_000.0)
            counters[2002] = _external_socket(2002, bytes_sent=41 * _MIB)
            second = monitor.poll(now=1_002.0)

        process = second.processes[0]
        self.assertEqual(process.product, "dsh")
        self.assertEqual(process.external_upload_delta, 40 * _MIB)
        self.assertEqual(process.alert_level, "danger")
        self.assertEqual(len(second.alerts), 1)
        self.assertEqual(second.alerts[0].level, "danger")
        self.assertIn("DeepSeek Harness", second.alerts[0].message)
        self.assertNotIn("SECRET", second.alerts[0].message)

    def test_loopback_bytes_do_not_trigger_upload_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write_process(
                root,
                pid=12,
                comm="dsh",
                command=("dsh", "web"),
                sockets=(3003,),
            )
            counters = {
                3003: _loopback_socket(3003, bytes_sent=_MIB),
            }
            monitor = TrafficMonitor(
                thresholds=_small_thresholds(),
                proc_root=root,
                ignore_pids=(),
                socket_reader=lambda: counters,
            )
            monitor.poll(now=1_000.0)
            counters[3003] = _loopback_socket(3003, bytes_sent=50 * _MIB)
            second = monitor.poll(now=1_002.0)

        process = second.processes[0]
        self.assertEqual(process.loopback_upload_delta, 49 * _MIB)
        self.assertEqual(process.external_upload_delta, 0)
        self.assertIsNone(process.alert_level)
        self.assertEqual(second.alerts, ())

    def test_child_sockets_count_toward_parent_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write_process(
                root,
                pid=20,
                comm="codex",
                command=("codex",),
            )
            _write_process(
                root,
                pid=21,
                comm="zsh",
                command=("zsh", "-c", "true"),
                ppid=20,
                sockets=(4004,),
            )
            counters = {
                4004: _external_socket(4004, bytes_sent=100),
            }
            monitor = TrafficMonitor(
                thresholds=_small_thresholds(),
                proc_root=root,
                ignore_pids=(),
                socket_reader=lambda: counters,
            )
            monitor.poll(now=1_000.0)
            counters[4004] = _external_socket(4004, bytes_sent=100 + 9 * _MIB)
            second = monitor.poll(now=1_002.0)

        self.assertEqual(len(second.processes), 1)
        process = second.processes[0]
        self.assertEqual(process.pid, 20)
        self.assertEqual(process.external_upload_delta, 9 * _MIB)
        self.assertEqual(process.alert_level, "warn")
        self.assertIn(21, process.pids)

    def test_monitor_process_tree_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write_process(
                root,
                pid=30,
                comm="python",
                command=("python", "-m", "token_monitor", "daemon"),
            )
            _write_process(
                root,
                pid=31,
                comm="codex",
                command=("codex", "app-server", "--listen", "stdio://"),
                ppid=30,
                sockets=(5005,),
            )
            counters = {
                5005: _external_socket(5005, bytes_sent=_MIB),
            }
            monitor = TrafficMonitor(
                thresholds=_small_thresholds(),
                proc_root=root,
                ignore_pids=(30,),
                socket_reader=lambda: counters,
            )
            snapshot = monitor.poll(now=1_000.0)

        self.assertEqual(snapshot.processes, ())

    def test_warn_threshold_is_below_danger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write_process(
                root,
                pid=40,
                comm="kimi",
                command=("kimi",),
                sockets=(6006,),
            )
            counters = {6006: _external_socket(6006, bytes_sent=10)}
            monitor = TrafficMonitor(
                thresholds=_small_thresholds(),
                proc_root=root,
                ignore_pids=(),
                socket_reader=lambda: counters,
            )
            monitor.poll(now=1_000.0)
            counters[6006] = _external_socket(6006, bytes_sent=10 + 9 * _MIB)
            snapshot = monitor.poll(now=1_002.0)

        self.assertEqual(snapshot.processes[0].alert_level, "warn")
        self.assertEqual(snapshot.alerts[0].kind, "burst")

    def test_anomalous_processes_are_listed_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write_process(
                root,
                pid=50,
                comm="grok",
                command=("grok",),
                sockets=(7007,),
            )
            _write_process(
                root,
                pid=51,
                comm="kimi",
                command=("kimi",),
                sockets=(7008,),
            )
            counters = {
                7007: _external_socket(7007, bytes_sent=10),
                7008: _external_socket(7008, bytes_sent=10),
            }
            monitor = TrafficMonitor(
                thresholds=_small_thresholds(),
                proc_root=root,
                ignore_pids=(),
                socket_reader=lambda: counters,
            )
            monitor.poll(now=1_000.0)
            counters[7007] = _external_socket(7007, bytes_sent=10 + _MIB)
            counters[7008] = _external_socket(7008, bytes_sent=10 + 9 * _MIB)
            snapshot = monitor.poll(now=1_002.0)

        self.assertEqual(
            [item.product for item in snapshot.processes],
            ["kimi", "grok"],
        )
        self.assertEqual(snapshot.processes[0].alert_level, "warn")
        self.assertIsNone(snapshot.processes[1].alert_level)

    def test_dsh_web_ui_listen_port_is_not_treated_as_upload(self) -> None:
        """DeepSeek Harness 长期把会话推给浏览器，不应算异常上传。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write_process(
                root,
                pid=60,
                comm="MainThread",
                command=("node", "/usr/bin/dsh", "web"),
                sockets=(8001, 8002, 8003),
            )
            counters = {
                8001: _listen_socket(8001, port=3080),
                8002: _service_socket(8002, bytes_sent=_MIB, local_port=3080),
                8003: _external_socket(8003, bytes_sent=100),
            }
            monitor = TrafficMonitor(
                thresholds=_small_thresholds(),
                proc_root=root,
                ignore_pids=(),
                socket_reader=lambda: counters,
            )
            monitor.poll(now=1_000.0)
            counters[8002] = _service_socket(
                8002, bytes_sent=_MIB + 40 * _MIB, local_port=3080
            )
            counters[8003] = _external_socket(8003, bytes_sent=100 + 512)
            snapshot = monitor.poll(now=1_002.0)

        process = snapshot.processes[0]
        self.assertEqual(process.product, "dsh")
        self.assertEqual(process.external_upload_delta, 512)
        self.assertEqual(process.loopback_upload_delta, 40 * _MIB)
        self.assertIsNone(process.alert_level)
        self.assertEqual(snapshot.alerts, ())
        service = [item for item in process.connections if item.service]
        external = [
            item
            for item in process.connections
            if not item.loopback and not item.service
        ]
        self.assertEqual(len(service), 1)
        self.assertEqual(external[0].remote, "203.0.113.10:443")

    def test_format_bytes_and_loopback_helpers(self) -> None:
        self.assertEqual(format_bytes(512), "512 B")
        self.assertTrue(format_bytes(8 * _MIB).endswith("MiB"))
        self.assertTrue(is_loopback("127.0.0.1"))
        self.assertTrue(is_loopback("::1"))
        self.assertFalse(is_loopback("1.1.1.1"))


def _small_thresholds() -> TrafficThresholds:
    """测试用较小阈值，避免构造几十兆的等待。"""

    return TrafficThresholds(
        burst_window_seconds=15.0,
        burst_warn_bytes=8 * _MIB,
        burst_danger_bytes=32 * _MIB,
        window_seconds=300.0,
        window_warn_bytes=64 * _MIB,
        window_danger_bytes=256 * _MIB,
        alert_cooldown_seconds=0.0,
    )


def _write_process(
    proc_root: Path,
    pid: int,
    comm: str,
    command: tuple[str, ...],
    ppid: int = 1,
    start: str = "1000",
    sockets: tuple[int, ...] = (),
    cwd: Path | None = None,
) -> None:
    """在临时 /proc 树中写入一个进程目录。"""

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
    if cwd is not None:
        (directory / "cwd").symlink_to(cwd)
    for index, inode in enumerate(sockets, start=3):
        (directory / "fd" / str(index)).symlink_to(f"socket:[{inode}]")


def _external_socket(inode: int, bytes_sent: int) -> SocketCounters:
    return SocketCounters(
        inode=inode,
        local_ip="10.0.0.2",
        local_port=40000,
        remote_ip="203.0.113.10",
        remote_port=443,
        bytes_sent=bytes_sent,
        bytes_received=12,
    )


def _listen_socket(inode: int, port: int = 3080) -> SocketCounters:
    return SocketCounters(
        inode=inode,
        local_ip="127.0.0.1",
        local_port=port,
        remote_ip="0.0.0.0",
        remote_port=0,
        bytes_sent=0,
        bytes_received=0,
        listening=True,
    )


def _service_socket(
    inode: int,
    bytes_sent: int,
    local_port: int = 3080,
) -> SocketCounters:
    return SocketCounters(
        inode=inode,
        local_ip="172.17.182.189",
        local_port=local_port,
        remote_ip="172.17.176.1",
        remote_port=54321,
        bytes_sent=bytes_sent,
        bytes_received=12,
    )


def _loopback_socket(inode: int, bytes_sent: int) -> SocketCounters:
    return SocketCounters(
        inode=inode,
        local_ip="127.0.0.1",
        local_port=3080,
        remote_ip="127.0.0.1",
        remote_port=44956,
        bytes_sent=bytes_sent,
        bytes_received=12,
    )


if __name__ == "__main__":
    unittest.main()
