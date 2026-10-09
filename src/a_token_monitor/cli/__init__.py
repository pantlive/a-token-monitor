"""命令行入口包。

子模块划分：

* ``parser``  全部命令行参数定义
* ``common``  子命令共用的账号、扫描目录、格式化与日志工具
* ``main``    入口：解析参数并分派到子命令
* 每个子命令一个模块：``status``、``quota``、``sessions``、``traffic``、
  ``alerts``、``usage``、``disk``、``daemon``、``service``

``a-token-monitor`` 控制台脚本与 ``python -m a_token_monitor`` 都指向这里的 ``main``。
"""

from __future__ import annotations

from .main import main
from .parser import build_parser

__all__ = ["build_parser", "main"]
