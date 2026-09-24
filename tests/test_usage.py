"""JSONL 用量汇总测试。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from a_token_monitor.registry import MultiSessionRegistry
from a_token_monitor.usage import (
    SessionSwitchThresholds,
    TokenUsage,
    UsageAggregator,
    _estimate_usage,
    _lookup_pricing,
    _UsageIndexStore,
    search_since_days,
)


class UsageAggregatorTests(unittest.TestCase):
    """验证 token 增量、模型归属、时间窗口和账号归组。"""

    def test_reads_cumulative_usage_and_updates_after_append(self) -> None:
        # 中文注释：这条用例需要一个「没有公开单价」的模型来验证未定价分支，
        # 因此用不会进价目表的占位 ID；不要换成真实模型名（例如 glm-5.3）。
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_path = root / ".codex" / "sessions" / "session.jsonl"
            session_path.parent.mkdir(parents=True)
            lines = [
                {
                    "timestamp": "2026-08-26T16:59:00Z",
                    "type": "session_meta",
                    "payload": {"cwd": "/workspace/project-a"},
                },
                {
                    "timestamp": "2026-08-26T17:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "thread_settings": {"model": "gpt-5.6-luna"},
                    },
                },
                {
                    "timestamp": "2026-08-26T17:01:00Z",
                    "type": "event_msg",
                    "payload": {
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 100,
                                "cached_input_tokens": 20,
                                "output_tokens": 30,
                                "reasoning_output_tokens": 10,
                                "total_tokens": 130,
                            },
                        },
                    },
                },
                {
                    "timestamp": "2026-08-27T01:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "thread_settings": {"model": "gpt-5.6-sol"},
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 300,
                                "cached_input_tokens": 100,
                                "output_tokens": 80,
                                "reasoning_output_tokens": 30,
                                "total_tokens": 380,
                            },
                        },
                    },
                },
                {
                    "timestamp": "2026-08-27T01:01:00Z",
                    "type": "event_msg",
                    "payload": {
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 300,
                                "cached_input_tokens": 100,
                                "output_tokens": 80,
                                "reasoning_output_tokens": 30,
                                "total_tokens": 380,
                            },
                        },
                    },
                },
            ]
            session_path.write_text(
                "\n".join(json.dumps(line) for line in lines) + "\n",
                encoding="utf-8",
            )
            registry = MultiSessionRegistry(root / "state")
            aggregator = UsageAggregator(discovery_interval=0.01)
            metadata = {
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(root / ".codex"),
                },
            }
            now = _timestamp("2026-08-27T12:00:00Z")

            state = aggregator.snapshot(
                {"codex": registry},
                account_metadata=metadata,
                now=now,
            )
            today = _period(state, "today")
            account = today["accounts"][0]
            self.assertEqual(account["input_tokens"], 300)
            self.assertEqual(account["cached_input_tokens"], 100)
            self.assertEqual(account["output_tokens"], 80)
            self.assertEqual(account["reasoning_output_tokens"], 30)
            self.assertEqual(account["total_tokens"], 380)
            self.assertEqual(
                [item["model"] for item in account["models"]],
                ["gpt-5.6-luna", "gpt-5.6-sol"],
            )
            self.assertEqual(account["models"][0]["total_tokens"], 130)
            self.assertEqual(account["models"][1]["total_tokens"], 250)
            self.assertIsNone(account["estimated_credits"])
            self.assertEqual(account["estimated_cost_usd"], 0.0015644)
            self.assertEqual(account["api_equivalent_cost_usd"], 0.0015644)
            self.assertIsNone(account["subscription_cost_usd"])
            self.assertFalse(account["credits_pricing_complete"])
            self.assertTrue(account["pricing_complete"])
            self.assertEqual(
                account["projects"][0]["project"],
                "/workspace/project-a",
            )
            self.assertEqual(account["projects"][0]["total_tokens"], 380)
            cached_state = aggregator.snapshot(
                {"codex": registry},
                account_metadata=metadata,
                now=now,
            )
            self.assertEqual(
                _period(cached_state, "today")["accounts"][0]["total_tokens"],
                380,
            )

            with session_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "timestamp": "2026-08-27T11:00:00Z",
                            "type": "event_msg",
                            "payload": {
                                "info": {
                                    "total_token_usage": {
                                        "input_tokens": 500,
                                        "cached_input_tokens": 150,
                                        "output_tokens": 100,
                                        "reasoning_output_tokens": 40,
                                        "total_tokens": 600,
                                    },
                                },
                            },
                        }
                    )
                    + "\n"
                )
            updated_state = aggregator.snapshot(
                {"codex": registry},
                account_metadata=metadata,
                now=now,
            )
            updated_account = _period(updated_state, "today")["accounts"][0]
            self.assertEqual(updated_account["total_tokens"], 600)
            self.assertEqual(updated_account["models"][1]["total_tokens"], 470)
            cached_file = aggregator._cache[session_path.resolve()]
            self.assertLess(
                cached_file.last_read_bytes,
                session_path.stat().st_size,
            )
            known_credits = updated_account["estimated_credits"]
            known_cost = updated_account["estimated_cost_usd"]

            with session_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    "\n".join(
                        json.dumps(item)
                        for item in (
                            {
                                "timestamp": "2026-08-27T11:30:00Z",
                                "type": "event_msg",
                                "payload": {
                                    "thread_settings": {
                                        "model": "local-experimental-model-xyz"
                                    },
                                },
                            },
                            {
                                "timestamp": "2026-08-27T11:31:00Z",
                                "type": "event_msg",
                                "payload": {
                                    "info": {
                                        "total_token_usage": {
                                            "input_tokens": 600,
                                            "cached_input_tokens": 150,
                                            "output_tokens": 100,
                                            "reasoning_output_tokens": 40,
                                            "total_tokens": 700,
                                        },
                                    },
                                },
                            },
                        )
                    )
                    + "\n"
                )
            mixed_state = aggregator.snapshot(
                {"codex": registry},
                account_metadata=metadata,
                now=now,
            )
            mixed_account = _period(mixed_state, "today")["accounts"][0]
            self.assertEqual(mixed_account["total_tokens"], 700)
            self.assertEqual(mixed_account["estimated_credits"], known_credits)
            self.assertEqual(mixed_account["estimated_cost_usd"], known_cost)
            self.assertEqual(
                mixed_account["unpriced_models"],
                ["local-experimental-model-xyz"],
            )
            self.assertFalse(mixed_account["pricing_complete"])

    def test_never_exposes_partial_totals_as_final_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_path = root / ".codex" / "sessions" / "session.jsonl"
            session_path.parent.mkdir(parents=True)
            session_path.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "timestamp": "2026-08-27T01:00:00Z",
                            "type": "event_msg",
                            "payload": {
                                "info": {
                                    "total_token_usage": {
                                        "input_tokens": index,
                                        "total_tokens": index,
                                    }
                                }
                            },
                        }
                    )
                    for index in range(1, 200)
                )
                + "\n",
                encoding="utf-8",
            )
            registry = MultiSessionRegistry(root / "state")
            aggregator = UsageAggregator(
                discovery_interval=0.01,
                refresh_interval=0.01,
                read_budget_bytes=256,
            )
            metadata = {
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(root / ".codex"),
                }
            }

            state = aggregator.snapshot(
                {"codex": registry},
                account_metadata=metadata,
                now=_timestamp("2026-08-27T12:00:00Z"),
            )

        self.assertFalse(state["indexing"]["complete"])
        self.assertEqual(state["periods"], [])

    def test_persists_index_checkpoint_without_replaying_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            session_path = home / "sessions" / "session.jsonl"
            session_path.parent.mkdir(parents=True)
            events = [
                {
                    "timestamp": "2026-08-27T01:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "info": {
                            "total_token_usage": {
                                "input_tokens": index * 100,
                                "total_tokens": index * 100,
                            }
                        }
                    },
                }
                for index in range(1, 80)
            ]
            session_path.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            registry = MultiSessionRegistry(root / "state")
            metadata = {
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(home),
                }
            }
            cache_path = root / "state" / "usage-index.sqlite3"
            first_aggregator = UsageAggregator(
                discovery_interval=0.01,
                refresh_interval=0.01,
                read_budget_bytes=512,
                cache_path=cache_path,
            )
            first = first_aggregator.snapshot(
                {"codex": registry},
                account_metadata=metadata,
                now=_timestamp("2026-08-27T12:00:00Z"),
            )
            second_aggregator = UsageAggregator(
                discovery_interval=0.01,
                refresh_interval=0.01,
                read_budget_bytes=512,
                cache_path=cache_path,
            )
            second = second_aggregator.snapshot(
                {"codex": registry},
                account_metadata=metadata,
                now=_timestamp("2026-08-27T12:00:00Z"),
            )
            cache_exists = cache_path.exists()

        self.assertFalse(first["indexing"]["complete"])
        self.assertFalse(second["indexing"]["complete"])
        self.assertEqual(second["indexing"]["read_bytes_this_refresh"], 512)
        self.assertGreater(
            second["indexing"]["indexed_bytes"],
            first["indexing"]["indexed_bytes"],
        )
        self.assertTrue(cache_exists)

    def test_applies_cache_write_and_long_context_api_pricing(self) -> None:
        estimate = _estimate_usage(
            TokenUsage(
                input_tokens=300_000,
                cached_input_tokens=100_000,
                cache_write_input_tokens=10_000,
                output_tokens=1_000,
                total_tokens=301_000,
            ),
            "gpt-5.6-luna",
        )

        self.assertEqual(estimate["estimated_credits"], None)
        self.assertEqual(estimate["estimated_cost_usd"], 0.0868)
        self.assertEqual(estimate["api_equivalent_cost_usd"], 0.0868)

    def test_applies_gpt_6_astra_api_pricing(self) -> None:
        standard = _estimate_usage(
            TokenUsage(
                input_tokens=100_000,
                cached_input_tokens=10_000,
                cache_write_input_tokens=5_000,
                output_tokens=2_000,
                total_tokens=102_000,
            ),
            "gpt-6-astra",
        )
        long_context = _estimate_usage(
            TokenUsage(
                input_tokens=272_001,
                output_tokens=1_000,
                total_tokens=273_001,
            ),
            "gpt-6-astra",
        )

        self.assertEqual(standard["api_equivalent_cost_usd"], 1.0225)
        self.assertAlmostEqual(
            long_context["api_equivalent_cost_usd"],
            5.51502,
        )

    def test_applies_gpt_6_and_mimo_api_pricing(self) -> None:
        """新发布的 GPT-6 与小米 MiMo 也要能算出 API 等价金额。"""

        small = TokenUsage(
            input_tokens=100_000,
            cached_input_tokens=20_000,
            output_tokens=5_000,
            total_tokens=105_000,
        )
        # 官方单价：sol $2/$0.2/$10，luna $0.1/$0.01/$0.5（每百万 token）。
        # uncached 80k×2 + cached 20k×0.2 + output 5k×10 = 0.16+0.004+0.05
        self.assertAlmostEqual(
            _estimate_usage(small, "gpt-6-sol")["estimated_cost_usd"],
            0.214,
        )
        self.assertAlmostEqual(
            _estimate_usage(small, "gpt-6-luna")["estimated_cost_usd"],
            0.0107,
        )
        # MiMo 国际站 $0.14/$0.0028/$0.28：0.0112+0.000056+0.0014
        self.assertAlmostEqual(
            _estimate_usage(small, "xiaomi/mimo-v2.6-flash")["estimated_cost_usd"],
            0.012656,
        )
        # 官方文档：GPT-6 超过 272K 输入按 2x 输入计费。
        long_usage = TokenUsage(input_tokens=300_000, total_tokens=300_000)
        self.assertAlmostEqual(
            _estimate_usage(long_usage, "gpt-6-sol")["estimated_cost_usd"],
            1.2,
        )
        self.assertAlmostEqual(
            _estimate_usage(long_usage, "gpt-6-luna")["estimated_cost_usd"],
            0.06,
        )
        # MiMo 没有长上下文加价，300K 输入仍是单价原值。
        self.assertAlmostEqual(
            _estimate_usage(long_usage, "xiaomi/mimo-v2.6-flash")[
                "estimated_cost_usd"
            ],
            0.042,
        )
        # 聚合商前缀、官方快照后缀与开源权重命名都要能命中同一份价格。
        self.assertIsNotNone(_lookup_pricing("xiaomi/mimo-v2.6-flash"))
        self.assertIsNotNone(_lookup_pricing("MiMo-V2.6-Flash-RL"))
        self.assertIsNotNone(_lookup_pricing("gpt-6-sol-2026-09-22"))
        self.assertIsNotNone(_lookup_pricing("GPT-6-Luna"))

    def test_applies_glm_and_step_api_pricing(self) -> None:
        """GLM-5.3 系列与阶跃 Step-5-Preview 也要能算出 API 等价金额。"""

        small = TokenUsage(
            input_tokens=100_000,
            cached_input_tokens=20_000,
            output_tokens=5_000,
            total_tokens=105_000,
        )
        # Z.ai 官方美元价目（每百万 token）：glm-5.3 $1.4/$0.26/$4.4、
        # glm-5.3-flash $0.15/$0.03/$0.5、glm-5.3-flashx $0.37/$0.075/$1.25。
        # uncached 80k×单价 + cached 20k×缓存价 + output 5k×输出价。
        self.assertAlmostEqual(
            _estimate_usage(small, "glm-5.3")["estimated_cost_usd"],
            0.1392,
        )
        self.assertAlmostEqual(
            _estimate_usage(small, "glm-5.3-flash")["estimated_cost_usd"],
            0.0151,
        )
        self.assertAlmostEqual(
            _estimate_usage(small, "glm-5.3-flashx")["estimated_cost_usd"],
            0.03735,
        )
        # 阶跃官方价目 ¥7 / ¥0.35 / ¥20，按 7.0 折算：$1 / $0.05 / $2.86。
        self.assertAlmostEqual(
            _estimate_usage(small, "step-5-preview")["estimated_cost_usd"],
            0.0953,
        )
        # 官方都没有长上下文加价：300K 输入就是单价的线性放大。
        long_usage = TokenUsage(input_tokens=300_000, total_tokens=300_000)
        self.assertAlmostEqual(
            _estimate_usage(long_usage, "glm-5.3")["estimated_cost_usd"],
            0.42,
        )
        self.assertAlmostEqual(
            _estimate_usage(long_usage, "step-5-preview")["estimated_cost_usd"],
            0.3,
        )
        # 聚合商前缀、大小写与官方快照后缀都要命中同一份价格。
        for model in (
            "z-ai/glm-5.3-flash",
            "GLM-5.3",
            "glm-5.3-flash-2601",
            "stepfun/step-5-preview",
        ):
            with self.subTest(model=model):
                self.assertIsNotNone(_lookup_pricing(model))
        # flash 与 flashx 不能互相抢：各自命中自己的价目。
        flash = _lookup_pricing("glm-5.3-flash")
        flashx = _lookup_pricing("glm-5.3-flashx")
        assert flash is not None and flashx is not None
        self.assertEqual(flash.input_usd, 0.15)
        self.assertEqual(flashx.input_usd, 0.37)

    def test_indexes_history_with_a_bounded_read_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            session_path = home / "sessions" / "bounded.jsonl"
            session_path.parent.mkdir(parents=True)
            events = [
                {
                    "timestamp": "2026-08-27T01:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "thread_settings": {"model": "gpt-5.6-luna"},
                        "info": {
                            "total_token_usage": {
                                "input_tokens": index * 100,
                                "output_tokens": 0,
                                "total_tokens": index * 100,
                            }
                        },
                    },
                }
                for index in range(1, 31)
            ]
            session_path.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            registry = MultiSessionRegistry(root / "state")
            aggregator = UsageAggregator(
                discovery_interval=0.01,
                refresh_interval=0.01,
                read_budget_bytes=512,
            )
            metadata = {
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(home),
                }
            }
            now = _timestamp("2026-08-27T12:00:00Z")

            first = aggregator.snapshot(
                {"codex": registry},
                account_metadata=metadata,
                now=now,
            )
            final = first
            for _ in range(100):
                if final["indexing"]["complete"]:
                    break
                final = aggregator.snapshot(
                    {"codex": registry},
                    account_metadata=metadata,
                    now=now,
                )

        self.assertFalse(first["indexing"]["complete"])
        self.assertLessEqual(
            first["indexing"]["read_bytes_this_refresh"],
            512,
        )
        self.assertTrue(final["indexing"]["complete"])
        self.assertEqual(
            _period(final, "today")["accounts"][0]["total_tokens"],
            3000,
        )

    def test_counts_a_monotonic_cumulative_gap_without_using_partial_last(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_path = root / ".codex" / "sessions" / "session.jsonl"
            session_path.parent.mkdir(parents=True)
            session_path.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in (
                        {
                            "timestamp": "2026-08-27T01:00:00Z",
                            "type": "event_msg",
                            "payload": {
                                "thread_settings": {"model": "gpt-5.6-luna"},
                            },
                        },
                        {
                            "timestamp": "2026-08-27T01:01:00Z",
                            "type": "event_msg",
                            "payload": {
                                "info": {
                                    "total_token_usage": {
                                        "input_tokens": 100,
                                        "cached_input_tokens": 20,
                                        "output_tokens": 10,
                                        "total_tokens": 110,
                                    },
                                    "last_token_usage": {
                                        "input_tokens": 100,
                                        "cached_input_tokens": 20,
                                        "output_tokens": 10,
                                        "total_tokens": 110,
                                    },
                                }
                            },
                        },
                        {
                            "timestamp": "2026-08-27T01:10:00Z",
                            "type": "event_msg",
                            "payload": {
                                "info": {
                                    "total_token_usage": {
                                        "input_tokens": 3_000_000,
                                        "cached_input_tokens": 2_800_000,
                                        "output_tokens": 5_000,
                                        "total_tokens": 3_005_000,
                                    },
                                    "last_token_usage": {
                                        "input_tokens": 200,
                                        "cached_input_tokens": 80,
                                        "output_tokens": 20,
                                        "total_tokens": 220,
                                    },
                                }
                            },
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            registry = MultiSessionRegistry(root / "state")
            metadata = {
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(root / ".codex"),
                }
            }

            state = UsageAggregator(discovery_interval=0.01).snapshot(
                {"codex": registry},
                account_metadata=metadata,
                now=_timestamp("2026-08-27T12:00:00Z"),
            )

        account = _period(state, "today")["accounts"][0]
        self.assertEqual(account["total_tokens"], 3_005_000)
        self.assertEqual(account["input_tokens"], 3_000_000)
        self.assertEqual(account["output_tokens"], 5_000)
        self.assertEqual(
            account["estimated_cost_usd"],
            _estimate_usage(
                TokenUsage(
                    input_tokens=100,
                    cached_input_tokens=20,
                    output_tokens=10,
                    total_tokens=110,
                ),
                "gpt-5.6-luna",
            )["estimated_cost_usd"]
            + _estimate_usage(
                TokenUsage(
                    input_tokens=2_999_900,
                    cached_input_tokens=2_799_980,
                    output_tokens=4_990,
                    total_tokens=3_004_890,
                ),
                "gpt-5.6-luna",
            )["estimated_cost_usd"],
        )

    def test_uses_last_request_when_cumulative_counter_resets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_path = root / ".codex" / "sessions" / "session.jsonl"
            session_path.parent.mkdir(parents=True)
            events = (
                _usage_event(total=1_000, last=1_000),
                _usage_event(total=1_500, last=500),
                _usage_event(total=800, last=100),
            )
            session_path.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            registry = MultiSessionRegistry(root / "state")
            metadata = {
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(root / ".codex"),
                }
            }

            state = UsageAggregator(discovery_interval=0.01).snapshot(
                {"codex": registry},
                account_metadata=metadata,
                now=_timestamp("2026-08-27T12:00:00Z"),
            )

        account = _period(state, "today")["accounts"][0]
        self.assertEqual(account["total_tokens"], 1_600)
        self.assertEqual(account["input_tokens"], 1_600)

    def test_skips_tool_output_lines_without_losing_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_path = root / ".codex" / "sessions" / "session.jsonl"
            session_path.parent.mkdir(parents=True)
            session_path.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in (
                        {
                            "timestamp": "2026-08-27T01:00:00Z",
                            "type": "event_msg",
                            "payload": {
                                "thread_settings": {"model": "gpt-5.6-luna"},
                            },
                        },
                        {
                            "timestamp": "2026-08-27T01:01:00Z",
                            "type": "event_msg",
                            "payload": {
                                "info": {
                                    "total_token_usage": {
                                        "input_tokens": 50,
                                        "output_tokens": 0,
                                        "total_tokens": 50,
                                    }
                                }
                            },
                        },
                        {
                            "type": "response_item",
                            "payload": {"text": "x" * 20_000},
                        },
                        {
                            "timestamp": "2026-08-27T01:02:00Z",
                            "type": "event_msg",
                            "payload": {
                                "info": {
                                    "total_token_usage": {
                                        "input_tokens": 80,
                                        "output_tokens": 0,
                                        "total_tokens": 80,
                                    }
                                }
                            },
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            registry = MultiSessionRegistry(root / "state")
            metadata = {
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(root / ".codex"),
                }
            }

            state = UsageAggregator(discovery_interval=0.01).snapshot(
                {"codex": registry},
                account_metadata=metadata,
                now=_timestamp("2026-08-27T12:00:00Z"),
            )

        self.assertEqual(
            _period(state, "today")["accounts"][0]["total_tokens"],
            80,
        )

    def test_incomplete_index_refresh_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            session_path = home / "sessions" / "session.jsonl"
            session_path.parent.mkdir(parents=True)
            events = [
                {
                    "timestamp": "2026-08-27T01:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "info": {
                            "total_token_usage": {
                                "input_tokens": index * 100,
                                "total_tokens": index * 100,
                            }
                        }
                    },
                }
                for index in range(1, 80)
            ]
            session_path.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            registry = MultiSessionRegistry(root / "state")
            metadata = {
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(home),
                }
            }
            aggregator = UsageAggregator(
                discovery_interval=0.01,
                refresh_interval=60.0,
                read_budget_bytes=256,
            )

            first = aggregator.snapshot(
                {"codex": registry},
                account_metadata=metadata,
            )
            second = aggregator.snapshot(
                {"codex": registry},
                account_metadata=metadata,
            )

        self.assertFalse(first["indexing"]["complete"])
        self.assertFalse(second["indexing"]["complete"])
        self.assertGreater(
            second["indexing"]["indexed_bytes"],
            first["indexing"]["indexed_bytes"],
        )

    def test_background_indexer_finishes_without_repeated_clicks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / ".codex"
            session_path = home / "sessions" / "bounded.jsonl"
            session_path.parent.mkdir(parents=True)
            events = [
                {
                    "timestamp": "2026-08-27T01:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "thread_settings": {"model": "gpt-5.6-luna"},
                        "info": {
                            "total_token_usage": {
                                "input_tokens": index * 100,
                                "output_tokens": 0,
                                "total_tokens": index * 100,
                            }
                        },
                    },
                }
                for index in range(1, 31)
            ]
            session_path.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )
            registry = MultiSessionRegistry(root / "state")
            metadata = {
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(home),
                }
            }
            aggregator = UsageAggregator(
                discovery_interval=0.01,
                refresh_interval=60.0,
                read_budget_bytes=512,
                background_indexing=True,
                index_bytes_per_sec=10_000_000,
            )
            try:
                first = aggregator.snapshot(
                    {"codex": registry},
                    account_metadata=metadata,
                    now=_timestamp("2026-08-27T12:00:00Z"),
                )
                finished = None
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    cached = aggregator.cached_snapshot()
                    if cached.get("indexing", {}).get("complete"):
                        finished = cached
                        break
                    time.sleep(0.05)
            finally:
                aggregator.close()

        self.assertFalse(first["indexing"]["complete"])
        self.assertIsNotNone(finished)
        assert finished is not None
        self.assertTrue(finished["indexing"]["complete"])
        self.assertEqual(
            _period(finished, "today")["accounts"][0]["total_tokens"],
            3000,
        )

    def test_background_delay_limits_disk_rate_and_completed_refreshes(self) -> None:
        aggregator = UsageAggregator(refresh_interval=300.0)

        self.assertEqual(aggregator.read_budget_bytes, 1024 * 1024)
        self.assertEqual(aggregator.index_bytes_per_sec, 1024 * 1024)
        self.assertEqual(
            aggregator._background_delay(
                complete=False,
                read_bytes=1024 * 1024,
            ),
            1.0,
        )
        self.assertEqual(
            aggregator._background_delay(complete=False, read_bytes=0),
            1.0,
        )
        self.assertEqual(
            aggregator._background_delay(complete=True, read_bytes=0),
            300.0,
        )

    def test_merges_profiles_with_the_same_real_account_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            personal_home = root / ".codex"
            work_home = root / ".codex-work"
            _write_total_usage(personal_home, 100, "gpt-5.6-luna")
            _write_total_usage(work_home, 200, "gpt-5.6-terra")
            personal_registry = MultiSessionRegistry(root / "personal-state")
            work_registry = MultiSessionRegistry(root / "work-state")
            metadata = {
                "codex": {
                    "account_id": "same-account",
                    "profile_name": "codex",
                    "codex_home": str(personal_home),
                },
                "codex-work": {
                    "account_id": "same-account",
                    "profile_name": "codex-work",
                    "codex_home": str(work_home),
                },
            }

            state = UsageAggregator(discovery_interval=0.01).snapshot(
                {"codex": personal_registry, "codex-work": work_registry},
                account_metadata=metadata,
                now=_timestamp("2026-08-27T12:00:00Z"),
            )

        account = _period(state, "today")["accounts"][0]
        self.assertEqual(account["account_id"], "same-account")
        self.assertEqual(account["total_tokens"], 300)
        self.assertEqual(set(account["profiles"]), {"codex", "codex-work"})
        self.assertEqual(
            {item["model"] for item in account["models"]},
            {"gpt-5.6-luna", "gpt-5.6-terra"},
        )

    def test_deduplicates_the_same_session_across_codex_homes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            personal_home = root / ".codex"
            work_home = root / ".codex-work"
            session_name = (
                "rollout-2026-08-27T01-00-00-"
                "01a017e8-a4ba-7c62-8838-0116bd8d1e84.jsonl"
            )
            personal_path = personal_home / "sessions" / session_name
            work_path = work_home / "sessions" / session_name
            personal_path.parent.mkdir(parents=True)
            work_path.parent.mkdir(parents=True)
            first_event = _usage_event(total=100, last=100)
            second_event = _usage_event(total=200, last=100)
            personal_path.write_text(
                json.dumps(first_event) + "\n",
                encoding="utf-8",
            )
            personal_size = personal_path.stat().st_size
            work_path.write_text(
                "\n".join(json.dumps(item) for item in (first_event, second_event))
                + "\n",
                encoding="utf-8",
            )
            metadata = {
                "codex": {
                    "account_id": "personal-account",
                    "profile_name": "codex",
                    "codex_home": str(personal_home),
                },
                "codex-work": {
                    "account_id": "work-account",
                    "profile_name": "codex-work",
                    "codex_home": str(work_home),
                },
            }
            aggregator = UsageAggregator(discovery_interval=0.01)

            state = aggregator.snapshot(
                {
                    "codex": MultiSessionRegistry(root / "personal-state"),
                    "codex-work": MultiSessionRegistry(root / "work-state"),
                },
                account_metadata=metadata,
                now=_timestamp("2026-08-27T12:00:00Z"),
            )

        accounts = {
            account["account_id"]: account
            for account in _period(state, "today")["accounts"]
        }
        self.assertEqual(accounts["personal-account"]["total_tokens"], 0)
        self.assertEqual(accounts["work-account"]["total_tokens"], 200)
        self.assertEqual(state["indexing"]["deduplicated_files"], 1)
        self.assertEqual(
            state["indexing"]["deduplicated_bytes"],
            personal_size,
        )


def _write_total_usage(home: Path, total: int, model: str) -> None:
    """写入最小的合成 session JSONL。"""

    path = home / "sessions" / "session.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(
            json.dumps(item)
            for item in (
                {
                    "timestamp": "2026-08-27T01:00:00Z",
                    "type": "event_msg",
                    "payload": {"thread_settings": {"model": model}},
                },
                {
                    "timestamp": "2026-08-27T01:01:00Z",
                    "type": "event_msg",
                    "payload": {
                        "info": {
                            "total_token_usage": {
                                "input_tokens": total,
                                "output_tokens": 0,
                                "total_tokens": total,
                            },
                        },
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )


def _usage_event(total: int, last: int) -> dict[str, object]:
    """生成同时带累计和单次 token 的测试事件。"""

    return {
        "timestamp": "2026-08-27T01:00:00Z",
        "type": "event_msg",
        "payload": {
            "thread_settings": {"model": "gpt-5.6-luna"},
            "info": {
                "total_token_usage": {
                    "input_tokens": total,
                    "total_tokens": total,
                },
                "last_token_usage": {
                    "input_tokens": last,
                    "total_tokens": last,
                },
            },
        },
    }


def _period(state: dict[str, object], key: str) -> dict[str, object]:
    """从汇总状态中取出一个时间窗口。"""

    periods = state["periods"]
    assert isinstance(periods, list)
    for period in periods:
        assert isinstance(period, dict)
        if period["key"] == key:
            return period
    raise AssertionError(f"缺少时间窗口: {key}")


class UsageInsightsTests(unittest.TestCase):
    """验证跨对话的习惯画像、规模分布和省 token 建议。"""

    def test_builds_conversation_profile_and_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sessions_dir = root / ".codex" / "sessions"
            sessions_dir.mkdir(parents=True)
            _write_session(
                sessions_dir / "short-1.jsonl",
                "/workspace/alpha",
                [("2026-08-27T09:00:00Z", 200, 0, 100, 50)],
            )
            _write_session(
                sessions_dir / "short-2.jsonl",
                "/workspace/alpha",
                [("2026-08-27T10:00:00Z", 300, 0, 100, 60)],
            )
            _write_session(
                sessions_dir / "short-3.jsonl",
                "/workspace/beta",
                [
                    ("2026-08-27T11:00:00Z", 400, 0, 200, 80),
                    ("2026-08-27T11:30:00Z", 800, 100, 300, 160),
                ],
            )
            _write_session(
                sessions_dir / "big.jsonl",
                "/workspace/gamma",
                [
                    ("2026-08-27T14:00:00Z", 30_000, 10_000, 0, 1_000),
                    ("2026-08-27T14:20:00Z", 60_000, 30_000, 0, 2_000),
                    ("2026-08-27T14:40:00Z", 90_000, 45_000, 0, 4_000),
                    ("2026-08-27T15:00:00Z", 120_000, 60_000, 0, 6_000),
                ],
            )
            _write_session(
                sessions_dir / "medium.jsonl",
                "/workspace/beta",
                [
                    ("2026-08-27T22:00:00Z", 1_000, 200, 0, 100),
                    ("2026-08-27T22:10:00Z", 2_000, 400, 0, 200),
                    ("2026-08-27T22:20:00Z", 3_000, 600, 0, 300),
                ],
            )
            registry = MultiSessionRegistry(root / "state")
            aggregator = UsageAggregator(discovery_interval=0.01)
            metadata = {
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(root / ".codex"),
                },
            }
            now = _timestamp("2026-08-27T23:00:00Z")

            insights = aggregator.insights(
                {"codex": registry},
                account_metadata=metadata,
                now=now,
            )

        self.assertTrue(insights["ready"])
        self.assertFalse(insights["insufficient"])
        self.assertEqual(insights["conversation_count"], 5)
        self.assertEqual(insights["total_tokens"], 130_870)
        self.assertAlmostEqual(insights["estimated_cost_usd"], 0.021843)
        self.assertAlmostEqual(insights["cache_hit_rate"], 48.8)
        histogram = insights["hour_histogram"]
        self.assertEqual(len(histogram), 24)
        self.assertEqual(
            sum(item["total_tokens"] for item in histogram),
            130_870,
        )
        buckets = {item["label"]: item for item in insights["size_buckets"]}
        self.assertEqual(buckets["1–2 轮"]["conversations"], 3)
        self.assertEqual(buckets["3–10 轮"]["conversations"], 2)
        self.assertEqual(buckets["31 轮以上"]["conversations"], 0)
        top = insights["top_conversations"]
        self.assertEqual(top[0]["label"], "big")
        self.assertEqual(top[0]["turns"], 4)
        self.assertAlmostEqual(top[0]["estimated_cost_usd"], 0.0204)
        self.assertEqual(top[0]["project"], "/workspace/gamma")
        models = insights["models"]
        self.assertEqual(models[0]["model"], "gpt-5.6-luna")
        self.assertIsNotNone(models[0]["estimated_cost_usd"])
        titles = [item["title"] for item in insights["suggestions"]]
        short_titles = [title for title in titles if "不超过 2 轮" in title]
        self.assertEqual(len(short_titles), 1)
        short_suggestion = next(
            item
            for item in insights["suggestions"]
            if "不超过 2 轮" in item["title"]
        )
        self.assertEqual(short_suggestion["level"], "warn")
        # 命中率 48.8% 落在 40–60% 区间，产生 tip 级缓存建议。
        cache_suggestion = next(
            item
            for item in insights["suggestions"]
            if "缓存命中率" in item["title"]
        )
        self.assertEqual(cache_suggestion["level"], "tip")
        self.assertAlmostEqual(cache_suggestion["saving_usd"], 0.0024984)
        # 最贵对话 0.0204 / 总成本 0.021843 ≈ 93%，触发集中度提示。
        self.assertTrue(
            any("最贵的一个对话" in title for title in titles),
            titles,
        )
        # 缓存建议 0.0024984 + 短对话写入溢价 0.000025。
        self.assertAlmostEqual(insights["potential_savings_usd"], 0.0025234)
        observations = insights["observations"]
        self.assertTrue(any("平均每个对话" in item for item in observations))
        self.assertTrue(any("最活跃时段" in item for item in observations))
        self.assertTrue(any("gpt-5.6-luna" in item for item in observations))
        self.assertTrue(
            any("缓存命中已累计节省" in item for item in observations),
            observations,
        )

    def test_unpriced_conversations_do_not_hide_priced_cost(self) -> None:
        """单个未定价对话不应把总成本变成未知，只标记 has_unpriced。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sessions_dir = root / ".codex" / "sessions"
            sessions_dir.mkdir(parents=True)
            for index in range(3):
                _write_session(
                    sessions_dir / f"priced-{index}.jsonl",
                    "/workspace/alpha",
                    [("2026-08-27T09:00:00Z", 1_000, 0, 0, 100)],
                )
            _write_session(
                sessions_dir / "unpriced.jsonl",
                "/workspace/alpha",
                [("2026-08-27T09:00:00Z", 5_000, 0, 0, 500)],
                model="kimi-code/kimi-for-coding",
            )
            registry = MultiSessionRegistry(root / "state")
            aggregator = UsageAggregator(discovery_interval=0.01)
            metadata = {
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(root / ".codex"),
                },
            }

            insights = aggregator.insights(
                {"codex": registry},
                account_metadata=metadata,
                now=_timestamp("2026-08-27T23:00:00Z"),
            )

        # 3 个可计价对话 × (1000×0.2 + 100×1.2)/1M。
        self.assertAlmostEqual(insights["estimated_cost_usd"], 0.00096)
        self.assertTrue(insights["has_unpriced"])
        unpriced = next(
            item
            for item in insights["top_conversations"]
            if item["label"] == "unpriced"
        )
        self.assertEqual(unpriced["estimated_cost_usd"], 0.0)
        self.assertTrue(unpriced["has_unpriced"])

    def test_praises_high_cache_hit_rate_and_flags_marathon_sessions(self) -> None:
        """高缓存命中率应得到正面反馈，超长多轮对话应提示拆分。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sessions_dir = root / ".codex" / "sessions"
            sessions_dir.mkdir(parents=True)
            for index in range(3):
                _write_session(
                    sessions_dir / f"marathon-{index}.jsonl",
                    "/workspace/alpha",
                    [
                        (
                            f"2026-08-27T09:{turn % 60:02d}:00Z",
                            (turn + 1) * 1_000,
                            int((turn + 1) * 1_000 * 0.96),
                            0,
                            (turn + 1) * 10,
                        )
                        for turn in range(101)
                    ],
                )
            registry = MultiSessionRegistry(root / "state")
            aggregator = UsageAggregator(discovery_interval=0.01)
            metadata = {
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(root / ".codex"),
                },
            }

            insights = aggregator.insights(
                {"codex": registry},
                account_metadata=metadata,
                now=_timestamp("2026-08-27T23:00:00Z"),
            )

        titles = [item["title"] for item in insights["suggestions"]]
        self.assertTrue(any("超过 100 轮" in title for title in titles), titles)
        praise = [
            item
            for item in insights["suggestions"]
            if "前缀复用做得很好" in item["title"]
        ]
        self.assertEqual(len(praise), 1)
        self.assertEqual(praise[0]["level"], "info")

    def test_insufficient_conversations_skip_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sessions_dir = root / ".codex" / "sessions"
            sessions_dir.mkdir(parents=True)
            _write_session(
                sessions_dir / "only.jsonl",
                "/workspace/alpha",
                [("2026-08-27T09:00:00Z", 200, 0, 0, 50)],
            )
            registry = MultiSessionRegistry(root / "state")
            aggregator = UsageAggregator(discovery_interval=0.01)
            metadata = {
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(root / ".codex"),
                },
            }

            insights = aggregator.insights(
                {"codex": registry},
                account_metadata=metadata,
                now=_timestamp("2026-08-27T23:00:00Z"),
            )

        self.assertTrue(insights["ready"])
        self.assertTrue(insights["insufficient"])
        self.assertEqual(insights["conversation_count"], 1)
        self.assertEqual(insights["suggestions"], [])

    def test_insights_wait_for_completed_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sessions_dir = root / ".codex" / "sessions"
            sessions_dir.mkdir(parents=True)
            _write_session(
                sessions_dir / "bounded.jsonl",
                "/workspace/alpha",
                [
                    (
                        f"2026-08-27T01:{index % 60:02d}:00Z",
                        (index + 1) * 100,
                        0,
                        0,
                        0,
                    )
                    for index in range(30)
                ],
            )
            registry = MultiSessionRegistry(root / "state")
            aggregator = UsageAggregator(
                discovery_interval=0.01,
                refresh_interval=0.01,
                read_budget_bytes=512,
            )
            metadata = {
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(root / ".codex"),
                },
            }
            now = _timestamp("2026-08-27T12:00:00Z")

            first = aggregator.insights(
                {"codex": registry},
                account_metadata=metadata,
                now=now,
            )
            final = first
            for _ in range(100):
                if final["ready"]:
                    break
                final = aggregator.insights(
                    {"codex": registry},
                    account_metadata=metadata,
                    now=now,
                )

        self.assertFalse(first["ready"])
        self.assertFalse(first["indexing"]["complete"])
        self.assertTrue(final["ready"])
        self.assertEqual(final["conversation_count"], 1)
        self.assertEqual(final["total_tokens"], 3_000)

    def test_insights_filter_by_time_window(self) -> None:
        """按天数窗口分析时只统计窗口内的对话和增量。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sessions_dir = root / ".codex" / "sessions"
            sessions_dir.mkdir(parents=True)
            _write_session(
                sessions_dir / "old.jsonl",
                "/workspace/alpha",
                [("2026-08-01T09:00:00Z", 10_000, 0, 0, 1_000)],
            )
            _write_session(
                sessions_dir / "recent.jsonl",
                "/workspace/alpha",
                [
                    ("2026-08-15T09:00:00Z", 5_000, 0, 0, 500),
                    ("2026-08-26T09:00:00Z", 7_000, 0, 0, 700),
                ],
            )
            registry = MultiSessionRegistry(root / "state")
            aggregator = UsageAggregator(discovery_interval=0.01)
            metadata = {
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(root / ".codex"),
                },
            }
            now = _timestamp("2026-08-27T23:00:00Z")

            full = aggregator.insights(
                {"codex": registry},
                account_metadata=metadata,
                now=now,
            )
            week = aggregator.insights(
                {"codex": registry},
                account_metadata=metadata,
                now=now,
                since_days=7,
            )

        self.assertIsNone(full["window_days"])
        self.assertEqual(full["conversation_count"], 2)
        self.assertEqual(full["total_tokens"], 18_700)
        self.assertEqual(week["window_days"], 7)
        # 窗口只剩 recent.jsonl 的第二个增量（7700 − 5500）。
        self.assertEqual(week["conversation_count"], 1)
        self.assertEqual(week["total_tokens"], 2_200)
        self.assertEqual(week["top_conversations"][0]["label"], "recent")
        self.assertEqual(week["top_conversations"][0]["turns"], 1)


