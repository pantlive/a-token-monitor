"""异常流量告警落盘、合并和历史查询测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from token_monitor.alerts import (
    AlertQuery,
    AlertStoreError,
    TrafficAlertStore,
)
from token_monitor.traffic import TrafficAlert


_MIB = 1024 * 1024


def _alert(
    *,
    level: str = "danger",
    kind: str = "burst",
    product: str = "codex",
    pid: int = 100,
    process_key: str = "codex:100:1000",
    bytes_sent: int = 40 * _MIB,
    observed_at: float = 1_000.0,
    remote: str | None = "203.0.113.10:443",
    cwd: str | None = "/home/dev/project",
    command: str | None = "codex",
    message: str | None = None,
) -> TrafficAlert:
    """构造一条测试告警。"""

    return TrafficAlert(
        level=level,
        product=product,
        pid=pid,
        kind=kind,
        bytes=bytes_sent,
        window_seconds=15.0 if kind == "burst" else 300.0,
        message=message
        or f"{product} pid {pid} 在窗口内向外发送 {bytes_sent} 字节",
        observed_at=observed_at,
        remote=remote,
        process_key=process_key,
        command=command,
        cwd=cwd,
    )


class TrafficAlertStoreTests(unittest.TestCase):
    """验证告警落盘、重复合并、筛选和清理。"""

    def test_repeated_alerts_merge_into_one_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = TrafficAlertStore(Path(temporary_directory))
            store.record([_alert(observed_at=1_000.0)], now=1_000.0)
            store.record(
                [_alert(bytes_sent=48 * _MIB, observed_at=1_100.0)],
                now=1_100.0,
            )

            alerts = store.query()
            total = store.stats()["total"]

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].count, 2)
        self.assertEqual(alerts[0].first_seen_at, 1_000.0)
        self.assertEqual(alerts[0].last_seen_at, 1_100.0)
        self.assertEqual(alerts[0].peak_bytes, 48 * _MIB)
        self.assertEqual(total, 1)

    def test_alerts_beyond_merge_window_stay_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = TrafficAlertStore(
                Path(temporary_directory),
                merge_window_seconds=60.0,
            )
            store.record([_alert(observed_at=1_000.0)], now=1_000.0)
            store.record([_alert(observed_at=1_500.0)], now=1_500.0)

            alerts = store.query()

        self.assertEqual(len(alerts), 2)
        self.assertEqual([item.count for item in alerts], [1, 1])

    def test_level_and_rule_produce_separate_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = TrafficAlertStore(Path(temporary_directory))
            store.record(
                [
                    _alert(level="warn", kind="burst"),
                    _alert(level="danger", kind="burst"),
                    _alert(level="warn", kind="window"),
                ],
                now=1_000.0,
            )

            stats = store.stats()

        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["warn"], 2)
        self.assertEqual(stats["danger"], 1)

    def test_query_filters_by_level_kind_product_and_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = TrafficAlertStore(Path(temporary_directory))
            store.record(
                [
                    _alert(level="warn", process_key="codex:1:10", pid=1),
                    _alert(
                        level="danger",
                        product="dsh",
                        process_key="dsh:2:20",
                        pid=2,
                        observed_at=2_000.0,
                    ),
                ],
                now=1_000.0,
            )

            danger = store.query(AlertQuery(levels=("danger",)))
            dsh = store.query(AlertQuery(products=("dsh",)))
            recent = store.query(AlertQuery(since=1_500.0))
            burst_and_codex = store.query(
                AlertQuery(kinds=("burst",), products=("codex",))
            )

        self.assertEqual([item.product for item in danger], ["dsh"])
        self.assertEqual([item.pid for item in dsh], [2])
        self.assertEqual([item.pid for item in recent], [2])
        self.assertEqual([item.pid for item in burst_and_codex], [1])

    def test_keyword_search_matches_directory_remote_and_escapes_wildcards(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = TrafficAlertStore(Path(temporary_directory))
            store.record(
                [
                    _alert(process_key="codex:1:10", pid=1),
                    _alert(
                        product="kimi",
                        process_key="kimi:2:20",
                        pid=2,
                        cwd="/srv/100%_work",
                        remote="198.51.100.7:8443",
                    ),
                ],
                now=1_000.0,
            )

            by_remote = store.query(AlertQuery(keyword="198.51.100.7"))
            by_cwd = store.query(AlertQuery(keyword="100%_work"))
            literal_wildcard = store.query(AlertQuery(keyword="100%_"))
            empty = store.query(AlertQuery(keyword="不存在的关键词"))

        self.assertEqual([item.pid for item in by_remote], [2])
        self.assertEqual([item.pid for item in by_cwd], [2])
        self.assertEqual([item.pid for item in literal_wildcard], [2])
        self.assertEqual(empty, ())

    def test_pagination_reports_more_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = TrafficAlertStore(Path(temporary_directory))
            store.record(
                [
                    _alert(process_key=f"codex:{index}:{index}", pid=index,
                           observed_at=1_000.0 + index)
                    for index in range(1, 6)
                ],
                now=1_000.0,
            )

            first_page, has_more = store.query_page(AlertQuery(limit=2))
            second_page, second_more = store.query_page(
                AlertQuery(limit=2, offset=3)
            )

        self.assertEqual(len(first_page), 2)
        self.assertTrue(has_more)
        self.assertEqual(len(second_page), 2)
        self.assertFalse(second_more)
        self.assertEqual(first_page[0].pid, 5)

    def test_acknowledge_unacknowledge_and_all(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = TrafficAlertStore(Path(temporary_directory))
            stored = store.record(
                [
                    _alert(process_key="codex:1:10", pid=1),
                    _alert(process_key="codex:2:20", pid=2),
                ],
                now=1_000.0,
            )

            changed = store.acknowledge([stored[0].id], now=1_100.0)
            after_single = store.stats()

            changed_all = store.acknowledge(all_alerts=True, now=1_200.0)
            after_all = store.stats()

            restored = store.unacknowledge([stored[0].id])
            after_restore = store.stats()

        self.assertEqual(changed, 1)
        self.assertEqual(changed_all, 1)
        self.assertEqual(restored, 1)
        self.assertEqual(after_single["unread"], 1)
        self.assertEqual(after_all["unread"], 0)
        self.assertEqual(after_restore["unread"], 1)

    def test_merged_alert_becomes_unread_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = TrafficAlertStore(Path(temporary_directory))
            stored = store.record([_alert()], now=1_000.0)
            store.acknowledge([stored[0].id], now=1_010.0)

            store.record([_alert()], now=1_020.0)
            alerts = store.query()

        self.assertEqual(alerts[0].count, 2)
        self.assertFalse(alerts[0].acknowledged)

    def test_query_filters_by_acknowledged_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = TrafficAlertStore(Path(temporary_directory))
            stored = store.record(
                [
                    _alert(process_key="codex:1:10", pid=1),
                    _alert(process_key="codex:2:20", pid=2),
                ],
                now=1_000.0,
            )
            store.acknowledge([stored[0].id], now=1_100.0)

            unread = store.query(AlertQuery(acknowledged=False))
            read = store.query(AlertQuery(acknowledged=True))

        self.assertEqual([item.pid for item in unread], [2])
        self.assertEqual([item.pid for item in read], [1])

    def test_clear_preview_and_clear_operations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = TrafficAlertStore(Path(temporary_directory))
            stored = store.record(
                [
                    _alert(process_key="codex:1:10", pid=1, observed_at=1_000.0),
                    _alert(process_key="codex:2:20", pid=2, observed_at=5_000.0),
                ],
                now=1_000.0,
            )

            pending = store.count_before(4_000.0)
            removed_before = store.clear_before(4_000.0)
            removed_id = store.clear([stored[1].id])
            removed_all = store.clear_all()

        self.assertEqual(pending, 1)
        self.assertEqual(removed_before, 1)
        self.assertEqual(removed_id, 1)
        self.assertEqual(removed_all, 0)

    def test_prune_respects_retention_days(self) -> None:
        day = 86_400.0
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = TrafficAlertStore(
                Path(temporary_directory),
                retention_days=1.0,
            )
            store.record(
                [
                    _alert(
                        process_key="codex:1:10",
                        pid=1,
                        observed_at=1_000.0,
                    ),
                    _alert(
                        process_key="codex:2:20",
                        pid=2,
                        observed_at=10 * day,
                    ),
                ],
                now=1_000.0,
            )

            removed = store.prune(now=10 * day, force=True)
            remaining = store.query()

        self.assertEqual(removed, 1)
        self.assertEqual([item.pid for item in remaining], [2])

    def test_invalid_query_limits_are_rejected(self) -> None:
        with self.assertRaises(AlertStoreError):
            AlertQuery(limit=0)
        with self.assertRaises(AlertStoreError):
            AlertQuery(limit=10_000)
        with self.assertRaises(AlertStoreError):
            AlertQuery(offset=-1)
        with self.assertRaises(AlertStoreError):
            AlertQuery(levels=("critical",))

    def test_store_requires_positive_retention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(ValueError):
                TrafficAlertStore(Path(temporary_directory), retention_days=0)

    def test_empty_store_queries_return_empty_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = TrafficAlertStore(Path(temporary_directory))

            self.assertEqual(store.query(), ())
            self.assertEqual(store.stats()["total"], 0)
            self.assertEqual(store.acknowledge(all_alerts=True), 0)
            self.assertEqual(store.clear_all(), 0)

    @unittest.skipIf(os.name != "posix", "只验证 POSIX 权限位")
    def test_database_file_is_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = TrafficAlertStore(Path(temporary_directory))
            store.record([_alert()], now=1_000.0)

            mode = store.database_file.stat().st_mode & 0o777

        self.assertEqual(mode, 0o600)

    def test_count_all_and_vacuum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = TrafficAlertStore(Path(temporary_directory))

            # 数据库文件尚未创建时 count_all/vacuum 都是安全的空操作。
            self.assertEqual(store.count_all(), 0)
            store.vacuum()
            self.assertFalse(store.db_path.exists())

            store.record([_alert(observed_at=1_000.0)], now=1_000.0)
            store.record(
                [_alert(pid=200, process_key="codex:200:1000", observed_at=1_100.0)],
                now=1_100.0,
            )
            self.assertEqual(store.count_all(), 2)
            self.assertTrue(store.db_path.exists())

            # vacuum 后数据库必须继续可读写。
            store.vacuum()
            self.assertEqual(store.count_all(), 2)
            store.record(
                [_alert(pid=300, process_key="codex:300:1000", observed_at=1_200.0)],
                now=1_200.0,
            )
            self.assertEqual(store.count_all(), 3)


if __name__ == "__main__":
    unittest.main()
