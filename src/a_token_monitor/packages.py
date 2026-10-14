"""agent 自更新留下的多版本副本：只统计（并在显式开启时清理）声明过的 provider 目录。

范围严格限制在 ``AuditTarget.path`` 之内，不碰全局包管理器缓存（pip / npm / uv…）。
「哪一份还在用」由三类标记决定，任一命中就绝不清理：

1. **指向在用版本的符号链接**：``packages/<变体>/current``、``bin/grok``；
2. **version 标记文件**：``packages/<变体>/auto-update-version`` 里写的版本名；
3. **同一组里版本号最高的那份**（老版本才是残留，最新的一份永远保留）。

另外两条兜底：7 天内改动过的不动；名字里解析不出版本号的不标可清理（只报告，交给人判断）。
清理默认关闭（``DashboardConfig.allow_package_cleanup``），删除前会重新扫描一次，
只删「此刻仍然可清理」的条目，避免预览与执行之间状态变化。
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

# 中文注释：刚落地不到一天的副本先不动（可能正在激活）；真正的在用版本由下面的
# 标记规则保护，所以这个窗口不需要很长。
DEFAULT_MIN_AGE_DAYS = 1.0
_SECONDS_PER_DAY = 86400.0
# 中文注释：版本号在名字里的位置不固定：``0.162.1-x86_64-unknown-linux-musl``、
# ``grok-1.0.50-linux-x86_64``。只认「带点的数字段」，避免把 x86_64 当版本。
_VERSION_PATTERN = re.compile(r"(?:^|[^0-9.])(\d+(?:\.\d+)+)")
# 中文注释：单个副本的遍历上限，异常目录树不至于卡住请求线程。
_MAX_ENTRIES = 200_000


@dataclass(frozen=True)
class ReleaseSpec:
    """一类「多版本发布副本」的位置与在用标记。

    ``pattern`` 与两个 marker 字段都是相对 provider home 的 glob；marker 允许为空，
    表示这一类没有可判断在用版本的线索（此时只报告，不标可清理）。
    """

    key: str
    label: str
    product: str
    pattern: str
    link_markers: tuple[str, ...] = ()
    version_markers: tuple[str, ...] = ()


# 中文注释：按 provider 注册。Codex 的两种变体（standalone / app-server-daemon）
# 共用同一套标记，所以一条 spec 就够；Grok 的入口符号链接在 bin/ 下，单独列出。
RELEASE_SPECS: tuple[ReleaseSpec, ...] = (
    ReleaseSpec(
        key="codex-releases",
        label="Codex 自更新版本",
        product="codex",
        pattern="packages/*/releases/*",
        link_markers=("packages/*/current",),
        version_markers=("packages/*/auto-update-version",),
    ),
    ReleaseSpec(
        key="grok-downloads",
        label="Grok 自更新下载",
        product="grok",
        pattern="downloads/*",
        link_markers=("bin/grok",),
    ),
)

_SPECS_BY_PRODUCT: dict[str, tuple[ReleaseSpec, ...]] = {}
for _spec in RELEASE_SPECS:
    _SPECS_BY_PRODUCT.setdefault(_spec.product, ())
    _SPECS_BY_PRODUCT[_spec.product] += (_spec,)


@dataclass(frozen=True)
class ReleaseEntry:
    """一份可能可清理的发布副本。"""

    spec_key: str
    label: str
    product: str
    target_label: str
    home: Path
    path: Path
    name: str
    version: str | None
    bytes: int
    files: int
    modified_at: float
    removable: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """转换为 Dashboard 展示字段。"""

        return {
            "spec_key": self.spec_key,
            "label": self.label,
            "product": self.product,
            "target_label": self.target_label,
            "home": str(self.home),
            "path": str(self.path),
            "name": self.name,
            "version": self.version,
            "bytes": self.bytes,
            "files": self.files,
            "modified_at": self.modified_at,
            "removable": self.removable,
            "reason": self.reason,
        }


def parse_release_version(name: str) -> str | None:
    """从目录/文件名里取出版本号（允许出现在中间）；取不到返回 ``None``。"""

    match = _VERSION_PATTERN.search(name.strip())
    return match.group(1) if match else None


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _measure(path: Path) -> tuple[int, int]:
    """返回 ``(字节数, 文件数)``，口径与 ``housekeeping._walk_usage`` 一致。

    中文注释：必须用 ``lstat`` 且跳过符号链接——发布目录里 ``codex -> bin/codex``
    这种链接很常见，跟随它会把同一份二进制算两遍（432 MiB 变 712 MiB）。
    """

    try:
        info = path.lstat()
    except OSError:
        return 0, 0
    if not stat.S_ISDIR(info.st_mode):
        return (0, 0) if stat.S_ISLNK(info.st_mode) else (info.st_size, 1)
    total = 0
    files = 0
    seen = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen > _MAX_ENTRIES:
                return total, files
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                total += entry.stat(follow_symlinks=False).st_size
                files += 1
            except OSError:
                continue
    return total, files


def _modified_at(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _inside(path: Path, root: Path) -> bool:
    """路径解析后是否仍在给定目录之内（防符号链接逃逸）。"""

    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:  # pragma: no cover - resolve 失败时按不安全处理
        return False


def _marker_targets(home: Path, spec: ReleaseSpec) -> tuple[set[Path], set[str]]:
    """收集 spec 声明的在用标记：符号链接目标集合 + 版本名集合。"""

    targets: set[Path] = set()
    versions: set[str] = set()
    for pattern in spec.link_markers:
        for marker in home.glob(pattern):
            if not marker.is_symlink():
                continue
            try:
                targets.add(marker.resolve())
            except OSError:  # pragma: no cover - 断链按无标记处理
                continue
    for pattern in spec.version_markers:
        for marker in home.glob(pattern):
            try:
                raw = marker.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                continue
            if raw:
                versions.add(raw)
                parsed = parse_release_version(raw)
                if parsed:
                    versions.add(parsed)
    return targets, versions


def scan_release_leftovers(
    targets: Sequence[Any],
    *,
    now: float | None = None,
    min_age_days: float = DEFAULT_MIN_AGE_DAYS,
) -> dict[str, Any]:
    """扫描各 provider home 里的发布副本，标出哪些是旧版本。

    ``targets`` 是 ``housekeeping.AuditTarget`` 序列（只用 ``label`` / ``product`` /
    ``path`` 三个字段）；同一 home 被多个账号重复声明时会去重。
    """

    moment = time.time() if now is None else float(now)
    min_age_seconds = max(0.0, float(min_age_days)) * _SECONDS_PER_DAY
    entries: list[ReleaseEntry] = []
    seen_homes: set[tuple[str, str]] = set()

    for target in targets:
        product = str(getattr(target, "product", ""))
        specs = _SPECS_BY_PRODUCT.get(product, ())
        if not specs:
            continue
        home = Path(getattr(target, "path"))
        target_label = str(getattr(target, "label", "") or home)
        for spec in specs:
            marker = (spec.key, str(home))
            if marker in seen_homes:
                continue
            seen_homes.add(marker)
            if not home.is_dir():
                continue
            entries.extend(
                _scan_spec(
                    home,
                    spec,
                    target_label=target_label,
                    moment=moment,
                    min_age_seconds=min_age_seconds,
                )
            )

    # 中文注释：按 provider 目录分组，组内大的排前面——多个 Codex 账号时一眼能对上。
    entries.sort(key=lambda item: (str(item.home), -item.bytes, item.name))
    return {
        "generated_at": moment,
        "min_age_days": float(min_age_days),
        "entries": [entry.to_dict() for entry in entries],
        "totals": {
            "count": len(entries),
            "bytes": sum(entry.bytes for entry in entries),
            "files": sum(entry.files for entry in entries),
            "removable_count": sum(1 for entry in entries if entry.removable),
            "removable_bytes": sum(
                entry.bytes for entry in entries if entry.removable
            ),
        },
    }


def _scan_spec(
    home: Path,
    spec: ReleaseSpec,
    *,
    target_label: str,
    moment: float,
    min_age_seconds: float,
) -> list[ReleaseEntry]:
    """扫描单个 spec：按组（同一个 releases 目录 / downloads 目录）判定在用版本。"""

    protected_paths, protected_versions = _marker_targets(home, spec)
    groups: dict[Path, list[Path]] = {}
    for path in sorted(home.glob(spec.pattern)):
        groups.setdefault(path.parent, []).append(path)

    entries: list[ReleaseEntry] = []
    for group, members in groups.items():
        highest: str | None = None
        parsed_members: list[tuple[Path, str]] = []
        for path in members:
            version = parse_release_version(path.name)
            if version is None:
                continue
            parsed_members.append((path, version))
            if highest is None or _version_key(version) > _version_key(highest):
                highest = version
        for path in members:
            version = parse_release_version(path.name)
            size, files = _measure(path)
            modified = _modified_at(path)
            removable, reason = _classify(
                path=path,
                home=home,
                spec=spec,
                version=version,
                highest=highest,
                protected_paths=protected_paths,
                protected_versions=protected_versions,
                modified=modified,
                moment=moment,
                min_age_seconds=min_age_seconds,
            )
            entries.append(
                ReleaseEntry(
                    spec_key=spec.key,
                    label=spec.label,
                    product=spec.product,
                    target_label=target_label,
                    home=home,
                    path=path,
                    name=path.name,
                    version=version,
                    bytes=size,
                    files=files,
                    modified_at=modified,
                    removable=removable,
                    reason=reason,
                )
            )
    return entries


def _classify(
    *,
    path: Path,
    home: Path,
    spec: ReleaseSpec,
    version: str | None,
    highest: str | None,
    protected_paths: set[Path],
    protected_versions: set[str],
    modified: float,
    moment: float,
    min_age_seconds: float,
) -> tuple[bool, str]:
    """判断一份副本能不能清理，并给出理由（理由会进 i18n 目录表）。"""

    if not _inside(path, home):
        return False, "符号链接指向监控目录之外"
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - 断链
        return False, "路径无法解析"
    if resolved in protected_paths:
        return False, "正在使用（启动链接指向它）"
    if version is not None and version in protected_versions:
        return False, "auto-update-version 记录的在用版本"
    if version is None:
        return False, "文件名里没有版本号，需人工确认"
    if highest is not None and version == highest:
        return False, "同组里版本最高，默认保留"
    if moment - modified < min_age_seconds:
        return False, f"最近 {int(min_age_seconds // _SECONDS_PER_DAY)} 天内改动过"
    return True, "旧版本"


def clean_release_entries(
    paths: Iterable[str],
    targets: Sequence[Any],
    *,
    now: float | None = None,
    min_age_days: float = DEFAULT_MIN_AGE_DAYS,
) -> dict[str, Any]:
    """删除指定的发布副本，只删「此刻仍然可清理」的条目。

    删除前重新扫描一次：预览之后目录结构可能已经变了（升级、手工删除、切版本），
    重新判定可以避免把刚变成「在用」的副本删掉。
    """

    report = scan_release_leftovers(targets, now=now, min_age_days=min_age_days)
    fresh = {str(item["path"]): item for item in report["entries"]}
    removed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    freed = 0
    for raw in paths:
        item = fresh.get(str(raw))
        if item is None:
            skipped.append({"path": str(raw), "reason": "不在可管理的发布副本里"})
            continue
        if not item["removable"]:
            skipped.append({"path": str(raw), "reason": str(item["reason"])})
            continue
        path = Path(item["path"])
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as error:
            skipped.append({"path": str(raw), "reason": f"删除失败: {error}"})
            continue
        freed += int(item["bytes"])
        removed.append(
            {
                "path": str(path),
                "name": item["name"],
                "label": item["label"],
                "bytes": int(item["bytes"]),
            }
        )
    return {
        "removed": removed,
        "skipped": skipped,
        "freed_bytes": freed,
        "report": scan_release_leftovers(
            targets, now=now, min_age_days=min_age_days
        ),
    }
