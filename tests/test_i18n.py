"""多语言支持的核心逻辑测试：语言判断、翻译、负载本地化与清单扫描。"""

from __future__ import annotations

import re
import unittest

from a_token_monitor import i18n
from a_token_monitor.usage import pricing_metadata


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
                i18n.substitute("额度窗口 · 额度", "en"),
                "Quota windows · Quota",
            )
        # 中文与英文以外的语言不做替换。
        self.assertEqual(i18n.substitute("额度窗口", "zh"), "额度窗口")

    def test_substitute_never_bites_into_longer_words(self) -> None:
        """中文没有词边界：短条目不能命中更长的词，否则会翻成中英混杂。"""

        # 用「甲/乙」当邻居，保证不会命中真实目录里的整句条目。
        with patch_catalog({"可用": "Available", "刷新": "Refresh"}):
            self.assertEqual(i18n.substitute("甲不可用乙", "en"), "甲不可用乙")
            self.assertEqual(i18n.substitute("甲刷新乙", "en"), "甲刷新乙")
            self.assertEqual(i18n.substitute("甲：可用", "en"), "甲：Available")
            self.assertEqual(i18n.substitute("刷新 乙", "en"), "Refresh 乙")
        with patch_catalog({"可用": "Available", "不可用": "Unavailable"}):
            # 长条目先命中，短条目就不会再咬进它里面。
            self.assertEqual(i18n.substitute("不可用", "en"), "Unavailable")

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

    def test_literal_scanner_handles_templates_and_comments(self) -> None:
        """模板字符串里的嵌套引号、嵌套模板与注释都不能把字面量切错。"""

        sample = (
            "const a = '未知';\n"
            'const b = "共 {n} 条";\n'
            "const c = `另有 ${items.filter((item) => item.name === '模型')"
            ".length} 个模型`;\n"
            "const d = `外层 ${`内层 ${x} 文本`} 结束`;\n"
            "/* 注释里的 '引号' 不算 */\n"
            '// 行注释里的 "引号" 也不算\n'
            'const e = "no chinese here";\n'
        )
        found = list(i18n.iter_string_literals(sample))
        self.assertEqual(
            found,
            [
                "未知",
                "共 {n} 条",
                "另有 ${items.filter((item) => item.name === '模型').length} 个模型",
                "外层 ${`内层 ${x} 文本`} 结束",
            ],
        )

    def test_uncovered_literals_reports_remaining(self) -> None:
        # 用虚构文案，避免目录表补齐后命中真实条目。
        source = "const a = '甲甲甲';\nconst b = '乙乙乙';\n"
        with patch_catalog({"甲甲甲": "AAA"}):
            self.assertEqual(i18n.uncovered_literals(source), ("乙乙乙",))

    def test_contains_cjk_matches_chinese_only(self) -> None:
        self.assertTrue(i18n.contains_cjk("总览"))
        self.assertTrue(i18n.contains_cjk("额 度"))
        self.assertFalse(i18n.contains_cjk("Dashboard"))
        self.assertFalse(i18n.contains_cjk("Usage & cost estimation"))

    def test_missing_entries_reports_untranslated(self) -> None:
        # 用一个不会进目录表的占位文案，避免目录补齐后这条用例失效。
        missing = i18n.missing_entries("<h2>这是一条没有条目的文案</h2>")
        self.assertEqual(missing, ("这是一条没有条目的文案",))
        self.assertEqual(i18n.missing_entries("<h2>Dashboard</h2>"), ())


