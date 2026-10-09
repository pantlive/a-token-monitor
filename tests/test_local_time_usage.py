"""守护：本地时区只能经 ``a_token_monitor.local_time`` 读取。

用量的「今天」、按天汇总、日期筛选、文件名里的本地时间都依赖本地时区。直接调用
``datetime.fromtimestamp(ts)``、``.astimezone()``、``time.localtime()`` 等会绕开
``local_time`` 的时区注入，测试结果随开发机时区变化，夏令时切换日还会算偏日界线。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "a_token_monitor"
ALLOWED = {PACKAGE / "local_time.py"}


def _system_local_time_calls(tree: ast.AST) -> list[tuple[int, str]]:
    """返回直接读取系统本地时区的调用（行号, 描述）。"""

    found: list[tuple[int, str]] = []
    # 中文注释：文档字符串里可以解释为什么不用 'localtime'，只检查参与运算的字符串。
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            if "'localtime'" in node.value:
                found.append((node.lineno, "SQLite 'localtime' 修饰符"))
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        name = node.func.attr
        owner = node.func.value
        owner_name = owner.id if isinstance(owner, ast.Name) else None
        keywords = {keyword.arg for keyword in node.keywords}
        if name == "fromtimestamp" and len(node.args) < 2 and "tz" not in keywords:
            found.append((node.lineno, "datetime.fromtimestamp() 不带 tz"))
        elif name == "astimezone" and not node.args and not node.keywords:
            found.append((node.lineno, ".astimezone() 不带时区"))
        elif owner_name == "time" and name in {"localtime", "mktime", "strftime", "ctime"}:
            found.append((node.lineno, f"time.{name}()"))
        elif owner_name == "datetime" and name == "now" and not node.args and not node.keywords:
            found.append((node.lineno, "datetime.now() 不带 tz"))
        elif owner_name in {"date", "datetime"} and name == "today":
            found.append((node.lineno, f"{owner_name}.today()"))
    return found


class LocalTimeUsageTests(unittest.TestCase):
    def test_no_module_reads_the_system_timezone_directly(self) -> None:
        offenders = []
        for path in sorted(PACKAGE.rglob("*.py")):
            if path in ALLOWED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for lineno, what in _system_local_time_calls(tree):
                offenders.append(f"{path.relative_to(PACKAGE)}:{lineno} {what}")
        self.assertEqual(
            offenders,
            [],
            "请改用 a_token_monitor.local_time（to_local / local_day_key / "
            "local_naive_to_timestamp 等）",
        )


if __name__ == "__main__":
    unittest.main()
