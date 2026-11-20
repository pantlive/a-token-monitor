"""用量索引的吞吐、批量读写和限速测试。"""

from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from a_token_monitor.registry import MultiSessionRegistry
from a_token_monitor.usage import UsageAggregator, _UsageIndexStore

_MIB = 1024 * 1024
_NOW = (
    datetime.fromisoformat("2026-08-27T12:00:00+00:00")
    .replace(tzinfo=timezone.utc)
    .timestamp()
)


def _usage_line(timestamp: str, total: int) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "thread_settings": {"model": "gpt-5.6-luna"},
            "info": {
                "total_token_usage": {
                    "input_tokens": total,
                    "output_tokens": 0,
                    "total_tokens": total,
                }
            },
        },
    }


def _padding_line(size: int) -> dict[str, object]:
    # 中文注释：模拟 Codex 的大段工具输出，不含任何用量或模型线索。
    return {
        "timestamp": "2026-08-27T01:00:30Z",
        "type": "response_item",
        "payload": {"text": "x" * size},
    }


def _write_session(path: Path, events: int, padding: int = 0) -> int:
    """写一个累计用量递增的会话，返回它的 token 合计。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for index in range(1, events + 1):
        lines.append(json.dumps(_usage_line("2026-08-27T01:00:00Z", index * 100)))
        if padding:
            lines.append(json.dumps(_padding_line(padding)))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return events * 100


def _append_usage(path: Path, total: int) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_usage_line("2026-08-27T02:00:00Z", total)) + "\n")


def _metadata(home: Path) -> dict[str, dict[str, str]]:
    return {
        "codex": {
            "account_id": "account-personal",
            "profile_name": "codex",
            "codex_home": str(home),
        }
    }


def _today_tokens(snapshot: dict[str, object]) -> int:
    periods = snapshot["periods"]
    assert isinstance(periods, list)
    for period in periods:
        if period["key"] == "today":
            return sum(account["total_tokens"] for account in period["accounts"])
    raise AssertionError("缺少 today 时间窗口")


class UsageIndexSpeedTests(unittest.TestCase):
    """验证默认预算下的吞吐、SQLite 批量读写和重复写入的跳过。"""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.home = self.root / ".codex"
        self.registry = MultiSessionRegistry(self.root / "state")
        self.cache_path = self.root / "state" / "usage-index.sqlite3"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _snapshot(self, aggregator: UsageAggregator) -> dict[str, object]:
        return aggregator.snapshot(
            {"codex": self.registry},
            account_metadata=_metadata(self.home),
            now=_NOW,
        )

    def test_default_budget_indexes_40_mib_within_bounded_rounds(self) -> None:
        expected = 0
        for index in range(8):
            expected += _write_session(
                self.home / "sessions" / f"big-{index}.jsonl",
                events=40,
                padding=128 * 1024,
            )
        total_bytes = sum(
            path.stat().st_size for path in (self.home / "sessions").glob("*.jsonl")
        )
        self.assertGreaterEqual(total_bytes, 38 * _MIB)
        aggregator = UsageAggregator(
            discovery_interval=60.0,
            refresh_interval=0.01,
            cache_path=self.cache_path,
        )
        maximum_rounds = math.ceil(total_bytes / aggregator.read_budget_bytes) + 1

        rounds = 0
        snapshot = self._snapshot(aggregator)
        rounds += 1
        while not snapshot["indexing"]["complete"] and rounds < maximum_rounds:
            snapshot = self._snapshot(aggregator)
            rounds += 1

        self.assertTrue(snapshot["indexing"]["complete"])
        self.assertLessEqual(rounds, maximum_rounds)
        self.assertEqual(_today_tokens(snapshot), expected)

        # 中文注释：持久索引重开后结果不变，且无需再读 JSONL 内容。
        reopened = UsageAggregator(
            discovery_interval=60.0,
            refresh_interval=0.01,
            cache_path=self.cache_path,
        )
        warm = self._snapshot(reopened)
        self.assertTrue(warm["indexing"]["complete"])
        self.assertEqual(warm["indexing"]["read_bytes_this_refresh"], 0)
        self.assertEqual(_today_tokens(warm), expected)

    def test_load_many_matches_individual_loads(self) -> None:
        for index in range(4):
            _write_session(self.home / "sessions" / f"s-{index}.jsonl", events=5)
        aggregator = UsageAggregator(
            discovery_interval=60.0,
            refresh_interval=0.01,
            cache_path=self.cache_path,
        )
        self.assertTrue(self._snapshot(aggregator)["indexing"]["complete"])
        store = _UsageIndexStore(self.cache_path)
        paths = sorted((self.home / "sessions").glob("*.jsonl"))
        corrupt = paths[0]
        missing = self.home / "sessions" / "missing.jsonl"
        with closing(sqlite3.connect(self.cache_path)) as connection, connection:
            connection.execute(
                "UPDATE usage_file_state SET state_json = '{' WHERE path = ?",
                (str(corrupt),),
            )

        batch = store.load_many([*paths, missing])

        for path in paths:
            single = store.load(path)
            if single is None:
                self.assertNotIn(path, batch)
            else:
                self.assertEqual(batch[path], single)
        self.assertIsNone(store.load(corrupt))
        self.assertNotIn(corrupt, batch)
        self.assertNotIn(missing, batch)
        self.assertEqual(len(batch), len(paths) - 1)
        self.assertEqual(store.load_many([]), {})

    def test_one_round_reuses_a_single_connection(self) -> None:
        paths = [self.home / "sessions" / f"s-{index}.jsonl" for index in range(3)]
        for path in paths:
            _write_session(path, events=3)
        aggregator = UsageAggregator(
            discovery_interval=60.0,
            refresh_interval=0.01,
            cache_path=self.cache_path,
        )
        first = self._snapshot(aggregator)
        self.assertTrue(first["indexing"]["complete"])
        for path in paths:
            _append_usage(path, 1000)
        store = aggregator._persistent
        assert store is not None

        with patch.object(store, "_connect", wraps=store._connect) as connect:
            second = self._snapshot(aggregator)

        self.assertEqual(connect.call_count, 1)
        self.assertTrue(second["indexing"]["complete"])
        self.assertEqual(_today_tokens(second), 3 * 1000)
        # 中文注释：一次提交后三份文件的检查点和增量都已落盘。
        reopened = _UsageIndexStore(self.cache_path)
        for path in paths:
            cached = reopened.load(path)
            assert cached is not None
            self.assertEqual(cached.next_offset, path.stat().st_size)
            self.assertEqual(len(cached.total_deltas), 4)

    def test_failed_save_rolls_back_only_that_file(self) -> None:
        store = _UsageIndexStore(self.cache_path)
        aggregator = UsageAggregator(
            discovery_interval=60.0,
            refresh_interval=0.01,
            cache_path=self.cache_path,
        )
        good = self.home / "sessions" / "good.jsonl"
        bad = self.home / "sessions" / "bad.jsonl"
        _write_session(good, events=2)
        _write_session(bad, events=2)
        self._snapshot(aggregator)
        cached = store.load(good)
        assert cached is not None

        with store.transaction():
            store.save(good, cached, cached.total_deltas, (), replace_deltas=True)
            with patch(
                "a_token_monitor.usage.store._delta_to_row",
                side_effect=sqlite3.DatabaseError("boom"),
            ):
                # 中文注释：异常被 save 吞掉，且只回滚这一份文件的写入。
                store.save(bad, cached, cached.total_deltas, (), replace_deltas=True)

        reloaded_good = store.load(good)
        reloaded_bad = store.load(bad)
        assert reloaded_good is not None and reloaded_bad is not None
        self.assertEqual(len(reloaded_good.total_deltas), 2)
        self.assertEqual(len(reloaded_bad.total_deltas), 2)

    def test_unchanged_sources_skip_prune_and_account_writes(self) -> None:
        _write_session(self.home / "sessions" / "a.jsonl", events=2)
        aggregator = UsageAggregator(
            discovery_interval=60.0,
            refresh_interval=0.01,
            cache_path=self.cache_path,
        )
        store = aggregator._persistent
        assert store is not None
        with patch.object(store, "prune", wraps=store.prune) as prune, patch.object(
            store, "save_file_accounts", wraps=store.save_file_accounts
        ) as accounts:
            self._snapshot(aggregator)
            self._snapshot(aggregator)
            self.assertEqual(prune.call_count, 1)
            self.assertEqual(accounts.call_count, 1)

            # 中文注释：来源集合变化（新文件）后两者都要重新执行。
            aggregator._discovered.retain(lambda _key: False)
            _write_session(self.home / "sessions" / "b.jsonl", events=2)
            self._snapshot(aggregator)
            self.assertEqual(prune.call_count, 2)
            self.assertEqual(accounts.call_count, 2)

    def test_reported_rate_follows_measured_throughput_within_cap(self) -> None:
        aggregator = UsageAggregator()
        cap = aggregator.index_bytes_per_sec
        self.assertEqual(aggregator._reported_bytes_per_sec(), cap)

        aggregator._record_throughput(4 * _MIB, 1.0)
        self.assertEqual(aggregator._reported_bytes_per_sec(), 4 * _MIB)
        aggregator._record_throughput(64 * _MIB, 1.0)
        self.assertLessEqual(aggregator._reported_bytes_per_sec(), cap)
        # 中文注释：剩余 83 MiB 时，按默认上限估算的剩余时间不超过 10 秒。
        self.assertLessEqual(math.ceil(83 * _MIB / cap), 10)

    def test_rate_limit_spans_the_whole_cycle_including_cpu_work(self) -> None:
        aggregator = UsageAggregator()
        # 中文注释：读盘 0.1 秒 + 预建分桶 0.2 秒后，再等 0.2 秒凑满 0.5 秒周期。
        delay = aggregator._background_delay(
            complete=False,
            read_bytes=8 * _MIB,
            elapsed=0.1,
            busy=0.3,
        )
        self.assertAlmostEqual(delay, 0.2)
        self.assertLessEqual(8 * _MIB / (0.3 + delay), aggregator.index_bytes_per_sec)

    def test_prebuilt_rollups_produce_identical_snapshot(self) -> None:
        for index in range(3):
            _write_session(self.home / "sessions" / f"s-{index}.jsonl", events=6)
        plain = UsageAggregator(
            discovery_interval=60.0,
            refresh_interval=0.01,
            cache_path=self.cache_path,
        )
        expected = self._snapshot(plain)
        self.assertTrue(expected["indexing"]["complete"])

        prebuilt = UsageAggregator(
            discovery_interval=60.0,
            refresh_interval=0.01,
            cache_path=self.cache_path,
        )
        prebuilt._warm_cache(
            prebuilt._build_sources({"codex": self.registry}, _metadata(self.home))
        )
        prebuilt._prebuild_rollups(math.inf)
        self.assertEqual(len(prebuilt._rollups), 3)
        actual = self._snapshot(prebuilt)

        for key in ("periods", "daily", "heatmap"):
            self.assertEqual(actual[key], expected[key])

    def test_duty_cycle_leaves_disk_idle_at_least_half_the_time(self) -> None:
        aggregator = UsageAggregator()
        for elapsed in (0.01, 0.2, 0.5, 1.0, 3.0):
            delay = aggregator._background_delay(
                complete=False,
                read_bytes=aggregator.read_budget_bytes,
                elapsed=elapsed,
            )
            self.assertGreaterEqual(delay / (delay + elapsed), 0.5 - 1e-9)


if __name__ == "__main__":
    unittest.main()