class CliLocalizationTests(unittest.TestCase):
    """CLI 输出本地化：--lang / LANG 都要生效，中文默认不变。"""

    def _run(self, arguments: list[str]) -> str:
        import contextlib
        import io

        from a_token_monitor import cli as cli_module

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            try:
                cli_module.main(arguments)
            except SystemExit:
                pass
        return buffer.getvalue()

    def test_help_is_english_with_lang_flag(self) -> None:
        output = self._run(["--help", "--lang", "en"])
        self.assertNotIn("未知", output)
        leftover = [
            run for run in i18n.iter_text_runs(output) if i18n.contains_cjk(run)
        ]
        self.assertEqual(leftover, [])
        self.assertIn("Monitor quotas", output)

    def test_help_is_english_with_lang_env(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8"}, clear=False):
            output = self._run(["--help"])
        leftover = [
            run for run in i18n.iter_text_runs(output) if i18n.contains_cjk(run)
        ]
        self.assertEqual(leftover, [])

    def test_chinese_stays_default(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"LANG": "zh_CN.UTF-8"}, clear=False):
            output = self._run(["--help"])
        self.assertTrue(i18n.contains_cjk(output))

    def test_lang_works_after_subcommand(self) -> None:
        output = self._run(["status", "--help", "--lang", "en"])
        leftover = [
            run for run in i18n.iter_text_runs(output) if i18n.contains_cjk(run)
        ]
        self.assertEqual(leftover, [])


class PayloadLocalizationTests(unittest.TestCase):
    """API 负载本地化：拼出来的句子要翻到，用户数据不能被动。"""

    def test_pattern_rules_translate_composed_sentences(self) -> None:
        # 提醒消息是「标题 + ，超过 X 提醒阈值」拼出来的，模式要能套两层。
        message = "Codex (codex) 占用 9.27 GiB，超过 5.00 GiB 提醒阈值"
        self.assertEqual(
            i18n.translate(message, "en"),
            "Codex (codex) uses 9.27 GiB, above the 5.00 GiB threshold",
        )
        self.assertEqual(
            i18n.translate("会话 01a06a2f 建议切换新会话", "en"),
            "Session 01a06a2f: consider starting a new one",
        )
        self.assertEqual(
            i18n.translate(
                "DeepSeek Harness (pid 2286183) 在 15 秒内向外发送 9.14 MiB，"
                "目录 /home/lsl/tmp，主要对端 172.17.176.1:108",
                "en",
            ),
            "DeepSeek Harness (pid 2286183) sent 9.14 MiB outbound in 15s from "
            "/home/lsl/tmp to 172.17.176.1:108",
        )

    def test_payload_localization_leaves_user_data_alone(self) -> None:
        """带中文的路径、会话标题是用户数据，不能因为「看着像中文」被翻译。"""

        payload = {
            "project": "/mnt/d/data/眼底",
            "cwd": "/mnt/glass_patent/基于结构光分层神经场的瞳距瞳高测量",
            "label": "今天",
        }
        localized = i18n.localize_payload(payload, "en")
        self.assertEqual(localized["project"], "/mnt/d/data/眼底")
        self.assertEqual(
            localized["cwd"],
            "/mnt/glass_patent/基于结构光分层神经场的瞳距瞳高测量",
        )
        self.assertEqual(localized["label"], "Today")

    def test_api_shapes_localize_without_chinese(self) -> None:
        """后端真实会发出的各种句子形状，本地化后不允许再有中文。"""

        payload = {
            "usage": {
                "pricing": {"note": pricing_metadata()["note"]},
                "periods": [{"label": "今天"}, {"label": "近 7 天"}],
            },
            "accounts": [
                {
                    "quota": {
                        "windows": [
                            {"period_label": "5 小时", "duration_label": "5 小时"},
                            {"period_label": "周", "duration_label": "7 天"},
                            {"period_label": "月", "duration_label": "1 个月"},
                        ]
                    }
                }
            ],
            "sessions": [
                {
                    "last_error": "额度限制事件",
                    "archive": {"reason": "会话仍在运行"},
                    "usage": {
                        "reminder": {
                            "title": "会话 019fdb08-980b-7113-9d24-937f5c787c82 建议切换新会话",
                            "detail": (
                                "gpt-6-sol · 已进行 1425 轮。超长会话每一轮都按全量上下文"
                                "重新计费；任务做到阶段收尾后让模型总结要点，再开新会话更省 token。"
                            ),
                            "message": (
                                "会话 019fdb08-980b-7113-9d24-937f5c787c82（gpt-6-sol）"
                                "已进行 1425 轮，建议收尾并开启新会话"
                            ),
                            "reasons": ["已进行 1425 轮", "最近一次上下文 218,162 token"],
                        }
                    },
                }
            ],
            "insights": {
                "size_buckets": [{"label": "1–2 轮"}, {"label": "31 轮以上"}],
                "observations": [
                    "平均每个对话 210.5 轮、29,417,143 tokens",
                    "最活跃时段是 16:00–17:00，占全部 token 的 10.0%",
                    "gpt-5.6-sol 贡献了 46.7% 的 API 等价成本",
                    "对话最多的是项目 /home/dev/demo（38 个对话，3,490,680,833 tokens）",
                    "用量最高的一天是 2026-08-25，共 931,284,320 tokens",
                    "周末 token 占 8.8%",
                    "缓存命中已累计节省约 $16095.9137",
                ],
                "suggestions": [
                    {
                        "title": "239 个对话累计超过 20 万 token",
                        "detail": (
                            "上下文越长，每轮重计的输入越多。完成阶段性任务后让模型总结要点，"
                            "再开新会话继续，能避免旧上下文反复计费。"
                        ),
                    },
                    {"title": "缓存命中率 96.7%，前缀复用做得很好"},
                ],
            },
            "health": {
                "components": [
                    {"label": "监控主循环"},
                    {"label": "Codex 账号 codex"},
                ]
            },
            "alerts": [
                {
                    "message": (
                        "DeepSeek Harness (pid 2286183) 在 15 秒内向外发送 9.88 MiB，"
                        "目录 /home/lsl/tmp，主要对端 172.17.176.1:108"
                    )
                }
            ],
        }
        localized = i18n.localize_payload(payload, "en")
        chinese: list[str] = []

        def walk(value: object) -> None:
            if isinstance(value, str):
                if i18n.contains_cjk(value):
                    chinese.append(value)
            elif isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(localized)
        self.assertEqual(chinese, [])


