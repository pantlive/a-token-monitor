"""版本更新检测：GitHub Releases / 标签与 PyPI 三个来源，结果落盘缓存。

设计要点：

* **多来源**：先读 GitHub 最新 Release（带发布说明），再读标签列表（只打了标签
  没建 Release 也能发现），最后读 PyPI JSON（只发 PyPI 时兜底）；任一来源给出
  更高的正式版本就停止，不会因为某个来源限流就查不到更新。
* **离线安全**：只用标准库 ``urllib``，任何网络/解析错误都降级为「检查失败」，
  绝不向 CLI 或 Dashboard 抛异常；失败只记录错误串，并保留上一次的成功结果。
* **不阻塞**：结果缓存在状态目录的 ``update-check.json``。CLI 的一次性命令与
  Dashboard 的 ``/api/state`` 只读缓存，真正的抓取由显式的 ``update`` 命令、
  daemon 的后台线程或 ``POST /api/update`` 触发。
* **升级方式**：按安装方式（pip / pipx / 源码 checkout）给出对应的升级命令，
  纯本地判断，不执行任何写操作。
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import re
import shlex
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from . import __version__

# 中文注释：仓库与产物坐标只在这里写一次，CLI、Dashboard 和文档都引用它。
PACKAGE_NAME = "a-token-monitor"
PROJECT_REPOSITORY = "pantlive/a-token-monitor"
PROJECT_URL = f"https://github.com/{PROJECT_REPOSITORY}"
RELEASES_URL = f"{PROJECT_URL}/releases"
RELEASE_API = f"https://api.github.com/repos/{PROJECT_REPOSITORY}/releases/latest"
TAGS_API = f"https://api.github.com/repos/{PROJECT_REPOSITORY}/tags?per_page=100"
PYPI_API = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"

# 中文注释：默认每 6 小时检查一次；失败后 15 分钟才重试，避免离线时反复打网络。
DEFAULT_INTERVAL = 6 * 3600.0
FAILURE_RETRY_INTERVAL = 900.0
# 中文注释：显式 ``update`` 命令可以多等一会，自动提醒只等 2.5 秒。
DEFAULT_TIMEOUT = 6.0
NOTICE_TIMEOUT = 2.5

CACHE_FILENAME = "update-check.json"
CACHE_SCHEMA = 1
# 中文注释：发布说明只用于展示，截断后再落盘，避免缓存文件无限增长。
NOTES_LIMIT = 1200
ERROR_LIMIT = 240
RESTART_COMMAND = f"{PACKAGE_NAME} service restart"

_HTTP_USER_AGENT = f"{PACKAGE_NAME}/{__version__}"
_TOKEN_VARIABLES = (
    "A_TOKEN_MONITOR_GITHUB_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)
_TRUTHY = {"1", "true", "yes", "on"}
_VERSION_PATTERN = re.compile(r"^(\d+(?:\.\d+)*)(.*)$")
_DISABLE_VARIABLE = "A_TOKEN_MONITOR_NO_UPDATE_CHECK"


class UpdateCheckError(RuntimeError):
    """所有更新来源都不可用。"""


@dataclass(frozen=True)
class ReleaseCandidate:
    """一个来源给出的最新版本信息。"""

    version: str
    source: str
    url: str | None = None
    notes: str = ""
    published_at: float | None = None


def normalize_version(text: object) -> str:
    """去掉 ``v`` 前缀与空白，只保留版本号写法（``v0.10.0`` → ``0.10.0``）。"""

    raw = str(text or "").strip()
    if raw[:1] in {"v", "V"}:
        raw = raw[1:]
    return raw


def parse_version(text: object) -> tuple[tuple[int, ...], str] | None:
    """解析版本号：返回（数字段, 预发布标记），解析不了返回 ``None``。

    数字段补齐比较（``0.9`` 与 ``0.9.0`` 等价），预发布标记（``0.10.0-rc1`` 的
    ``rc1``）非空时表示这不是正式版本，不参与「有新版本」的判定。
    """

    raw = normalize_version(text)
    if not raw:
        return None
    match = _VERSION_PATTERN.match(raw)
    if match is None:
        return None
    numbers = tuple(int(part) for part in match.group(1).split("."))
    prerelease = match.group(2).strip(".-+_ ")
    return numbers, prerelease


def is_newer(candidate: object, current: object) -> bool:
    """判断 ``candidate`` 是否是比 ``current`` 更高的正式版本。"""

    newer = parse_version(candidate)
    base = parse_version(current)
    if newer is None or base is None or newer[1]:
        return False
    left, right = newer[0], base[0]
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def checks_disabled() -> bool:
    """是否通过环境变量关闭了自动更新检查。"""

    return os.environ.get(_DISABLE_VARIABLE, "").strip().lower() in _TRUTHY


def _quote(value: str) -> str:
    """给命令里的路径加引号；Windows 用双引号，其余平台按 shell 规则。"""

    if os.name == "nt":
        return f'"{value}"' if " " in value else value
    return shlex.quote(value)


def _pypi_install_command(package: str = PACKAGE_NAME) -> str:
    executable = _quote(sys.executable) if sys.executable else "python"
    return f"{executable} -m pip install --upgrade {package}"


def _pipx_prefix() -> bool:
    """当前解释器是否来自 pipx 的虚拟环境。"""

    parts = Path(sys.prefix).parts
    if "pipx" in parts:
        return True
    for variable in ("PIPX_HOME", "PIPX_LOCAL_VENVS"):
        value = os.environ.get(variable, "").strip()
        if value and Path(sys.prefix).is_relative_to(Path(value).expanduser()):
            return True
    return False


def _local_install(package: str = PACKAGE_NAME) -> tuple[Path, bool] | None:
    """本地安装信息 ``(源码目录, 是否可编辑安装)``；从发行包安装时返回 ``None``。

    ``direct_url.json`` 是 PEP 610 元数据：可编辑安装会带 ``dir_info.editable``，
    ``pip install .`` 只有 ``file://`` 的 url。两种情况升级方式不同，要分开处理。
    """

    try:
        distribution = importlib.metadata.distribution(package)
    except importlib.metadata.PackageNotFoundError:
        return None
    with contextlib.suppress(OSError, UnicodeDecodeError):
        raw = distribution.read_text("direct_url.json")
        if not raw:
            return None
        with contextlib.suppress(ValueError):
            payload = json.loads(raw)
            if not isinstance(payload, Mapping):
                return None
            url = str(payload.get("url") or "")
            if not url.startswith("file:"):
                return None
            path = unquote(urlsplit(url).path)
            if os.name == "nt" and path[:1] == "/" and ":" in path[:3]:
                # 中文注释：Windows 的 file:///C:/x 会多出一个前导斜杠。
                path = path[1:]
            if path:
                editable = bool((payload.get("dir_info") or {}).get("editable"))
                return Path(path), editable
    return None


def _repo_checkout() -> Path | None:
    """本文件所在目录属于一个 git 仓库时返回仓库根目录。"""

    root = Path(__file__).resolve().parents[2]
    return root if (root / ".git").exists() else None


def detect_upgrade(package: str = PACKAGE_NAME) -> dict[str, str]:
    """按当前安装方式给出升级命令与重启提示（不联网、不执行）。"""

    local = _local_install(package)
    if local is not None:
        root, editable = local
        if editable:
            return {
                "kind": "source",
                "label": "源码安装",
                "command": f"git -C {_quote(str(root))} pull --ff-only",
                "restart_command": RESTART_COMMAND,
            }
        # 中文注释：`pip install .` 装出来的副本不会跟着 git pull 更新，得重装。
        return {
            "kind": "local",
            "label": "本地安装",
            "command": _pypi_install_command(_quote(str(root))),
            "restart_command": RESTART_COMMAND,
        }
    checkout = _repo_checkout()
    if checkout is not None:
        return {
            "kind": "source",
            "label": "源码安装",
            "command": f"git -C {_quote(str(checkout))} pull --ff-only",
            "restart_command": RESTART_COMMAND,
        }
    if _pipx_prefix():
        return {
            "kind": "pipx",
            "label": "pipx 安装",
            "command": f"pipx upgrade {package}",
            "restart_command": RESTART_COMMAND,
        }
    return {
        "kind": "pip",
        "label": "pip 安装",
        "command": _pypi_install_command(package),
        "restart_command": RESTART_COMMAND,
    }


def _github_headers() -> dict[str, str]:
    """GitHub API 请求头；有 token 时带上以提高限额。"""

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": _HTTP_USER_AGENT,
    }
    for variable in _TOKEN_VARIABLES:
        token = os.environ.get(variable, "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
            break
    return headers


def _plain_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": _HTTP_USER_AGENT,
    }


def _fetch_json(
    url: str,
    timeout: float,
    headers: Mapping[str, str] | None = None,
) -> Any:
    """抓取并解析一个 JSON 接口；测试通过替换本函数注入假响应。"""

    request = Request(url, headers=dict(headers or _plain_headers()))
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_timestamp(value: object) -> float | None:
    """把 ISO-8601 时间（``2026-10-09T04:20:00Z``）转成 Unix 时间戳。"""

    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def _github_release(
    fetcher: Callable[..., Any],
    timeout: float,
) -> ReleaseCandidate | None:
    """GitHub 最新 Release；仓库还没建 Release 时返回 ``None``。"""

    payload = fetcher(RELEASE_API, timeout, _github_headers())
    if not isinstance(payload, Mapping):
        return None
    tag = str(payload.get("tag_name") or "").strip()
    if parse_version(tag) is None:
        return None
    url = str(payload.get("html_url") or "").strip() or None
    return ReleaseCandidate(
        version=normalize_version(tag),
        source="github-release",
        url=url,
        notes=str(payload.get("body") or "").strip()[:NOTES_LIMIT],
        published_at=_parse_timestamp(payload.get("published_at")),
    )


def _github_tag(
    fetcher: Callable[..., Any],
    timeout: float,
) -> ReleaseCandidate | None:
    """GitHub 标签里最高的正式版本；发布流程只打标签时靠这一路。"""

    payload = fetcher(TAGS_API, timeout, _github_headers())
    if not isinstance(payload, list):
        return None
    best: tuple[tuple[int, ...], str] | None = None
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        parsed = parse_version(name)
        if parsed is None or parsed[1]:
            continue
        if best is None or parsed[0] > best[0]:
            best = (parsed[0], normalize_version(name))
    if best is None:
        return None
    return ReleaseCandidate(
        version=best[1],
        source="github-tag",
        # 中文注释：走到这一路说明仓库只有标签、没有 Release，标签详情页会 404，
        # 所以指向 releases 列表页（一定存在）。
        url=RELEASES_URL,
    )


def _pypi_release(
    fetcher: Callable[..., Any],
    timeout: float,
) -> ReleaseCandidate | None:
    """PyPI 上的最新版本；只在 GitHub 两路都不可用时兜底。"""

    payload = fetcher(PYPI_API, timeout, _plain_headers())
    if not isinstance(payload, Mapping):
        return None
    info = payload.get("info")
    version = (
        normalize_version(info.get("version")) if isinstance(info, Mapping) else ""
    )
    if parse_version(version) is None:
        return None
    published_at = None
    files = payload.get("releases")
    if isinstance(files, Mapping):
        entries = files.get(version)
        if isinstance(entries, list) and entries:
            first = entries[0]
            if isinstance(first, Mapping):
                published_at = _parse_timestamp(first.get("upload_time_iso_8601"))
    return ReleaseCandidate(
        version=version,
        source="pypi",
        url=f"https://pypi.org/project/{PACKAGE_NAME}/{version}/",
        published_at=published_at,
    )


# 中文注释：按优先级排列；每个来源返回 None 表示「这个来源没有版本信息」。
# 标签用产品名（GitHub Release / GitHub tags / PyPI），中英文界面都不需要再翻译。
_SOURCES: tuple[tuple[str, Callable[..., ReleaseCandidate | None]], ...] = (
    ("GitHub Release", _github_release),
    ("GitHub tags", _github_tag),
    ("PyPI", _pypi_release),
)


def fetch_latest_release(
    timeout: float = DEFAULT_TIMEOUT,
    fetcher: Callable[..., Any] | None = None,
) -> ReleaseCandidate:
    """按优先级询问各来源，返回第一个可用的最新版本。"""

    resolve = fetcher or _fetch_json
    errors: list[str] = []
    for label, source in _SOURCES:
        try:
            candidate = source(resolve, timeout)
        except Exception as error:  # noqa: BLE001 - 任何来源失败都只降级到下一个
            errors.append(f"{label}: {_short_error(error)}")
            continue
        if candidate is not None:
            return candidate
    if errors:
        raise UpdateCheckError("；".join(errors))
    raise UpdateCheckError("所有更新来源都没有可用版本")


def _short_error(error: BaseException) -> str:
    """把异常压成一行短说明，去掉换行并截断。"""

    message = " ".join(str(error).split())
    if len(message) > ERROR_LIMIT:
        message = f"{message[: ERROR_LIMIT - 1]}…"
    return message or error.__class__.__name__


def _read_cache(path: Path | None) -> dict[str, Any]:
    """读取缓存文件；缺失、损坏或版本不符时按空缓存处理。"""

    if path is None:
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(payload, dict) or payload.get("schema") != CACHE_SCHEMA:
        return {}
    return payload


def _write_cache(path: Path | None, payload: Mapping[str, Any]) -> None:
    """原子写入缓存文件；目录不可写时静默跳过（只影响下次是否重查）。"""

    if path is None:
        return
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()


class UpdateChecker:
    """线程安全的版本检查器：缓存读写、限频抓取与后台异步刷新。

    ``state_dir`` 为 ``None`` 时只用内存状态（不落盘），适用于测试或一次性调用；
    ``enabled`` 为 ``False`` 时 ``refresh`` 直接返回缓存，不发任何网络请求。

    缓存文件是 CLI 与 daemon 共享的：手动跑过 ``a-token-monitor update`` 之后，
    页面上还应该立刻亮起徽标，所以每次读状态都比较一次文件里的检查时间，把别的
    进程写的新结果并进来（只并「比手里更新」的，不会把新信息读旧）。文件只有
    几百字节，且 ``/api/state`` 本身有 2 秒缓存，这点读取可以忽略。
    """

    def __init__(
        self,
        state_dir: Path | str | None = None,
        *,
        current_version: str | None = None,
        interval: float = DEFAULT_INTERVAL,
        failure_interval: float = FAILURE_RETRY_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        enabled: bool = True,
        fetcher: Callable[..., Any] | None = None,
        cache_path: Path | None = None,
    ) -> None:
        self.state_dir = (
            Path(state_dir).expanduser() if state_dir is not None else None
        )
        self.cache_path = (
            cache_path
            if cache_path is not None
            else (self.state_dir / CACHE_FILENAME if self.state_dir else None)
        )
        self.current_version = normalize_version(current_version or __version__)
        self.interval = float(interval)
        self.failure_interval = float(failure_interval)
        self.timeout = float(timeout)
        self.enabled = bool(enabled)
        self._fetcher = fetcher
        self._state: dict[str, Any] = self._empty_state()
        self._state.update(_read_cache(self.cache_path))
        self._state_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._thread_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._upgrade: dict[str, str] | None = None

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema": CACHE_SCHEMA,
            "current_version": self.current_version,
            "latest_version": self.current_version,
            "checked_at": None,
            "last_attempt_at": None,
            "last_error": None,
            "release_url": None,
            "release_source": None,
            "published_at": None,
            "notes": "",
            "notified_version": None,
        }

    # ---------------------------------------------------------------- 读缓存

    def _reload_cache(self) -> None:
        """把别的进程写进缓存文件的新结果并进来。

        只比较内容里的 ``last_attempt_at``：文件系统的时间戳粒度可能粗到两次
        写入完全一样，靠 mtime 判断会漏掉更新。
        """

        payload = _read_cache(self.cache_path)
        if not payload:
            return
        attempted = payload.get("last_attempt_at")
        if attempted is None:
            return
        with self._state_lock:
            known = self._state.get("last_attempt_at")
            # 中文注释：只有「文件确实比手里旧」才跳过；时间戳相等时以文件为准。
            # Windows 的 time.time() 粒度约 15 毫秒，两个进程同一刻度各写一次是常态，
            # 用 <= 会把刚由 CLI 写进来的新结果当成旧的丢掉（CI 上就是这样挂的）。
            if known is not None and float(attempted) < float(known):
                return
            if payload.get("notified_version") is None:
                # 提醒标记是 CLI 写的，别在并入时丢掉。
                payload["notified_version"] = self._state.get("notified_version")
            # 只并状态字段，缓存文件里多余的键不带进内存。
            self._state.update(
                {key: value for key, value in payload.items() if key in self._state}
            )

    def should_refresh(self, now: float | None = None) -> bool:
        """是否到了该重新抓取的时间（成功按 interval，失败按 failure_interval）。"""

        if not self.enabled:
            return False
        self._reload_cache()
        moment = time.time() if now is None else float(now)
        with self._state_lock:
            attempted = self._state.get("last_attempt_at")
            succeeded = self._state.get("checked_at")
        if attempted is None:
            return True
        window = self.interval if succeeded else self.failure_interval
        return moment - float(attempted) >= window

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        """返回可直接展示的当前状态；只读缓存，不联网。"""

        self._reload_cache()
        moment = time.time() if now is None else float(now)
        with self._state_lock:
            state = dict(self._state)
        checked_at = state.get("checked_at")
        latest = normalize_version(state.get("latest_version"))
        state.update(
            {
                "schema": CACHE_SCHEMA,
                "enabled": self.enabled,
                "current_version": self.current_version,
                "latest_version": latest or self.current_version,
                "update_available": is_newer(latest, self.current_version),
                "checked_at": checked_at,
                "checked_ago": (
                    None if checked_at is None else max(0.0, moment - float(checked_at))
                ),
                "stale": self.should_refresh(moment),
                "upgrade": self.upgrade(),
                "homepage": PROJECT_URL,
                "releases_url": RELEASES_URL,
            }
        )
        return state

    def upgrade(self) -> dict[str, str]:
        """当前安装方式的升级命令（进程内只探测一次）。"""

        with self._state_lock:
            if self._upgrade is None:
                with contextlib.suppress(Exception):
                    self._upgrade = detect_upgrade()
            return dict(self._upgrade or self._fallback_upgrade())

    @staticmethod
    def _fallback_upgrade() -> dict[str, str]:
        return {
            "kind": "pip",
            "label": "pip 安装",
            "command": _pypi_install_command(),
            "restart_command": RESTART_COMMAND,
        }

    # ---------------------------------------------------------------- 抓取

    def refresh(self, force: bool = False, now: float | None = None) -> dict[str, Any]:
        """按需抓取一次；未到时间且 ``force`` 为假时直接返回缓存。"""

        moment = time.time() if now is None else float(now)
        if not self.enabled:
            return self.snapshot(moment)
        if not force and not self.should_refresh(moment):
            return self.snapshot(moment)
        # 中文注释：同一时刻只允许一个抓取在跑，其余调用直接读缓存。
        if not self._refresh_lock.acquire(blocking=False):
            return self.snapshot(moment)
        try:
            if not force and not self.should_refresh(moment):
                return self.snapshot(moment)
            self._attempt(moment)
        finally:
            self._refresh_lock.release()
        return self.snapshot(moment)

    def _attempt(self, moment: float) -> None:
        try:
            candidate = fetch_latest_release(
                timeout=self.timeout,
                fetcher=self._fetcher,
            )
        except Exception as error:  # noqa: BLE001 - 检查失败不影响主流程
            self._record_failure(error, moment)
            return
        self._record_success(candidate, moment)

    def _record_success(self, candidate: ReleaseCandidate, moment: float) -> None:
        latest = normalize_version(candidate.version)
        with self._state_lock:
            state = dict(self._state)
            state.update(
                {
                    "checked_at": moment,
                    "last_attempt_at": moment,
                    "last_error": None,
                    "latest_version": latest,
                    "release_url": candidate.url,
                    "release_source": candidate.source,
                    "published_at": candidate.published_at,
                    "notes": (candidate.notes or "")[:NOTES_LIMIT],
                }
            )
            if not is_newer(latest, self.current_version):
                # 中文注释：已经追上（或本地更新），下次有新版本要重新提醒一次。
                state["notified_version"] = None
        self._commit(state)

    def _record_failure(self, error: BaseException, moment: float) -> None:
        with self._state_lock:
            state = dict(self._state)
            state.update(
                {
                    "last_attempt_at": moment,
                    "last_error": _short_error(error),
                }
            )
        self._commit(state)

    def _commit(self, state: dict[str, Any]) -> None:
        """先落盘再更新内存。

        中文注释：顺序反过来会出现「内存里已经能看到新结果、缓存文件还是旧的」的
        窗口——命令行进程正好在此刻退出，或另一个进程此刻读文件，就会拿到旧数据。
        先写盘则保证：任何进程看到新状态时，文件已经是新的。
        """

        _write_cache(self.cache_path, state)
        with self._state_lock:
            self._state.update(state)

    def refresh_async(self, force: bool = False, now: float | None = None) -> bool:
        """在后台线程里抓取一次；已有抓取在跑或未到时间时返回 ``False``。"""

        if not self.enabled:
            return False
        with self._thread_lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                return False
            if not force and not self.should_refresh(now):
                return False
            thread = threading.Thread(
                target=self.refresh,
                kwargs={"force": force, "now": now},
                name="a-token-monitor-update-check",
                daemon=True,
            )
            self._thread = thread
        thread.start()
        return True

    def wait_for_refresh(self, timeout: float | None = None) -> bool:
        """等待后台抓取线程结束；返回是否已经结束（没有线程时视为已结束）。"""

        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # ---------------------------------------------------------------- 提醒

    def mark_notified(self, version: object) -> None:
        """记录「这个版本已经提醒过」，避免每次命令都重复提示。"""

        normalized = normalize_version(version)
        with self._state_lock:
            if self._state.get("notified_version") == normalized:
                return
            state = dict(self._state)
            state["notified_version"] = normalized
        self._commit(state)

    def pending_notice(self) -> dict[str, Any] | None:
        """有新版本且还没提醒过时返回快照，否则返回 ``None``。"""

        state = self.snapshot()
        if not state.get("update_available"):
            return None
        if state.get("notified_version") == state.get("latest_version"):
            return None
        return state
