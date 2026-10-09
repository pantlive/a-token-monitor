"""pytest 全局配置：把被测代码的本地时区固定为 UTC+8。

用量统计的「今天」和按天汇总按本地日历日切分，而用例里的会话时间戳和 ``now``
都是写死的 UTC 时间，是按 UTC+8 设计的：例如 ``2026-08-26T17:00Z`` 在 UTC+8
下属于 8 月 27 日。机器在别的时区（比如美西）运行时，这些记录会落到前一天，
「今天」的用量就变成 0。

被测代码的本地时间都经过 ``a_token_monitor.local_time``，这里在任何用例运行前
注入固定时区。注入不依赖 ``time.tzset``，Linux、macOS、Windows 行为一致。
"""

from __future__ import annotations

from datetime import timedelta, timezone

from a_token_monitor.local_time import set_local_timezone

set_local_timezone(timezone(timedelta(hours=8), "UTC+8"))
