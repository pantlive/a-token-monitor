"""测试用跨平台能力探测：符号链接、真实 ``/proc`` 与 POSIX 权限位。

Windows 与 macOS 上并非所有 Linux 语义都可用：

* 创建符号链接在 Windows 需要开发者模式（或管理员权限），否则
  ``Path.symlink_to`` 抛 ``OSError``；合成 ``/proc`` 树的用例都依赖它。
* ``/proc`` 只有 Linux 有，默认构造的 ``TrafficMonitor`` /
  ``MultiAccountMonitor.run_once`` 会扫真实 ``/proc``。
* POSIX 权限位（``0o600`` 之类）在 Windows 上不生效，断言会失败。

因此这些用例统一用本模块的装饰器标注：能力具备时照常运行，不具备时
明确跳过而不是报错。装饰器在导入时求值，``symlinks_supported()`` 带缓存，
每个进程最多探测一次。
"""

from __future__ import annotations

import functools
import os
import shutil
import tempfile
import unittest
from pathlib import Path


@functools.lru_cache(maxsize=1)
def symlinks_supported() -> bool:
    """当前环境能否创建符号链接（结果按进程缓存）。

    在 ``tempfile.mkdtemp()`` 里实际建一次文件和目录符号链接：成功返回
    ``True``；``OSError``（Windows 未开开发者模式）或 ``NotImplementedError``
    返回 ``False``。无论结果如何都会清理临时目录。
    """

    temporary_directory = tempfile.mkdtemp(prefix="token-monitor-symlink-")
    try:
        root = Path(temporary_directory)
        target_file = root / "target.txt"
        target_file.write_text("probe", encoding="utf-8")
        target_directory = root / "target-dir"
        target_directory.mkdir()
        # 中文注释：测试里既有文件符号链接（fd/3 -> session.jsonl）也有
        # 目录符号链接（cwd -> 工作目录），两者都探测一次。
        (root / "link.txt").symlink_to(target_file)
        (root / "link-dir").symlink_to(target_directory, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    finally:
        shutil.rmtree(temporary_directory, ignore_errors=True)
    return True


def proc_supported() -> bool:
    """是否存在真实 ``/proc``（即是否 Linux）。"""

    return Path("/proc").is_dir()


# 中文注释：整类或单个用例按平台能力跳过；条件在导入时求值一次。
posix_only = unittest.skipUnless(os.name == "posix", "仅 POSIX 平台")
requires_symlinks = unittest.skipUnless(
    symlinks_supported(),
    "需要符号链接权限（Windows 需开发者模式）",
)
requires_proc = unittest.skipUnless(proc_supported(), "需要 /proc（Linux）")
requires_chmod = unittest.skipUnless(
    os.name == "posix",
    "Windows 不支持 POSIX 权限位",
)
