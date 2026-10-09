"""测试用：临时替换 provider 注册表里某个条目的函数。

各层按 ``PROVIDER_SPECS`` 查找账号、额度、活动会话等读取函数，测试不能再
patch 模块级名字，而是替换注册表条目；同一个 provider 可以嵌套替换多个字段。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Iterator
from unittest import mock

from a_token_monitor.providers import PROVIDER_SPECS


@contextmanager
def patch_provider(key: str, field: str, **mock_kwargs: Any) -> Iterator[mock.Mock]:
    """把 ``PROVIDER_SPECS[key].<field>`` 换成 Mock（参数同 ``mock.Mock``）。"""

    fake = mock.Mock(**mock_kwargs)
    patched = replace(PROVIDER_SPECS[key], **{field: fake})
    with mock.patch.dict(PROVIDER_SPECS, {key: patched}):
        yield fake
