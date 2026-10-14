"""数字的紧凑显示：token 用 K / M / B，其他计数不受影响。"""

from __future__ import annotations

import unittest

from a_token_monitor.formatting import format_tokens


class FormatTokensTests(unittest.TestCase):
    """压缩规则与网页端 ``formatTokens`` 保持一致。"""

    def test_thresholds(self) -> None:
        cases = {
            0: "0",
            1: "1",
            999: "999",
            1000: "1.0K",
            1234: "1.2K",
            12345: "12.3K",
            999_999: "1000.0K",
            1_000_000: "1.00M",
            1_234_567: "1.23M",
            5_864_000_000: "5.86B",
            12_345_678_901: "12.35B",
            2_500_000_000_000: "2.50T",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(format_tokens(value), expected)

    def test_non_numeric_values_pass_through(self) -> None:
        self.assertEqual(format_tokens(None), "0")
        self.assertEqual(format_tokens(""), "0")
        self.assertEqual(format_tokens("未知"), "未知")

    def test_accepts_numeric_strings_and_floats(self) -> None:
        self.assertEqual(format_tokens("1500000"), "1.50M")
        self.assertEqual(format_tokens(2_400_000.0), "2.40M")


if __name__ == "__main__":  # pragma: no cover - 便于单独运行
    unittest.main()
