"""中英双份 README 的一致性守护。

中文 ``README.md`` 与英文 ``README.en.md`` 必须同时存在、互相链接，并且结构一一对应：
章节数量、代码块数量一致，命令行示例完全相同，英文版除语言切换链接外不出现中文。
任何一边新增章节、改动命令却忘记同步另一边时，这里会直接失败。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHINESE_README = ROOT / "README.md"
ENGLISH_README = ROOT / "README.en.md"

# 英文版里唯一允许出现的中文：语言切换链接的标签。
ENGLISH_ALLOWED_CJK = ("中文",)


def _heading_count(text: str, level: int) -> int:
    """统计指定级别的 Markdown 标题数量（忽略代码块内的 ``#`` 注释）。"""

    lines: list[str] = []
    inside_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside_fence = not inside_fence
            continue
        if not inside_fence:
            lines.append(line)
    return len(re.findall(rf"^{'#' * level} \S", "\n".join(lines), re.MULTILINE))


def _command_blocks(text: str) -> list[str]:
    """提取含 ``a-token-monitor`` 的代码块，去掉注释并把占位符归一化后比较。

    注释是给人看的说明，两版各自用母语书写；命令本身必须逐字一致，所以这里只比较
    去掉注释、统一占位符写法之后的命令文本（多行续行也一起比较）。
    """

    blocks: list[str] = []
    for block in re.findall(r"```(?:bash|powershell)\n(.*?)```", text, re.DOTALL):
        if "a-token-monitor" not in block:
            continue
        kept = []
        for line in block.splitlines():
            if line.lstrip().startswith("#"):
                continue
            # 行尾注释同样属于说明文字（例如 `conda env create -f ... # 说明`），
            # 只在空白之后出现 `#` 时才截断，避免误伤命令里的普通字符。
            kept.append(re.sub(r"\s+#.*$", "", line).rstrip())
        normalized = re.sub(r"<[^>\n]+>", "<placeholder>", "\n".join(kept).strip())
        blocks.append(normalized)
    return blocks


def _table_rows(text: str) -> list[list[str]]:
    """按行拆出 Markdown 表格单元格，用于对齐两版的表格规模。"""

    rows: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith("|"):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def _provider_column(text: str) -> list[str]:
    """provider 表的首列（表头为 ``Provider``，两版同名所以可以直接比对）。"""

    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("| Provider |"):
            names: list[str] = []
            for row in lines[index + 2:]:
                if not row.startswith("|"):
                    break
                names.append(row.strip().strip("|").split("|")[0].strip())
            return names
    return []


class ReadmeBilingualTest(unittest.TestCase):
    def setUp(self) -> None:
        self.chinese = CHINESE_README.read_text(encoding="utf-8")
        self.english = ENGLISH_README.read_text(encoding="utf-8")

    def test_both_readmes_exist(self):
        self.assertTrue(CHINESE_README.is_file(), "缺少中文 README.md")
        self.assertTrue(ENGLISH_README.is_file(), "缺少英文 README.en.md")

    def test_readmes_link_to_each_other(self):
        self.assertIn("[English](README.en.md)", self.chinese)
        self.assertIn("[中文](README.md)", self.english)

    def test_section_structure_matches(self):
        for level in (2, 3):
            chinese = _heading_count(self.chinese, level)
            english = _heading_count(self.english, level)
            self.assertEqual(
                chinese,
                english,
                f"第 {level} 级标题数量不一致（中文 {chinese} / 英文 {english}）："
                "两版章节结构已经漂移，请同步缺失的小节",
            )

    def test_code_fences_are_balanced_and_equal(self):
        for name, text in (("README.md", self.chinese), ("README.en.md", self.english)):
            self.assertEqual(text.count("```") % 2, 0, f"{name} 里有未闭合的代码块")
        self.assertEqual(
            self.chinese.count("```"),
            self.english.count("```"),
            "中英两版代码块数量不一致",
        )

    def test_command_examples_are_identical(self):
        self.assertEqual(
            _command_blocks(self.chinese),
            _command_blocks(self.english),
            "中英两版的命令行示例已经不一致：请让英文版逐字保留同一条命令",
        )

    def test_tables_stay_in_step(self):
        """表格最容易一边改了另一边忘记：行数要一致，provider 表首列要逐一对齐。"""

        self.assertEqual(
            len(_table_rows(self.chinese)),
            len(_table_rows(self.english)),
            "两版表格行数不一致，请同步表格内容",
        )
        providers = _provider_column(self.chinese)
        self.assertEqual(providers, _provider_column(self.english))
        self.assertEqual(
            providers,
            [
                "Codex",
                "Grok",
                "Kimi Code",
                "DeepSeek Harness",
                "Command Code",
                "Claude Code",
            ],
            "provider 表首列与支持列表不一致",
        )

    def test_english_readme_has_no_chinese_prose(self):
        stripped = self.english
        for allowed in ENGLISH_ALLOWED_CJK:
            stripped = stripped.replace(allowed, "")
        leftovers = re.findall(r"[\u4e00-\u9fff]+", stripped)
        self.assertEqual(
            leftovers[:5],
            [],
            f"英文 README 里出现了中文，请翻译或补进白名单：{leftovers[:5]}",
        )


if __name__ == "__main__":  # pragma: no cover - 便于单独运行
    unittest.main()