def _write_session(
    path: Path,
    cwd: str,
    events: list[tuple[str, int, int, int, int]],
    model: str = "gpt-5.6-luna",
) -> None:
    """写一个带累计 token 快照的最小 Codex session JSONL。"""

    lines = [
        {
            "timestamp": events[0][0],
            "type": "session_meta",
            "payload": {"cwd": cwd},
        }
    ]
    for timestamp, input_tokens, cached_tokens, cache_write, output in events:
        lines.append(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {
                    "thread_settings": {"model": model},
                    "info": {
                        "total_token_usage": {
                            "input_tokens": input_tokens,
                            "cached_input_tokens": cached_tokens,
                            "cache_write_input_tokens": cache_write,
                            "output_tokens": output,
                            "reasoning_output_tokens": 0,
                            "total_tokens": input_tokens + output,
                        },
                    },
                },
            }
        )
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )


def _timestamp(value: str) -> float:
    """把测试时间转换为 Unix 秒。"""

    return (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


def _write_search_session(
    home: Path,
    session_id: str,
    cwd: str,
    rows: tuple[tuple[str, str, int], ...],
) -> Path:
    """写入一个带项目目录、模型和累计 token 的合成 session。"""

    path = (
        home
        / "sessions"
        / "2026"
        / "08"
        / "27"
        / f"rollout-2026-08-27T01-00-00-{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-27T01:00:00Z",
            "type": "session_meta",
            "payload": {"cwd": cwd},
        },
    ]
    for when, model, total in rows:
        events.append(
            {
                "timestamp": when,
                "type": "event_msg",
                "payload": {
                    "thread_settings": {"model": model},
                    "info": {
                        "total_token_usage": {
                            "input_tokens": total,
                            "output_tokens": 0,
                            "total_tokens": total,
                        }
                    },
                },
            }
        )
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    return path