class PageSubstitutionTests(unittest.TestCase):
    """替换结果的质量守卫：不能出现中英混杂，覆盖率只能前进。"""

    # 覆盖率棘轮：主页与设置页模板已经 0 残留，涨回去就直接失败。
    UNCOVERED_LIMIT = 0

    def test_substitution_never_produces_mixed_script_runs(self) -> None:
        """中文旁边紧邻英文字母就是翻坏了（例如「不Available」）。"""

        from a_token_monitor.dashboard import _DASHBOARD_HTML, _SETTINGS_HTML

        mixed = re.compile(r"[\u4e00-\u9fff][A-Za-z]|[A-Za-z][\u4e00-\u9fff]")
        for name, page in (("dashboard", _DASHBOARD_HTML), ("settings", _SETTINGS_HTML)):
            english = i18n._strip_comments(i18n.substitute(page, "en"))
            broken = [
                run for run in i18n.iter_text_runs(english) if mixed.search(run)
            ]
            with self.subTest(page=name):
                self.assertEqual(broken[:5], [])

    def test_english_coverage_only_improves(self) -> None:
        """英文覆盖率棘轮：未翻译片段数不能超过上一轮记录。"""

        from a_token_monitor.dashboard import _DASHBOARD_HTML, _SETTINGS_HTML

        pages = _DASHBOARD_HTML + _SETTINGS_HTML
        english = i18n._strip_comments(i18n.substitute(pages, "en"))
        remaining = len(list(i18n.iter_text_runs(english)))
        self.assertLessEqual(
            remaining,
            self.UNCOVERED_LIMIT,
            f"模板里又出现 {remaining} 条未翻译片段，请补目录表（或确认是否新增了文案）",
        )


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


_MISSING = object()


def patch_catalog(entries: dict[str, str]):
    """临时把条目塞进目录表，测完精确还原（含被覆盖的原有条目）。"""

    class _Patcher:
        def __enter__(self):
            # 覆盖式打补丁必须记住原值：直接 pop 会把真实条目也删掉，
            # 后面的覆盖率断言就会莫名其妙地失败。
            self.previous = {key: i18n._EN.get(key, _MISSING) for key in entries}
            i18n._EN.update(entries)
            return i18n

        def __exit__(self, *exc_info):
            for key, value in self.previous.items():
                if value is _MISSING:
                    i18n._EN.pop(key, None)
                else:
                    i18n._EN[key] = value
            return False

    return _Patcher()


if __name__ == "__main__":
    unittest.main()
