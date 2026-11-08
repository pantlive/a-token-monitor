"""provider 注册表的完整性检查：新增 agent 漏接某个功能时在这里直接失败。

每个在 ``PROVIDER_SPECS`` 登记的 agent 都必须接入下列功能的分派表；确实不适用的
组合写进 ``EXEMPT``，并说明原因。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from a_token_monitor import agents, alert_context, claude, commandcode, housekeeping, kimi
from a_token_monitor.cli import build_parser
from a_token_monitor.providers import (
    PROVIDER_SPECS,
    clear_quota_caches,
    home_keys,
    home_providers,
    resolve_provider_homes,
    update_provider_homes,
)
from a_token_monitor.service import ServiceConfig

# 功能 -> 不需要接入该功能的产品 ID 及原因。
EXEMPT = {
    "archive_rules": {"opencode": "会话存在单个 SQLite 库里，没有可单独归档的文件"},
    "speed_source": {
        "opencode": "会话存在单个 SQLite 库里，没有可逐行采样的 JSONL 会话文件"
    },
}


class RegistryCompletenessTests(unittest.TestCase):
    def test_every_provider_has_cli_option_help_and_markers(self) -> None:
        options = {
            option
            for action in build_parser()._actions
            for option in action.option_strings
        }
        for spec in PROVIDER_SPECS.values():
            with self.subTest(provider=spec.key):
                self.assertIn(spec.cli_option, options)
                self.assertTrue(spec.cli_help)
                self.assertTrue(spec.markers)

    def test_every_product_has_a_label_and_process_detection(self) -> None:
        detected = set(agents._AGENT_BINARIES.values())
        for spec in PROVIDER_SPECS.values():
            with self.subTest(product=spec.product_id):
                self.assertIn(spec.product_id, agents.PRODUCT_LABELS)
                self.assertIn(spec.product_id, detected)

    def test_every_product_is_wired_into_alert_context(self) -> None:
        for spec in PROVIDER_SPECS.values():
            with self.subTest(product=spec.product_id):
                self.assertIn(spec.product_id, alert_context._SOURCES)
                self.assertIn(spec.product_id, alert_context.SUPPORTED_PRODUCTS)

    def test_every_product_has_archive_rules(self) -> None:
        exempt = EXEMPT["archive_rules"]
        for spec in PROVIDER_SPECS.values():
            if spec.product_id in exempt:
                continue
            with self.subTest(product=spec.product_id):
                self.assertIn(spec.product_id, housekeeping._SESSION_SPECS)

    def test_every_product_has_a_speed_source(self) -> None:
        exempt = EXEMPT["speed_source"]
        for spec in home_providers():
            if spec.product_id in exempt:
                continue
            with self.subTest(product=spec.product_id):
                self.assertTrue(
                    spec.speed_files is not None
                    or spec.product_id in housekeeping._SESSION_SPECS
                )

    def test_commandcode_speed_source_is_registered_by_product_id(self) -> None:
        spec = PROVIDER_SPECS["commandcode"]
        self.assertNotEqual(spec.key, spec.product_id)
        self.assertEqual(spec.product_id, "command-code")
        self.assertIsNotNone(spec.speed_files)

    def test_exemptions_refer_to_registered_products(self) -> None:
        products = {spec.product_id for spec in PROVIDER_SPECS.values()}
        for feature, entries in EXEMPT.items():
            with self.subTest(feature=feature):
                self.assertLessEqual(set(entries), products)


class ProviderHomesTests(unittest.TestCase):
    def test_missing_provider_is_disabled_or_auto_detected(self) -> None:
        disabled = resolve_provider_homes(None, auto_detect=False)
        self.assertEqual(set(disabled), set(home_keys()))
        self.assertTrue(all(value == () for value in disabled.values()))

        detected = resolve_provider_homes({"grok": None}, auto_detect=True)
        self.assertEqual(
            detected["grok"], PROVIDER_SPECS["grok"].resolver(None)
        )

    def test_explicit_empty_tuple_disables_even_with_auto_detect(self) -> None:
        resolved = resolve_provider_homes({"grok": ()}, auto_detect=True)
        self.assertEqual(resolved["grok"], ())

    def test_unknown_key_is_rejected(self) -> None:
        # 产品 ID 与配置键不同的 provider 最容易写错（command-code / commandcode）。
        with self.assertRaises(ValueError):
            resolve_provider_homes({"command-code": ()}, auto_detect=False)
        with self.assertRaises(ValueError):
            update_provider_homes({}, {"codex": ()})

    def test_update_replaces_given_keys_and_keeps_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            current = resolve_provider_homes(
                {"grok": (root / "a",), "kimi": (root / "k",)},
                auto_detect=False,
            )
            updated = update_provider_homes(current, {"grok": (root / "b",), "kimi": None})

        self.assertEqual(updated["grok"], (root / "b",))
        self.assertEqual(updated["kimi"], (root / "k",))

    def test_service_json_keeps_one_field_per_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = ServiceConfig(
                state_dir=root / "state",
                codex_homes=(),
                session_root=None,
                verbose=False,
                codex_path="codex",
                scan_interval=2.0,
                reconcile_interval=30.0,
                quota_interval=300.0,
                dashboard=False,
                dashboard_host="127.0.0.1",
                dashboard_port=8765,
                provider_homes={"qwen": (root / ".qwen",)},
            )
            path = root / "service.json"
            path.write_text(json.dumps(config.to_dict()), encoding="utf-8")
            payload = json.loads(path.read_text(encoding="utf-8"))
            loaded = ServiceConfig.load(path)

        for spec in home_providers():
            with self.subTest(field=spec.homes_field):
                self.assertIn(spec.homes_field, payload)
        self.assertEqual(payload["qwen_homes"], [str(root / ".qwen")])
        self.assertEqual(loaded.provider_homes["qwen"], (root / ".qwen",))


class QuotaCacheClearTests(unittest.TestCase):
    """手动刷新时清空有网络额度缓存的 provider；重复调用也必须生效。"""

    def test_clear_quota_caches_clears_every_cached_provider(self) -> None:
        with (
            mock.patch.object(claude, "_clear_quota_cache") as clear_claude,
            mock.patch.object(commandcode, "_clear_quota_cache") as clear_commandcode,
            mock.patch.object(kimi, "_clear_quota_cache") as clear_kimi,
        ):
            clear_quota_caches()
            clear_quota_caches()

        for clear in (clear_claude, clear_commandcode, clear_kimi):
            self.assertEqual(clear.call_count, 2)


class QuotaReaderErrorTests(unittest.TestCase):
    """额度读取遇到意外错误时按无额度处理，但必须留下 warning 日志。"""

    def test_unexpected_quota_errors_are_logged_not_swallowed(self) -> None:
        cases = (
            (claude, "_fetch_claude_quota", claude.read_claude_quota),
            (kimi, "_fetch_kimi_quota", kimi.read_kimi_quota),
            (commandcode, "_fetch_commandcode_quota", commandcode.read_commandcode_quota),
        )
        for module, fetch_name, reader in cases:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as temporary:
                with (
                    mock.patch.object(module, fetch_name, side_effect=KeyError("windows")),
                    self.assertLogs(module.__name__, level="WARNING") as captured,
                ):
                    # 传入 http_get_json 会绕过模块级缓存，每次都真正调用 _fetch。
                    result = reader(Path(temporary), http_get_json=mock.Mock())
                self.assertIsNone(result)
                self.assertIn("KeyError", captured.output[0])


if __name__ == "__main__":
    unittest.main()
