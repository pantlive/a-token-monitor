"""pytest 全局配置：把被测代码的本地时区固定为 UTC+8。

用量统计的「今天」和按天汇总按本地日历日切分，而用例里的会话时间戳和 ``now``
都是写死的 UTC 时间，是按 UTC+8 设计的：例如 ``2026-08-26T17:00Z`` 在 UTC+8
下属于 8 月 27 日。机器在别的时区（比如美西）运行时，这些记录会落到前一天，
「今天」的用量就变成 0。

被测代码的本地时间都经过 ``a_token_monitor.local_time``，这里在任何用例运行前
注入固定时区。注入不依赖 ``time.tzset``，Linux、macOS、Windows 行为一致。

界面语言同理：CLI 与 Dashboard 在未显式指定时按 ``LC_ALL`` / ``LC_MESSAGES`` / ``LANG``
自动选择中文或英文，而用例按默认中文断言。CI 的 macOS / Windows runner 默认是英文
locale，这里移除这些变量，让被测代码回到默认语言；专门测语言判断的用例都显式传入 env。

临时目录用解析后的真实路径：macOS 的 ``/var/folders`` 是指向 ``/private/var`` 的符号链接，
Windows runner 的临时目录带 8.3 短文件名（``RUNNER~1``）。被测代码会把路径规范化，
用例若拿未解析的路径比较就会对不上。
"""

from __future__ import annotations

import os
import tempfile
from datetime import timedelta, timezone

from a_token_monitor.local_time import set_local_timezone

set_local_timezone(timezone(timedelta(hours=8), "UTC+8"))

for _language_variable in ("LC_ALL", "LC_MESSAGES", "LANG"):
    os.environ.pop(_language_variable, None)

tempfile.tempdir = os.path.realpath(tempfile.gettempdir())

# 中文注释：版本更新提醒在交互终端里会补一次网络检查；测试进程从不联网，
# 需要联网行为的用例（tests/test_updates.py）自己用 FakeFetcher 打桩。
os.environ["A_TOKEN_MONITOR_NO_UPDATE_CHECK"] = "1"
