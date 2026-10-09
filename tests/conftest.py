"""pytest 全局配置：把测试进程的本地时区固定为 UTC+8。

用量统计的「今天」按本地日历日切分（``usage._period_start``），而用例里的会话时间戳
和 ``now`` 都是写死的 UTC 时间，是按 UTC+8 设计的：例如 ``2026-08-26T17:00Z`` 在
UTC+8 下属于 8 月 27 日。机器在别的时区（比如美西）运行时，这些记录会落到前一天，
「今天」的用量就变成 0。这里在任何用例导入前固定时区，让结果不随开发机变化。

``time.tzset`` 只在 POSIX（Linux / macOS）上存在；Windows 无法这样改时区，
CI 的 Windows runner 固定为 UTC，所以用例数据同时保证在 UTC 下成立。
"""

from __future__ import annotations

import os
import time

# 中文注释：用 POSIX TZ 字符串而不是 "Asia/Shanghai"，不依赖系统 zoneinfo 数据。
_TEST_TIMEZONE = "CST-8"

if hasattr(time, "tzset"):
    os.environ["TZ"] = _TEST_TIMEZONE
    time.tzset()