class UsageSearchTests(unittest.TestCase):
    """验证按日期、模型和会话检索 token 用量历史。"""

    _PERSONAL_SESSION = "11111111-1111-4111-8111-111111111111"
    _WORK_SESSION = "22222222-2222-4222-8222-222222222222"

    def _aggregator(self, root: Path) -> UsageAggregator:
        """建立两个会话、两个模型、两天跨度的用量索引。"""

        personal = root / ".codex"
        _write_search_session(
            personal,
            self._PERSONAL_SESSION,
            "/home/dev/alpha",
            (
                ("2026-08-26T01:00:00Z", "gpt-5.6-luna", 1_000),
                ("2026-08-27T01:00:00Z", "gpt-5.6-luna", 3_000),
            ),
        )
        _write_search_session(
            personal,
            self._WORK_SESSION,
            "/home/dev/beta",
            (
                ("2026-08-27T02:00:00Z", "gpt-5.6-sol", 5_000),
                ("2026-08-27T03:00:00Z", "gpt-5.6-luna", 9_000),
            ),
        )
        registry = MultiSessionRegistry(root / "state")
        metadata = {
            "codex": {
                "account_id": "account-personal",
                "profile_name": "codex",
                "codex_home": str(personal),
            }
        }
        aggregator = UsageAggregator(
            discovery_interval=0.01,
            refresh_interval=0.01,
            cache_path=root / "state" / "usage-index.sqlite3",
        )
        aggregator.snapshot(
            {"codex": registry},
            account_metadata=metadata,
            now=_timestamp("2026-08-27T12:00:00Z"),
        )
        return aggregator

    def test_groups_by_date_session_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            aggregator = self._aggregator(Path(temporary_directory))

            result = aggregator.search(group="session", limit=50)

        self.assertTrue(result["available"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["totals"]["records"], 4)
        self.assertEqual(result["totals"]["sessions"], 2)
        self.assertEqual(result["totals"]["total_tokens"], 12_000)
        rows = result["rows"]
        self.assertEqual(len(rows), 4)
        session_ids = {row["session_id"] for row in rows}
        self.assertEqual(
            session_ids,
            {self._PERSONAL_SESSION, self._WORK_SESSION},
        )
        work_rows = [row for row in rows if row["session_id"] == self._WORK_SESSION]
        self.assertEqual(
            sorted(row["model"] for row in work_rows),
            ["gpt-5.6-luna", "gpt-5.6-sol"],
        )
        self.assertEqual(
            sorted(row["total_tokens"] for row in work_rows),
            [4_000, 5_000],
        )
        self.assertEqual({row["project"] for row in work_rows}, {"/home/dev/beta"})
        self.assertEqual(
            [row["date"] for row in work_rows],
            [
                datetime.fromtimestamp(
                    _timestamp("2026-08-27T02:00:00Z")
                ).astimezone().strftime("%Y-%m-%d")
            ]
            * 2,
        )

    def test_filters_by_model_session_project_and_date(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            aggregator = self._aggregator(Path(temporary_directory))

            by_model = aggregator.search(models=("gpt-5.6-sol",))
            by_session = aggregator.search(session=self._WORK_SESSION[:8])
            by_project = aggregator.search(project="beta")
            by_keyword = aggregator.search(keyword="gpt-5.6-luna")
            first_day = aggregator.search(
                until=_timestamp("2026-08-26T23:59:59Z")
            )
            empty = aggregator.search(session="不存在的会话")

        self.assertEqual(by_model["totals"]["records"], 1)
        self.assertEqual(by_model["totals"]["total_tokens"], 5_000)
        self.assertEqual(by_session["totals"]["records"], 2)
        self.assertEqual(by_session["totals"]["sessions"], 1)
        self.assertEqual(by_project["totals"]["records"], 2)
        self.assertEqual(by_keyword["totals"]["records"], 3)
        self.assertEqual(first_day["totals"]["records"], 1)
        self.assertEqual(empty["matched_rows"], 0)
        self.assertEqual(empty["rows"], [])

    def test_summary_groups_and_sorting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            aggregator = self._aggregator(Path(temporary_directory))

            by_date = aggregator.search(group="date", sort="tokens")
            by_model = aggregator.search(group="model", sort="tokens")

        self.assertEqual(by_date["matched_rows"], 2)
        newest = by_date["rows"][0]
        self.assertEqual(newest["total_tokens"], 11_000)
        self.assertEqual(newest["records"], 3)
        self.assertEqual(newest["models"], ["gpt-5.6-luna", "gpt-5.6-sol"])
        self.assertEqual(by_model["matched_rows"], 2)
        self.assertEqual(by_model["rows"][0]["models"], ["gpt-5.6-luna"])
        self.assertEqual(by_model["rows"][0]["total_tokens"], 7_000)
        self.assertEqual(by_model["rows"][1]["model"], None)
        self.assertEqual(by_model["rows"][1]["models"], ["gpt-5.6-sol"])

    def test_pagination_and_facets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            aggregator = self._aggregator(Path(temporary_directory))

            first_page = aggregator.search(limit=2)
            second_page = aggregator.search(limit=2, offset=2)
            facets = aggregator.usage_facets()

        self.assertEqual(len(first_page["rows"]), 2)
        self.assertTrue(first_page["has_more"])
        self.assertEqual(first_page["matched_rows"], 4)
        self.assertFalse(second_page["has_more"])
        self.assertEqual(len(second_page["rows"]), 2)
        self.assertTrue(facets["available"])
        self.assertEqual(facets["records"], 4)
        self.assertEqual(facets["sessions"], 2)
        self.assertEqual(facets["models"], ["gpt-5.6-luna", "gpt-5.6-sol"])
        self.assertEqual(
            facets["first_at"],
            _timestamp("2026-08-26T01:00:00Z"),
        )

    def test_rejects_invalid_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            aggregator = self._aggregator(Path(temporary_directory))

            for kwargs in (
                {"group": "project"},
                {"sort": "random"},
                {"limit": 0},
                {"limit": 10_000},
                {"offset": -1},
            ):
                with self.assertRaises(ValueError):
                    aggregator.search(**kwargs)

    def test_empty_index_reports_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            aggregator = UsageAggregator(
                cache_path=Path(temporary_directory) / "state" / "usage-index.sqlite3"
            )

            result = aggregator.search()
            facets = aggregator.usage_facets()
            empty = UsageAggregator.empty_search()

        self.assertTrue(result["available"])
        self.assertEqual(result["matched_rows"], 0)
        self.assertFalse(facets["available"])
        self.assertEqual(facets["models"], [])
        self.assertEqual(facets["accounts"], [])
        self.assertFalse(empty["available"])
        self.assertEqual(empty["rows"], [])


class UsageAccountGroupingTests(unittest.TestCase):
    """验证用量检索按账号聚合、账号筛选和产品标签。"""

    _PERSONAL_SESSION = "33333333-3333-4333-8333-333333333333"
    _WORK_SESSION = "44444444-4444-4444-8444-444444444444"

    def _aggregator(self, root: Path) -> UsageAggregator:
        """建立两个账号、两个会话、两个模型的用量索引。"""

        personal_home = root / ".codex-personal"
        work_home = root / ".codex-work"
        _write_search_session(
            personal_home,
            self._PERSONAL_SESSION,
            "/home/dev/alpha",
            (
                ("2026-08-27T01:00:00Z", "gpt-5.6-luna", 1_000),
                ("2026-08-27T02:00:00Z", "gpt-5.6-luna", 3_000),
            ),
        )
        _write_search_session(
            work_home,
            self._WORK_SESSION,
            "/home/dev/beta",
            (("2026-08-27T03:00:00Z", "gpt-5.6-sol", 5_000),),
        )
        aggregator = UsageAggregator(
            discovery_interval=0.01,
            refresh_interval=0.01,
            cache_path=root / "state" / "usage-index.sqlite3",
        )
        aggregator.snapshot(
            {
                "personal": MultiSessionRegistry(root / "state" / "personal"),
                "work": MultiSessionRegistry(root / "state" / "work"),
            },
            account_metadata={
                "personal": {
                    "account_id": "account-personal",
                    "profile_name": "personal",
                    "codex_home": str(personal_home),
                },
                "work": {
                    "account_id": "account-work",
                    "profile_name": "work",
                    "codex_home": str(work_home),
                },
            },
            now=_timestamp("2026-08-27T12:00:00Z"),
        )
        return aggregator

    def test_period_accounts_carry_products(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            aggregator = self._aggregator(Path(temporary_directory))

            today = _period(aggregator.cached_snapshot(), "today")
            accounts = {item["account"]: item for item in today["accounts"]}

        self.assertEqual(set(accounts), {"account-personal", "account-work"})
        for account in accounts.values():
            self.assertEqual(account["products"], ["Codex CLI"])
        # 累计快照产生的增量：personal 1k + 2k，work 5k。
        self.assertEqual(accounts["account-personal"]["total_tokens"], 3_000)
        self.assertEqual(accounts["account-work"]["total_tokens"], 5_000)

    def test_groups_by_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            aggregator = self._aggregator(Path(temporary_directory))

            result = aggregator.search(group="account", sort="tokens")
            sessions = aggregator.search(group="session")

        self.assertTrue(result["available"])
        self.assertEqual(result["group"], "account")
        self.assertEqual(result["matched_rows"], 2)
        rows = {row["account"]: row for row in result["rows"]}
        self.assertEqual(set(rows), {"account-personal", "account-work"})
        self.assertEqual(rows["account-personal"]["total_tokens"], 3_000)
        self.assertEqual(rows["account-personal"]["account_id"], "account-personal")
        self.assertEqual(rows["account-personal"]["account_key"], "account-personal")
        self.assertEqual(rows["account-work"]["total_tokens"], 5_000)
        self.assertEqual(rows["account-personal"]["products"], ["Codex CLI"])
        self.assertEqual(rows["account-personal"]["models"], ["gpt-5.6-luna"])
        self.assertEqual(result["totals"]["sessions"], 2)
        # 会话明细同样带账号标签，方便判断某次对话消耗了哪个账号。
        by_session = {row["session_id"]: row for row in sessions["rows"]}
        self.assertEqual(
            by_session[self._PERSONAL_SESSION]["account"],
            "account-personal",
        )
        self.assertEqual(by_session[self._WORK_SESSION]["account"], "account-work")

    def test_filters_by_account_key_name_and_product(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            aggregator = self._aggregator(Path(temporary_directory))

            by_id = aggregator.search(account="account-work", group="account")
            by_profile = aggregator.search(account="personal", group="account")
            by_product = aggregator.search(account="codex", group="account")
            missing = aggregator.search(account="不存在的账号", group="account")

        self.assertEqual(by_id["matched_rows"], 1)
        self.assertEqual(by_id["totals"]["total_tokens"], 5_000)
        self.assertEqual(by_profile["matched_rows"], 1)
        self.assertEqual(by_profile["totals"]["total_tokens"], 3_000)
        self.assertEqual(by_product["totals"]["total_tokens"], 8_000)
        self.assertEqual(missing["matched_rows"], 0)
        self.assertEqual(missing["rows"], [])

    def test_scanned_path_groups_by_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            aggregator = self._aggregator(Path(temporary_directory))
            store = aggregator._persistent
            assert store is not None

            grouped = aggregator.search(group="account", sort="tokens")
            scanned = aggregator._search_scan(store, group="account", sort="tokens")

        self.assertEqual(
            [row["account"] for row in grouped["rows"]],
            [row["account"] for row in scanned["rows"]],
        )
        self.assertEqual(
            [row["total_tokens"] for row in grouped["rows"]],
            [row["total_tokens"] for row in scanned["rows"]],
        )
        self.assertEqual(
            scanned["rows"][0]["products"],
            ["Codex CLI"],
        )

    def test_facets_list_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            aggregator = self._aggregator(Path(temporary_directory))

            facets = aggregator.usage_facets()

        self.assertEqual(
            facets["accounts"],
            ["account-personal", "account-work"],
        )

    def test_unknown_account_rows_are_kept_grouped(self) -> None:
        """索引里还没有账号信息的旧文件按未知账号归组，不能丢数据。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            aggregator = self._aggregator(root)
            store = aggregator._persistent
            assert store is not None
            index = root / "state" / "usage-index.sqlite3"
            connection = sqlite3.connect(index)
            connection.execute("DELETE FROM usage_file_account")
            connection.commit()
            connection.close()

            result = aggregator.search(group="account", sort="tokens")

        self.assertEqual(result["matched_rows"], 1)
        self.assertEqual(result["rows"][0]["account"], "未知账号")
        self.assertEqual(result["totals"]["total_tokens"], 8_000)


class SessionUsageTests(unittest.TestCase):
    """验证会话轮数、上下文汇总和切换新会话提醒。"""

    _SESSION = "55555555-5555-4555-8555-555555555555"

    def _aggregator(self, root: Path) -> tuple[UsageAggregator, Path]:
        """建立一个两轮、上下文 300k 的会话索引。"""

        home = root / ".codex"
        path = (
            home
            / "sessions"
            / "2026"
            / "08"
            / "27"
            / f"rollout-2026-08-27T01-00-00-{self._SESSION}.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(
                json.dumps(event)
                for event in (
                    {
                        "timestamp": "2026-08-27T01:00:00Z",
                        "type": "session_meta",
                        "payload": {"cwd": "/home/dev/theta"},
                    },
                    {
                        "timestamp": "2026-08-27T02:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "thread_settings": {"model": "gpt-5.6-luna"},
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 1_000_000,
                                    "total_tokens": 1_000_000,
                                }
                            },
                        },
                    },
                    {
                        "timestamp": "2026-08-27T03:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "thread_settings": {"model": "gpt-5.6-luna"},
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 1_300_000,
                                    "total_tokens": 1_300_000,
                                }
                            },
                        },
                    },
                )
            )
            + "\n",
            encoding="utf-8",
        )
        aggregator = UsageAggregator(
            discovery_interval=0.01,
            refresh_interval=0.01,
            cache_path=root / "state" / "usage-index.sqlite3",
        )
        aggregator.snapshot(
            {"codex": MultiSessionRegistry(root / "state")},
            account_metadata={
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(home),
                }
            },
            now=_timestamp("2026-08-27T12:00:00Z"),
        )
        return aggregator, path

    def test_session_usages_reports_turns_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            aggregator, path = self._aggregator(Path(temporary_directory))

            usages = aggregator.session_usages([str(path)])
            missing = aggregator.session_usages([str(path.parent / "other.jsonl")])
            empty = aggregator.session_usages([])

        usage = usages[str(path)]
        self.assertEqual(usage.turns, 2)
        self.assertEqual(usage.context_tokens, 300_000)
        self.assertEqual(usage.total_tokens, 1_300_000)
        self.assertEqual(usage.model, "gpt-5.6-luna")
        self.assertEqual(usage.session_id, self._SESSION)
        self.assertIsNotNone(usage.estimated_cost_usd)
        payload = usage.to_dict()
        self.assertEqual(payload["turns"], 2)
        self.assertEqual(payload["context_tokens"], 300_000)
        self.assertEqual(missing, {})
        self.assertEqual(empty, {})

    def test_reminder_triggers_on_turns_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            aggregator, path = self._aggregator(Path(temporary_directory))

            usage = aggregator.session_usages([str(path)])[str(path)]
            default = usage.reminder(SessionSwitchThresholds())
            by_turns = usage.reminder(
                SessionSwitchThresholds(turn_warn=2, context_warn_tokens=10**9)
            )
            quiet = usage.reminder(
                SessionSwitchThresholds(turn_warn=99, context_warn_tokens=10**9)
            )

        self.assertIsNotNone(default)
        self.assertEqual(default["kind"], "session")
        self.assertIn("300,000", default["message"])
        self.assertEqual(by_turns["reasons"], ["已进行 2 轮"])
        self.assertIsNone(quiet)

    def test_thresholds_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            SessionSwitchThresholds(turn_warn=0)
        with self.assertRaises(ValueError):
            SessionSwitchThresholds(context_warn_tokens=0)
        self.assertEqual(
            SessionSwitchThresholds(turn_warn=5, context_warn_tokens=10).to_dict(),
            {"turn_warn": 5, "context_warn_tokens": 10},
        )


class UsageSearchAggregationTests(unittest.TestCase):
    """验证 SQL 聚合路径与逐条扫描路径结果一致。"""

    def _aggregator(self, root: Path) -> UsageAggregator:
        """建立一个含长/短上下文与已定价/未定价模型的索引。"""

        home = root / ".codex"
        session = (
            home
            / "sessions"
            / "2026"
            / "08"
            / "27"
            / "rollout-2026-08-27T01-00-00-99999999-9999-4999-8999-999999999999.jsonl"
        )
        session.parent.mkdir(parents=True, exist_ok=True)
        events = [
            {
                "timestamp": "2026-08-27T01:00:00Z",
                "type": "session_meta",
                "payload": {"cwd": "/home/dev/omega"},
            },
            # 短上下文：gpt-5.6-luna（阈值取全局 272k）
            self._usage_event("2026-08-27T02:00:00Z", "gpt-5.6-luna", 100_000, 150_000),
            # 长上下文：单条就超过模型阈值
            self._usage_event("2026-08-27T03:00:00Z", "gpt-5.6-luna", 700_000, 750_000),
            # 未定价模型
            self._usage_event("2026-08-27T04:00:00Z", "codex-auto-review", 800_000, 810_000),
        ]
        session.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
        aggregator = UsageAggregator(
            discovery_interval=0.01,
            refresh_interval=0.01,
            cache_path=root / "state" / "usage-index.sqlite3",
        )
        aggregator.snapshot(
            {"codex": MultiSessionRegistry(root / "state")},
            account_metadata={
                "codex": {
                    "account_id": "account-personal",
                    "profile_name": "codex",
                    "codex_home": str(home),
                }
            },
            now=_timestamp("2026-08-27T12:00:00Z"),
        )
        return aggregator

    @staticmethod
    def _usage_event(
        when: str,
        model: str,
        total: int,
        cached: int,
    ) -> dict[str, object]:
        return {
            "timestamp": when,
            "type": "event_msg",
            "payload": {
                "thread_settings": {"model": model},
                "info": {
                    "total_token_usage": {
                        "input_tokens": total,
                        "cached_input_tokens": cached,
                        "output_tokens": 1_000,
                        "total_tokens": total,
                    }
                },
            },
        }

    def test_grouped_and_scanned_paths_agree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            aggregator = self._aggregator(Path(temporary_directory))
            store = aggregator._persistent
            assert store is not None

            self.assertTrue(store.supports_json_aggregation())
            self.assertFalse(store.has_pending_long_context())
            grouped = aggregator.search(group="session")
            scanned = aggregator._search_scan(store, group="session")
            by_model_grouped = aggregator.search(group="model", sort="tokens")
            by_model_scanned = aggregator._search_scan(
                store,
                group="model",
                sort="tokens",
            )

        self.assertEqual(
            grouped["totals"]["usage"],
            scanned["totals"]["usage"],
        )
        self.assertEqual(
            grouped["totals"]["total_tokens"],
            scanned["totals"]["total_tokens"],
        )
        self.assertEqual(
            grouped["totals"]["records"],
            scanned["totals"]["records"],
        )
        self.assertEqual(
            [row["total_tokens"] for row in grouped["rows"]],
            [row["total_tokens"] for row in scanned["rows"]],
        )
        self.assertEqual(
            [row["estimated_cost_usd"] for row in grouped["rows"]],
            [row["estimated_cost_usd"] for row in scanned["rows"]],
        )
        self.assertEqual(
            [row["estimated_cost_usd"] for row in by_model_grouped["rows"]],
            [row["estimated_cost_usd"] for row in by_model_scanned["rows"]],
        )
        # 长上下文分桶必须保留：luna 的两条记录一条短路一条长路
        luna = next(
            row for row in by_model_grouped["rows"] if row["models"] == ["gpt-5.6-luna"]
        )
        self.assertIsNotNone(luna["estimated_cost_usd"])

    def test_search_results_are_cached_and_expire(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            aggregator = self._aggregator(Path(temporary_directory))

            first = aggregator.search(group="session")
            second = aggregator.search(group="session")
            aggregator._search_cache.clear()
            third = aggregator.search(group="session")

        self.assertIs(first, second)
        self.assertIsNot(first, third)
        self.assertEqual(first["totals"], third["totals"])

    def test_derived_since_is_aligned_for_cache_hits(self) -> None:
        """同一「近 N 天」筛选取到的起始时间必须一致，否则缓存永不命中。"""

        first = search_since_days(30, now=1_700_000_010.0)
        second = search_since_days(30, now=1_700_000_020.0)
        third = search_since_days(30, now=1_700_000_040.0)

        self.assertEqual(first, second)
        self.assertNotEqual(second, third)
        self.assertEqual(third - first, 60.0)

    def test_legacy_rows_are_backfilled_with_long_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            aggregator = self._aggregator(root)
            store = aggregator._persistent
            assert store is not None
            index = root / "state" / "usage-index.sqlite3"

            # 模拟旧索引：把标记清空后重新打开，应当自动回填。
            connection = sqlite3.connect(index)
            connection.execute("UPDATE usage_delta SET long_context = NULL")
            connection.commit()
            connection.close()
            reopened = _UsageIndexStore(index)
            pending = reopened.has_pending_long_context()
            flags = sqlite3.connect(index).execute(
                "SELECT long_context FROM usage_delta ORDER BY rowid"
            ).fetchall()

        self.assertFalse(pending)
        # 第一条 100k（短）、第二条增量 600k（长）、第三条未定价模型（按短路处理）
        self.assertEqual([row[0] for row in flags], [0, 1, 0])


class UpdateHomesTests(unittest.TestCase):
    """验证 UsageAggregator.update_homes 的热更新语义。"""

    def test_swapped_grok_homes_are_indexed_next_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            grok_a = root / "grok-a"
            grok_b = root / "grok-b"
            for home in (grok_a, grok_b):
                (home / "logs").mkdir(parents=True)
                (home / "logs" / "unified.jsonl").write_text("", encoding="utf-8")
            aggregator = UsageAggregator(grok_homes=(grok_a,))

            aggregator.update_homes(grok_homes=(grok_b,))
            sources = aggregator._build_sources({}, {})

        self.assertNotIn(grok_a / "logs" / "unified.jsonl", sources)
        self.assertIn(grok_b / "logs" / "unified.jsonl", sources)
        self.assertEqual(aggregator._grok_homes, (grok_b,))

    def test_empty_tuple_disables_and_none_keeps_current(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            grok_home = root / "grok"
            aggregator = UsageAggregator(grok_homes=(grok_home,))

            aggregator.update_homes()
            unchanged = aggregator._grok_homes
            aggregator.update_homes(grok_homes=())
            disabled = aggregator._grok_homes
            sources = aggregator._build_sources({}, {})

        self.assertEqual(unchanged, (grok_home,))
        self.assertEqual(disabled, ())
        self.assertEqual(sources, {})

    def test_removed_home_drops_session_index_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            grok_home = root / "grok"
            aggregator = UsageAggregator(grok_homes=(grok_home,))
            aggregator._grok_sessions[grok_home] = {}
            aggregator._grok_sessions_at[grok_home] = 1.0

            aggregator.update_homes(grok_homes=())

        self.assertEqual(aggregator._grok_sessions, {})
        self.assertEqual(aggregator._grok_sessions_at, {})


class IndexHealthTests(unittest.TestCase):
    """验证 UsageAggregator 的索引进度与错误健康摘要。"""

    @staticmethod
    def _aggregator() -> UsageAggregator:
        """构造不触碰真实用户目录的聚合器。"""

        return UsageAggregator(
            grok_homes=(),
            kimi_homes=(),
            dsh_homes=(),
            claude_homes=(),
        )

    def test_success_marks_last_indexed_at(self) -> None:
        aggregator = self._aggregator()
        before = aggregator.index_health()

        aggregator.refresh_index({}, {})
        after = aggregator.index_health()

        self.assertIsNone(before["last_indexed_at"])
        self.assertIsNotNone(after["last_indexed_at"])
        self.assertIsNone(after["last_error"])
        # 未启用后台索引时 worker_alive 为 None。
        self.assertIsNone(after["worker_alive"])

    def test_failure_records_sanitized_error(self) -> None:
        aggregator = self._aggregator()
        with patch.object(
            aggregator,
            "_index_once",
            side_effect=OSError("磁盘炸了"),
        ):
            with self.assertRaises(OSError):
                aggregator.refresh_index({}, {})
            health = aggregator.index_health()

        self.assertIn("磁盘炸了", health["last_error"])
        self.assertIsNone(health["last_indexed_at"])

        # 下一轮成功后错误被清除。
        aggregator.refresh_index({}, {})
        recovered = aggregator.index_health()
        self.assertIsNone(recovered["last_error"])
        self.assertIsNotNone(recovered["last_indexed_at"])


class IndexRetentionTests(unittest.TestCase):
    """验证 _UsageIndexStore 的历史清理接口只动 usage_delta 明细。"""

    @staticmethod
    def _seed_store(root: Path) -> "_UsageIndexStore":
        """构造带一条文件检查点和三条增量的索引库。"""

        store = _UsageIndexStore(root / "usage-index.sqlite3")
        connection = sqlite3.connect(store.path)
        connection.execute(
            "INSERT INTO usage_file_state (path, inode, mtime_ns, file_size, "
            "next_offset, complete, state_json) "
            "VALUES ('/a.jsonl', 1, 1, 1, 1, 1, '{}')"
        )
        for timestamp in (10.0, 50.0, 100.0):
            connection.execute(
                "INSERT INTO usage_delta (path, kind, timestamp, model, usage_json) "
                "VALUES ('/a.jsonl', 'codex', ?, 'gpt-5', '{}')",
                (timestamp,),
            )
        connection.commit()
        connection.close()
        return store

    def test_count_and_delete_deltas_before_respects_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = self._seed_store(Path(temporary_directory))

            total_before = store.count_deltas()
            expired = store.count_deltas_before(50.0)
            deleted = store.delete_deltas_before(50.0)
            total_after = store.count_deltas()
            state_rows = sqlite3.connect(store.path).execute(
                "SELECT COUNT(*) FROM usage_file_state"
            ).fetchone()[0]
            store.close()

        self.assertEqual(total_before, 3)
        # 恰好等于 cutoff 的行不删,只删严格更早的 ts=10。
        self.assertEqual(expired, 1)
        self.assertEqual(deleted, 1)
        self.assertEqual(total_after, 2)
        # 增量读取检查点绝不能被动,否则文件会被全量重读。
        self.assertEqual(state_rows, 1)

    def test_vacuum_keeps_database_usable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = self._seed_store(Path(temporary_directory))

            store.delete_deltas_before(1000.0)
            store.vacuum()
            remaining = store.count_deltas()
            store.close()
            reopened = _UsageIndexStore(Path(temporary_directory) / "usage-index.sqlite3")
            remaining_after_reopen = reopened.count_deltas()

        self.assertEqual(remaining, 0)
        self.assertEqual(remaining_after_reopen, 0)


if __name__ == "__main__":
    unittest.main()
