"""多语言支持的核心逻辑测试：语言判断、翻译、负载本地化与清单扫描。"""

from __future__ import annotations

import unittest

from a_token_monitor import i18n


class LanguageResolutionTests(unittest.TestCase):
    """浏览器/系统语言怎么写都要能归一到 zh 或 en。"""

    def test_normalizes_common_forms(self) -> None:
        cases = {
            "zh": "zh",
            "zh-CN": "zh",
            "zh_TW": "zh",
            "zh-Hans-CN": "zh",
            "en": "en",
            "en-US": "en",
            "EN_us": "en",
            "zh-CN,zh;q=0.9,en;q=0.8": "zh",
            "fr-FR,en;q=0.7": "en",
            "": None,
            None: None,
            "de-DE": None,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(i18n.normalize_language(raw), expected)

    def test_resolution_precedence(self) -> None:
        # 手动选择优先于浏览器语言，浏览器语言优先于环境变量，都没有则中文。
        self.assertEqual(
            i18n.resolve_language(accept_language="en-US", override="zh"),
            "zh",
        )
        self.assertEqual(i18n.resolve_language(accept_language="en-US"), "en")
        self.assertEqual(i18n.resolve_language(env={"LANG": "en_US.UTF-8"}), "en")
        self.assertEqual(
            i18n.resolve_language(
                accept_language="de-DE",
                env={"LANG": "zh_CN.UTF-8"},
            ),
            "zh",
        )
        self.assertEqual(i18n.resolve_language(), i18n.DEFAULT_LANGUAGE)

    def test_env_prefers_lc_all(self) -> None:
        env = {"LC_ALL": "en_GB.UTF-8", "LANG": "zh_CN.UTF-8"}
        self.assertEqual(i18n.language_from_env(env), "en")
        self.assertEqual(i18n.language_from_env({"LANG": "zh_CN.UTF-8"}), "zh")
        self.assertIsNone(i18n.language_from_env({}))


class TranslationTests(unittest.TestCase):
    """翻译只动英文，中文与未知文案原样返回。"""

    def test_translate_keeps_source_when_missing(self) -> None:
        self.assertEqual(i18n.translate("没有条目的文案", "en"), "没有条目的文案")
        self.assertEqual(i18n.translate("工作台", "zh"), "工作台")

    def test_substitute_replaces_longest_first(self) -> None:
        with patch_catalog({"额度": "Quota", "额度窗口": "Quota windows"}):
            self.assertEqual(
                i18n.substitute("额度窗口与额度", "en"),
                "Quota windows与Quota",
            )
        # 中文与英文以外的语言不做替换。
        self.assertEqual(i18n.substitute("额度窗口", "zh"), "额度窗口")

    def test_localize_payload_translates_values_only(self) -> None:
        with patch_catalog({"今天": "Today", "近 7 天": "Last 7 days"}):
            payload = {
                "label": "今天",
                "periods": [{"label": "近 7 天", "count": 3}],
                "nested": {"ok": True},
            }
            localized = i18n.localize_payload(payload, "en")
            self.assertEqual(localized["label"], "Today")
            self.assertEqual(localized["periods"][0]["label"], "Last 7 days")
            self.assertEqual(localized["periods"][0]["count"], 3)
            self.assertIs(localized["nested"], payload["nested"])
            # 中文请求不做任何改动。
            self.assertIs(i18n.localize_payload(payload, "zh"), payload)


class SourceStringScanTests(unittest.TestCase):
    """清单扫描要跳过注释、只留可翻译的中文串。"""

    def test_skips_comments_and_deduplicates(self) -> None:
        template = (
            "/* 中文注释：这段不该进清单 */\n"
            "// 中文行注释也不该进清单\n"
            "<h2>用量与成本估算</h2>\n"
            "const label = '总览';\n"
            "const again = '总览';\n"
            "const template = `另有 ${count} 个模型`;\n"
        )
        found = list(i18n.iter_source_strings(template))
        self.assertIn("用量与成本估算", found)
        self.assertIn("总览", found)
        self.assertIn("另有 ${count} 个模型", found)
        self.assertEqual(found.count("总览"), 1)
        for comment in ("这段不该进清单", "中文行注释也不该进清单"):
            self.assertNotIn(comment, found)

    def test_contains_cjk_matches_chinese_only(self) -> None:
        self.assertTrue(i18n.contains_cjk("总览"))
        self.assertTrue(i18n.contains_cjk("额 度"))
        self.assertFalse(i18n.contains_cjk("Dashboard"))
        self.assertFalse(i18n.contains_cjk("Usage & cost estimation"))

    def test_missing_entries_reports_untranslated(self) -> None:
        missing = i18n.missing_entries("<h2>用量与成本估算</h2>")
        self.assertEqual(missing, ("用量与成本估算",))
        self.assertEqual(i18n.missing_entries("<h2>Dashboard</h2>"), ())


class EnglishCompletenessTests(unittest.TestCase):
    """英文目录补齐后才启用：英文页面里不允许出现中文。"""

    @unittest.skipUnless(i18n.EN_COMPLETE, "英文目录仍在补齐中")
    def test_english_pages_have_no_cjk(self) -> None:
        from a_token_monitor.dashboard import _DASHBOARD_HTML, _SETTINGS_HTML

        for name, page in (("dashboard", _DASHBOARD_HTML), ("settings", _SETTINGS_HTML)):
            with self.subTest(page=name):
                english = i18n.substitute(page, "en")
                missing = i18n.missing_entries(english)
                self.assertEqual(missing, (), f"{name} 还有未翻译文案: {missing[:10]}")
                self.assertFalse(i18n.contains_cjk(english.split("<style>")[0]))


def patch_catalog(entries: dict[str, str]):
    """临时把条目塞进目录表，测完还原。"""

    class _Patcher:
        def __enter__(self):
            self.added = entries
            i18n._EN.update(entries)
            return i18n

        def __exit__(self, *exc_info):
            for key in self.added:
                i18n._EN.pop(key, None)
            return False

    return _Patcher()


if __name__ == "__main__":
    unittest.main()
