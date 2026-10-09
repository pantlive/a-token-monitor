"""Command Code 用量闭环、增量汇总和 SQL 分页的验收测试。"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from a_token_monitor.usage import (
    TokenUsage,
    UsageAggregator,
    UsageDelta,
    _UsageAggregate,
    _UsageSource,
    _aggregate_to_dict,
    _estimate_usage,
)


def _message(
    entry_id: str, timestamp: float, model: str = "gpt-6.1-sol"
) -> dict[str, object]:
    """官方 CLI sessionStore 的 assistant 请求用量格式。"""

    return {
        "type": "message",
        "id": entry_id,
        "timestamp": timestamp,
        "model": model,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "SECRET-CONTENT"}],
        },
        "usage": {
            "inputTokens": 1000,
            "outputTokens": 100,
            "cacheReadTokens": 300,
            "cacheWriteTokens": 200,
        },
    }


def _write_records(path: Path, records: list[dict[str, object]]) -> None:
    """只向临时文件写入合成记录。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8"
    )


class CommandCodeUsageTests(unittest.TestCase):
    """真实格式从发现、检查点到各检索视图必须完整贯通。"""

    def test_restart_append_dedup_partial_line_and_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "command-code"
            path = home / "projects" / "demo" / "session-1.jsonl"
            now = time.time()
            _write_records(
                path,
                [
                    {
                        "type": "session",
                        "cwd": "/workspace/demo",
                        "timestamp": now - 10,
                    },
                    _message("request-1", now - 9),
                    _message("request-2", now - 8),
                ],
            )
            _write_records(
                path.with_name("session-1.checkpoints.jsonl"),
                [_message("sidecar", now - 7)],
            )
            cache = root / "usage.sqlite3"
            first = UsageAggregator(homes={"commandcode": (home,)}, cache_path=cache)
            first.refresh_index({}, now=now)
            summary = first.session_usages([str(path)])[str(path)]
            self.assertEqual(summary.total_tokens, 2200)
            self.assertEqual(
                first.search()["totals"]["usage"]["cached_input_tokens"], 600
            )
            self.assertEqual(
                first.search()["totals"]["usage"]["cache_write_input_tokens"], 400
            )
            self.assertNotIn("SECRET-CONTENT", json.dumps(first.search()))
            first.close()

            resumed = UsageAggregator(homes={"commandcode": (home,)}, cache_path=cache)
            try:
                # 中文注释：readers 与 parsing 各自引用 _parse_usage_chunk，
                # 两处共用同一个 mock 才能断言整个索引流程都没有重新解析。
                with patch(
                    "a_token_monitor.usage.readers._parse_usage_chunk"
                ) as parse, patch(
                    "a_token_monitor.usage.parsing._parse_usage_chunk", new=parse
                ):
                    resumed.refresh_index({}, now=now)
                    parse.assert_not_called()
                partial = json.dumps(_message("request-3", now - 6))
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(_message("request-1", now - 9)) + "\n")
                    stream.write(partial[: len(partial) // 2])
                resumed.refresh_index({}, now=now)
                self.assertEqual(
                    resumed.session_usages([str(path)])[str(path)].total_tokens, 2200
                )
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(partial[len(partial) // 2 :] + "\n")
                resumed.refresh_index({}, now=now)
                self.assertEqual(
                    resumed.session_usages([str(path)])[str(path)].total_tokens, 3300
                )
                # 中文注释：截断应替换旧增量，而非把新文件叠加到旧历史。
                _write_records(path, [_message("replacement", now - 1)])
                resumed.refresh_index({}, now=now)
                self.assertEqual(
                    resumed.session_usages([str(path)])[str(path)].total_tokens, 1100
                )
            finally:
                resumed.close()

    def test_all_views_and_hot_reload_include_commandcode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = time.time()
            homes = root / "first", root / "second"
            for index, home in enumerate(homes):
                path = home / "projects" / "demo" / f"session-{index}.jsonl"
                _write_records(path, [_message(f"request-{index}", now - 1)])
                (home / "auth.json").write_text(
                    json.dumps({"userId": f"account-{index}"}), encoding="utf-8"
                )
            aggregator = UsageAggregator(
                homes={
                    "commandcode": (homes[0],),
                },cache_path=root / "usage.sqlite3"
            )
            try:
                aggregator.refresh_index({}, now=now)
                for group in ("session", "date", "model", "account"):
                    result = aggregator.search(group=group, account="command-code")
                    self.assertEqual(result["totals"]["total_tokens"], 1100)
                    self.assertEqual(result["matched_rows"], 1)
                aggregator.update_homes(homes={"commandcode": (homes[1],)})
                snapshot = aggregator.snapshot({}, now=now)
                accounts = snapshot["periods"][0]["accounts"]
                self.assertEqual(
                    [item["account_id"] for item in accounts], ["account-1"]
                )
                self.assertEqual(aggregator.usage_facets()["sessions"], 1)
            finally:
                aggregator.close()


class IncrementalRollupTests(unittest.TestCase):
    """对照逐请求计算，验证时间边界、缓存、长上下文和重写后的金额口径。"""

    def test_matches_request_reference_and_only_prices_new_tail(self) -> None:
        now = time.time()
        path = Path("/synthetic/session.jsonl")
        source = _UsageSource("test", "account", None, project="/workspace/default")
        sources = {path: source}
        short = TokenUsage(
            input_tokens=200000,
            cached_input_tokens=50000,
            output_tokens=100,
            total_tokens=200100,
        )
        long = TokenUsage(
            input_tokens=400000,
            cache_write_input_tokens=10000,
            output_tokens=300,
            total_tokens=400300,
        )
        deltas = (
            UsageDelta(now - 3600, "gpt-6.1-sol", short),
            UsageDelta(now - 1800, "gpt-6.1-sol", long, project="/workspace/other"),
            UsageDelta(now + 10, "gpt-6.1-sol", short),
        )
        aggregator = UsageAggregator()
        try:
            # 中文注释：_build_period / _build_daily 经 aggregates 子模块的
            # _UsageAggregate 调用 _estimate_usage，所以在那里统计调用次数。
            with patch(
                "a_token_monitor.usage.aggregates._estimate_usage",
                wraps=_estimate_usage,
            ) as estimate:
                first = aggregator._build_period(
                    "test", "test", "seven_days", now, sources, {path: deltas}, {}
                )
                self.assertEqual(estimate.call_count, 3)
                aggregator._build_daily(now, {path: deltas})
                same = aggregator._build_period(
                    "test", "test", "seven_days", now, sources, {path: deltas}, {}
                )
                self.assertEqual(estimate.call_count, 3)
                self.assertEqual(first, same)
                appended = deltas + (UsageDelta(now - 100, "gpt-6.1-sol", short),)
                aggregator._build_period(
                    "test", "test", "seven_days", now, sources, {path: appended}, {}
                )
                self.assertEqual(estimate.call_count, 4)
            reference = _UsageAggregate()
            for delta in deltas[:2]:
                reference.add(source, delta)
            expected = _aggregate_to_dict(
                reference,
                account_name=source.account_name,
                account_id=source.account_id,
                profiles=reference.profiles,
            )
            self.assertEqual(first["accounts"], [expected])
            replacement = (UsageDelta(now - 50, "gpt-6.1-sol", short),)
            replaced = aggregator._build_period(
                "test", "test", "seven_days", now, sources, {path: replacement}, {}
            )
            self.assertEqual(
                replaced["accounts"][0]["total_tokens"], short.total_tokens
            )
        finally:
            aggregator.close()

    def test_sql_pages_keep_complete_totals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "command-code"
            now = time.time()
            for index in range(25):
                path = home / "projects" / "demo" / f"session-{index}.jsonl"
                _write_records(path, [_message(f"request-{index}", now - index)])
            aggregator = UsageAggregator(
                homes={
                    "commandcode": (home,),
                },cache_path=root / "usage.sqlite3"
            )
            try:
                aggregator.refresh_index({}, now=now)
                result = aggregator.search(group="session", limit=3, offset=3)
                self.assertEqual(len(result["rows"]), 3)
                self.assertEqual(result["matched_rows"], 25)
                self.assertEqual(result["totals"]["total_tokens"], 27500)
                self.assertEqual(result["totals"]["sessions"], 25)
                self.assertTrue(result["has_more"])
                self.assertEqual(
                    [item["session_id"] for item in result["rows"]],
                    ["session-3", "session-4", "session-5"],
                )
                self.assertFalse(result["truncated"])
            finally:
                aggregator.close()
