"""版本更新检测：版本比较、多来源抓取、缓存、升级命令与自动提醒。"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from a_token_monitor import __version__, updates
from a_token_monitor.cli import main as cli_main
from a_token_monitor.dashboard import DashboardConfig, DashboardServer
from a_token_monitor.providers import home_keys


class _TtyStream(io.StringIO):
    """把 stderr 伪装成终端，触发「交互环境才补一次同步检查」的分支。"""

    def isatty(self) -> bool:
        return True


def _next_version(version: str = __version__) -> str:
    """比给定版本更高的一位版本号（次版本 +1）。

    中文注释：凡是比对「当前版本」的用例都不能写死 ``0.10.0`` 这类字面量——
    发一版就全部过期，1.0.0 发布时当场踩到过一次。
    """

    parsed = updates.parse_version(version)
    numbers = list(parsed[0]) if parsed is not None else [1, 0, 0]
    while len(numbers) < 3:
        numbers.append(0)
    numbers[1] += 1
    return ".".join(str(part) for part in numbers)


# 比当前版本更高的一版 / 再高一版，供「发现新版本」类用例使用。
NEXT_VERSION = _next_version()
FAR_VERSION = _next_version(NEXT_VERSION)


def _http_error(url: str, code: int) -> HTTPError:
    return HTTPError(url, code, "boom", {}, None)  # type: ignore[arg-type]


def _release_payload(version: str = "v0.10.0") -> dict:
    return {
        "tag_name": version,
        "html_url": f"https://github.com/pantlive/a-token-monitor/releases/tag/{version}",
        "body": "## Highlights\n- update banner",
        "published_at": "2026-10-09T04:20:00Z",
    }


def _tags_payload(*versions: str) -> list[dict]:
    return [{"name": version} for version in versions]


def _pypi_payload(version: str) -> dict:
    return {
        "info": {"version": version},
        "releases": {
            version: [{"upload_time_iso_8601": "2026-10-09T04:20:00Z"}],
        },
    }


class FakeFetcher:
    """按 URL 返回预置响应；没预置的 URL 按 HTTP 错误处理。"""

    def __init__(self, **routes: object) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, timeout: float, headers=None) -> object:
        self.calls.append((url, dict(headers or {})))
        for fragment, payload in self.routes.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise _http_error(url, 404)

    @property
    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]


class VersionTests(unittest.TestCase):
    """版本号解析与比较：预发布、补零、v 前缀都要正确。"""

    def test_normalize_strips_prefix(self) -> None:
        self.assertEqual(updates.normalize_version(" v0.10.0 "), "0.10.0")
        self.assertEqual(updates.normalize_version(None), "")
        self.assertEqual(updates.normalize_version("V1.2"), "1.2")

    def test_parse_version(self) -> None:
        self.assertEqual(updates.parse_version("v0.10.0"), ((0, 10, 0), ""))
        self.assertEqual(updates.parse_version("0.9"), ((0, 9), ""))
        self.assertEqual(updates.parse_version("1.2.3-rc1"), ((1, 2, 3), "rc1"))
        self.assertIsNone(updates.parse_version("nightly"))
        self.assertIsNone(updates.parse_version(""))

    def test_is_newer(self) -> None:
        cases = {
            ("0.10.0", "0.9.0"): True,
            ("v1.0", "0.99.9"): True,
            ("0.9.0", "0.9.0"): False,
            ("0.9", "0.9.0"): False,
            ("0.8.9", "0.9.0"): False,
            ("0.10.0-rc1", "0.9.0"): False,
            ("0.11.0", "0.10.0-rc1"): True,
            ("nightly", "0.9.0"): False,
        }
        for (candidate, current), expected in cases.items():
            with self.subTest(candidate=candidate, current=current):
                self.assertIs(updates.is_newer(candidate, current), expected)


class FetchTests(unittest.TestCase):
    """三个来源的优先级与降级。"""

    def test_prefers_github_release(self) -> None:
        fetcher = FakeFetcher(**{"releases/latest": _release_payload()})
        candidate = updates.fetch_latest_release(2.0, fetcher)
        self.assertEqual(candidate.version, "0.10.0")
        self.assertEqual(candidate.source, "github-release")
        self.assertIn("update banner", candidate.notes)
        self.assertIsNotNone(candidate.published_at)
        self.assertEqual(fetcher.urls, [updates.RELEASE_API])

    def test_falls_back_to_tags_and_skips_prerelease(self) -> None:
        fetcher = FakeFetcher(
            **{
                "releases/latest": _http_error(updates.RELEASE_API, 404),
                "/tags": _tags_payload("v0.8.0", "v0.9.1-rc1", "v0.10.0"),
            }
        )
        candidate = updates.fetch_latest_release(2.0, fetcher)
        self.assertEqual(candidate.version, "0.10.0")
        self.assertEqual(candidate.source, "github-tag")
        # 没有 Release 时标签详情页会 404，所以指向 releases 列表页。
        self.assertEqual(candidate.url, updates.RELEASES_URL)

    def test_falls_back_to_pypi(self) -> None:
        fetcher = FakeFetcher(
            **{
                "releases/latest": _http_error(updates.RELEASE_API, 403),
                "/tags": _http_error(updates.TAGS_API, 403),
                "pypi.org": _pypi_payload("0.12.0"),
            }
        )
        candidate = updates.fetch_latest_release(2.0, fetcher)
        self.assertEqual(candidate.version, "0.12.0")
        self.assertEqual(candidate.source, "pypi")
        self.assertIsNotNone(candidate.published_at)

    def test_reports_every_failed_source(self) -> None:
        fetcher = FakeFetcher(
            **{
                "releases/latest": _http_error(updates.RELEASE_API, 404),
                "/tags": _http_error(updates.TAGS_API, 500),
                "pypi.org": _http_error(updates.PYPI_API, 503),
            }
        )
        with self.assertRaises(updates.UpdateCheckError) as caught:
            updates.fetch_latest_release(2.0, fetcher)
        message = str(caught.exception)
        for label in ("GitHub Release", "GitHub tags", "PyPI"):
            self.assertIn(label, message)

    def test_empty_sources_report_missing_version(self) -> None:
        fetcher = FakeFetcher(
            **{"releases/latest": {}, "/tags": [], "pypi.org": {"info": {}}}
        )
        with self.assertRaises(updates.UpdateCheckError) as caught:
            updates.fetch_latest_release(2.0, fetcher)
        self.assertIn("所有更新来源都没有可用版本", str(caught.exception))

    def test_github_token_is_sent_when_configured(self) -> None:
        fetcher = FakeFetcher(**{"releases/latest": _release_payload()})
        with mock.patch.dict(
            "os.environ", {"A_TOKEN_MONITOR_GITHUB_TOKEN": "secret"}, clear=False
        ):
            updates.fetch_latest_release(2.0, fetcher)
        headers = fetcher.calls[0][1]
        self.assertEqual(headers.get("Authorization"), "Bearer secret")
        self.assertIn("a-token-monitor/", headers.get("User-Agent", ""))

    def test_fetch_json_builds_a_request(self) -> None:
        """真实抓取路径只做请求构造，响应由 urlopen 打桩。"""

        response = mock.MagicMock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__ = mock.MagicMock(return_value=response)
        response.__exit__ = mock.MagicMock(return_value=False)
        with mock.patch.object(updates, "urlopen", return_value=response) as opened:
            payload = updates._fetch_json("https://example.test/x", 3.0)
        self.assertEqual(payload, {"ok": True})
        request = opened.call_args.args[0]
        self.assertIsInstance(request, Request)
        self.assertEqual(opened.call_args.kwargs["timeout"], 3.0)


class UpgradeHintTests(unittest.TestCase):
    """按安装方式给出升级命令。"""

    def test_pip_install_command(self) -> None:
        # 从 PyPI 安装：既没有本地目录，也不在 pipx 环境里。
        with mock.patch.object(updates, "_local_install", return_value=None), mock.patch.object(
            updates, "_repo_checkout", return_value=None
        ), mock.patch.object(updates, "_pipx_prefix", return_value=False):
            hint = updates.detect_upgrade()
        self.assertEqual(hint["kind"], "pip")
        self.assertIn("-m pip install --upgrade a-token-monitor", hint["command"])
        self.assertEqual(hint["restart_command"], updates.RESTART_COMMAND)

    def test_pipx_install_command(self) -> None:
        with mock.patch.object(updates, "_local_install", return_value=None), mock.patch.object(
            updates, "_repo_checkout", return_value=None
        ), mock.patch.object(updates, "_pipx_prefix", return_value=True):
            hint = updates.detect_upgrade()
        self.assertEqual(hint["kind"], "pipx")
        self.assertEqual(hint["command"], "pipx upgrade a-token-monitor")

    def test_editable_install_uses_git_pull(self) -> None:
        root = Path("/tmp/checkout")
        with mock.patch.object(updates, "_local_install", return_value=(root, True)):
            hint = updates.detect_upgrade()
        self.assertEqual(hint["kind"], "source")
        # 中文注释：期望值用同一套路径渲染拼出来，否则 Windows 上的 `\tmp\checkout`
        # 会让写死 `/tmp/checkout` 的断言失败。
        self.assertEqual(
            hint["command"], f"git -C {updates._quote(str(root))} pull --ff-only"
        )

    def test_local_non_editable_install_reinstalls_from_the_directory(self) -> None:
        root = Path("/tmp/wheelhouse")
        with mock.patch.object(updates, "_local_install", return_value=(root, False)):
            hint = updates.detect_upgrade()
        self.assertEqual(hint["kind"], "local")
        self.assertIn(
            f"install --upgrade {updates._quote(str(root))}", hint["command"]
        )

    def test_repo_checkout_without_install_metadata(self) -> None:
        root = Path("/tmp/source")
        with mock.patch.object(updates, "_local_install", return_value=None), mock.patch.object(
            updates, "_repo_checkout", return_value=root
        ):
            hint = updates.detect_upgrade()
        self.assertEqual(hint["kind"], "source")
        self.assertEqual(
            hint["command"], f"git -C {updates._quote(str(root))} pull --ff-only"
        )

    def test_local_install_reads_pep610_metadata(self) -> None:
        """direct_url.json 解析：可编辑标记、file:// 判定与 URL 解码。"""

        def _distribution(payload: str):
            distribution = mock.MagicMock()
            distribution.read_text.return_value = payload
            return distribution

        with mock.patch.object(
            updates.importlib.metadata,
            "distribution",
            return_value=_distribution(
                json.dumps(
                    {
                        "url": "file:///tmp/my%20project",
                        "dir_info": {"editable": True},
                    }
                )
            ),
        ):
            self.assertEqual(
                updates._local_install(), (Path("/tmp/my project"), True)
            )
        with mock.patch.object(
            updates.importlib.metadata,
            "distribution",
            return_value=_distribution('{"url": "https://pypi.org/simple/x"}'),
        ):
            self.assertIsNone(updates._local_install())
        with mock.patch.object(
            updates.importlib.metadata,
            "distribution",
            side_effect=updates.importlib.metadata.PackageNotFoundError("nope"),
        ):
            self.assertIsNone(updates._local_install())


class CheckerTests(unittest.TestCase):
    """缓存读写、限频、失败降级与后台刷新。"""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def _checker(self, fetcher, **kwargs) -> updates.UpdateChecker:
        options = {
            "state_dir": self.state_dir,
            "current_version": "0.9.0",
            "fetcher": fetcher,
        }
        options.update(kwargs)
        return updates.UpdateChecker(**options)

    def test_refresh_persists_result(self) -> None:
        fetcher = FakeFetcher(**{"releases/latest": _release_payload()})
        snapshot = self._checker(fetcher).refresh()
        self.assertTrue(snapshot["update_available"])
        self.assertEqual(snapshot["latest_version"], "0.10.0")
        self.assertEqual(snapshot["release_source"], "github-release")
        self.assertEqual(snapshot["upgrade"]["kind"], "source")
        cached = json.loads((self.state_dir / updates.CACHE_FILENAME).read_text("utf-8"))
        self.assertEqual(cached["latest_version"], "0.10.0")
        self.assertEqual(cached["schema"], updates.CACHE_SCHEMA)

    def test_snapshot_reads_cache_written_by_another_process(self) -> None:
        fetcher = FakeFetcher(**{"releases/latest": _release_payload()})
        self._checker(fetcher).refresh()
        reader = updates.UpdateChecker(self.state_dir, current_version="0.9.0")
        snapshot = reader.snapshot()
        self.assertTrue(snapshot["update_available"])
        self.assertEqual(snapshot["latest_version"], "0.10.0")
        # 刚成功过：不需要重新抓取，也不会因为读缓存而联网。
        self.assertFalse(reader.should_refresh())

    def test_should_refresh_uses_interval_and_failure_retry(self) -> None:
        fetcher = FakeFetcher(**{"releases/latest": _release_payload()})
        checker = self._checker(fetcher, interval=100.0, failure_interval=10.0)
        self.assertTrue(checker.should_refresh())
        checker.refresh(now=1000.0)
        self.assertFalse(checker.should_refresh(now=1050.0))
        self.assertTrue(checker.should_refresh(now=1100.0))
        # 失败重试用一个干净的状态目录：否则会读到上面那次成功缓存里的 checked_at。
        failing = updates.UpdateChecker(
            self.state_dir / "failing",
            current_version="0.9.0",
            interval=100.0,
            failure_interval=10.0,
            fetcher=FakeFetcher(
                **{"releases/latest": _http_error(updates.RELEASE_API, 500)}
            ),
        )
        failing.refresh(now=2000.0)
        self.assertFalse(failing.should_refresh(now=2005.0))
        self.assertTrue(failing.should_refresh(now=2011.0))

    def test_failure_keeps_the_known_version(self) -> None:
        checker = self._checker(
            FakeFetcher(**{"releases/latest": _release_payload()})
        )
        checker.refresh(now=1000.0)
        checker._fetcher = FakeFetcher(
            **{"releases/latest": _http_error(updates.RELEASE_API, 500)}
        )
        snapshot = checker.refresh(force=True, now=2000.0)
        self.assertTrue(snapshot["update_available"])
        self.assertEqual(snapshot["latest_version"], "0.10.0")
        self.assertIn("GitHub Release", snapshot["last_error"])
        # 失败不清空上次成功时间，页面仍能显示「上次检查」。
        self.assertEqual(snapshot["checked_at"], 1000.0)

    def test_disabled_checker_never_uses_the_network(self) -> None:
        fetcher = FakeFetcher(**{"releases/latest": _release_payload()})
        checker = self._checker(fetcher, enabled=False)
        snapshot = checker.refresh(force=True)
        self.assertFalse(snapshot["enabled"])
        self.assertFalse(snapshot["update_available"])
        self.assertFalse(checker.should_refresh())
        self.assertFalse(checker.refresh_async(force=True))
        self.assertEqual(fetcher.calls, [])

    def test_refresh_async_only_starts_one_thread(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def fetcher(url: str, timeout: float, headers=None) -> object:
            started.set()
            release.wait(5)
            return _release_payload()

        checker = self._checker(fetcher)
        self.assertTrue(checker.refresh_async(force=True))
        self.assertTrue(started.wait(5))
        self.assertFalse(checker.refresh_async(force=True))
        release.set()
        # 中文注释：必须等后台线程真的结束——它还在写缓存临时文件时，用例结束的
        # 目录清理会撞上「Directory not empty」（macOS / Windows 上都复现过）。
        self.assertTrue(checker.wait_for_refresh(timeout=5))
        self.assertTrue(checker.snapshot()["update_available"])
        # 刚抓取成功，非强制调用不会再排队。
        self.assertFalse(checker.refresh_async())

    def test_pending_notice_is_shown_once(self) -> None:
        fetcher = FakeFetcher(**{"releases/latest": _release_payload()})
        checker = self._checker(fetcher)
        checker.refresh()
        pending = checker.pending_notice()
        self.assertIsNotNone(pending)
        checker.mark_notified(pending["latest_version"])
        self.assertIsNone(checker.pending_notice())
        self.assertIsNone(
            updates.UpdateChecker(self.state_dir, current_version="0.9.0").pending_notice()
        )

    def test_local_version_newer_than_release_is_not_an_update(self) -> None:
        fetcher = FakeFetcher(**{"releases/latest": _release_payload("v0.9.0")})
        checker = self._checker(fetcher, current_version="0.10.0")
        self.assertFalse(checker.refresh()["update_available"])
        self.assertIsNone(checker.pending_notice())

    def test_snapshot_picks_up_a_cache_written_by_another_process(self) -> None:
        """CLI 手动检查出更新后，daemon 的页面不用等 6 小时就能亮。"""

        daemon = self._checker(
            FakeFetcher(**{"releases/latest": _release_payload("v0.9.0")})
        )
        self.assertFalse(daemon.refresh()["update_available"])
        # 另一个进程（`a-token-monitor update`）查到了更高版本并写进同一个缓存。
        cli = self._checker(
            FakeFetcher(**{"releases/latest": _release_payload("v0.11.0")})
        )
        cli.refresh(force=True)
        cli.mark_notified("0.11.0")

        snapshot = daemon.snapshot()
        self.assertTrue(snapshot["update_available"])
        self.assertEqual(snapshot["latest_version"], "0.11.0")
        # 提醒标记也一起并进来，daemon 之后写缓存不会把它抹掉。
        self.assertEqual(snapshot["notified_version"], "0.11.0")

    def test_snapshot_adopts_a_cache_written_in_the_same_tick(self) -> None:
        """两个进程落在同一时钟刻度里也要能并入。

        中文注释：Windows 的 time.time() 粒度约 15 毫秒，daemon 与 CLI 各写一次缓存
        时 last_attempt_at 常常完全相等；早期实现用 ``<=`` 判断「文件不比手里新」，
        于是 CLI 刚查到的更新被丢掉（Windows CI 上真实挂过一次）。
        """

        moment = 1000.0
        daemon = self._checker(
            FakeFetcher(**{"releases/latest": _release_payload("v0.9.0")})
        )
        self.assertFalse(daemon.refresh(now=moment)["update_available"])

        cli = self._checker(
            FakeFetcher(**{"releases/latest": _release_payload("v0.11.0")})
        )
        cli.refresh(force=True, now=moment)

        snapshot = daemon.snapshot()
        self.assertTrue(snapshot["update_available"])
        self.assertEqual(snapshot["latest_version"], "0.11.0")
        self.assertEqual(snapshot["last_attempt_at"], moment)

    def test_snapshot_never_goes_backwards(self) -> None:
        """缓存文件比手里的结果旧时（例如自己刚写完）不覆盖内存状态。"""

        checker = self._checker(FakeFetcher(**{"releases/latest": _release_payload()}))
        checker.refresh(force=True)
        checker._state["notified_version"] = "0.10.0"
        # 手工写一份更旧的缓存，模拟被并发进程按旧结果覆盖。
        stale = dict(checker._state)
        stale.update({"latest_version": "0.9.0", "last_attempt_at": 1.0})
        (self.state_dir / updates.CACHE_FILENAME).write_text(
            json.dumps(stale), encoding="utf-8"
        )
        snapshot = checker.snapshot()
        self.assertEqual(snapshot["latest_version"], "0.10.0")
        self.assertTrue(snapshot["update_available"])

    def test_corrupt_cache_is_ignored(self) -> None:
        (self.state_dir / updates.CACHE_FILENAME).write_text("{not json", "utf-8")
        checker = updates.UpdateChecker(self.state_dir, current_version="0.9.0")
        self.assertEqual(checker.snapshot()["latest_version"], "0.9.0")
        self.assertTrue(checker.should_refresh())

    def test_state_without_interval_defaults(self) -> None:
        """snapshot 始终是 JSON 可序列化的完整负载。"""

        snapshot = updates.UpdateChecker(None).snapshot()
        json.dumps(snapshot)
        for key in (
            "current_version",
            "latest_version",
            "update_available",
            "upgrade",
            "releases_url",
        ):
            self.assertIn(key, snapshot)
        self.assertEqual(snapshot["current_version"], __version__)


class CliUpdateTests(unittest.TestCase):
    """CLI：`update` 子命令与一次性命令的自动提醒。"""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        # 任何用例都不应该真的联网。
        patcher = mock.patch.object(
            updates,
            "_fetch_json",
            side_effect=AssertionError("测试不应联网"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for variable in (
            "A_TOKEN_MONITOR_NO_UPDATE_CHECK",
            "A_TOKEN_MONITOR_GITHUB_TOKEN",
            "GITHUB_TOKEN",
            "GH_TOKEN",
        ):
            patcher = mock.patch.dict("os.environ", {variable: ""}, clear=False)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(
        self, *arguments: str, tty: bool = False
    ) -> tuple[int, str, str]:
        stream = _TtyStream if tty else io.StringIO
        stdout = io.StringIO()
        stderr = stream()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli_main(["--state-dir", str(self.state_dir), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def _seed_cache(
        self, version: str = NEXT_VERSION, notified: str | None = None
    ) -> None:
        moment = time.time()
        (self.state_dir / updates.CACHE_FILENAME).write_text(
            json.dumps(
                {
                    "schema": updates.CACHE_SCHEMA,
                    "current_version": __version__,
                    "latest_version": version,
                    "checked_at": moment,
                    "last_attempt_at": moment,
                    "last_error": None,
                    "release_url": f"https://example.test/v{version}",
                    "release_source": "github-release",
                    "published_at": moment - 60,
                    "notes": "notes",
                    "notified_version": notified,
                }
            ),
            encoding="utf-8",
        )

    def test_update_without_cache_flag_checks_the_network(self) -> None:
        self._seed_cache(version=__version__, notified=__version__)
        fetcher = FakeFetcher(
            **{"releases/latest": _release_payload(f"v{NEXT_VERSION}")}
        )
        with mock.patch.object(updates, "_fetch_json", side_effect=fetcher):
            code, stdout, _ = self._run("update")
        self.assertEqual(code, 0)
        self.assertTrue(fetcher.calls)
        self.assertIn(f"最新版本: {NEXT_VERSION}", stdout)
        self.assertIn("状态: 发现新版本", stdout)

    def test_update_json_reports_a_new_version(self) -> None:
        with mock.patch.object(
            updates,
            "_fetch_json",
            side_effect=FakeFetcher(
                **{"releases/latest": _release_payload(f"v{NEXT_VERSION}")}
            ),
        ):
            code, stdout, _ = self._run("update", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertTrue(payload["update_available"])
        self.assertEqual(payload["latest_version"], NEXT_VERSION)
        self.assertIn("command", payload["upgrade"])

    def test_update_cached_prints_upgrade_command_without_network(self) -> None:
        self._seed_cache()
        fetcher = FakeFetcher(
            **{"releases/latest": _release_payload(f"v{NEXT_VERSION}")}
        )
        with mock.patch.object(updates, "_fetch_json", side_effect=fetcher):
            code, stdout, _ = self._run("update", "--cached")
        self.assertEqual(code, 0)
        self.assertIn(f"最新版本: {NEXT_VERSION}", stdout)
        self.assertIn("状态: 发现新版本", stdout)
        self.assertIn("升级命令: ", stdout)
        self.assertEqual(fetcher.calls, [])
        self.assertNotIn("检查更新: 失败", stdout)

    def test_update_reports_failure_without_failing(self) -> None:
        with mock.patch.object(
            updates,
            "_fetch_json",
            side_effect=_http_error(updates.RELEASE_API, 404),
        ):
            code, stdout, _ = self._run("update", "--timeout", "1")
        self.assertEqual(code, 0)
        self.assertIn("检查更新: 失败", stdout)

    def test_notice_printed_once_per_version(self) -> None:
        self._seed_cache()
        with mock.patch.object(updates.UpdateChecker, "should_refresh", return_value=False):
            first = self._run("status")
            second = self._run("status")
        self.assertIn(f"发现新版本 v{NEXT_VERSION}", first[2])
        self.assertNotIn("发现新版本", second[2])

    def test_notice_respects_flag_and_environment(self) -> None:
        self._seed_cache()
        with mock.patch.object(updates.UpdateChecker, "should_refresh", return_value=False):
            flagged = self._run("--no-update-check", "status")
            trailing = self._run("status", "--no-update-check")
        self.assertNotIn("发现新版本", flagged[2])
        self.assertNotIn("发现新版本", trailing[2])
        self._seed_cache()
        with mock.patch.dict(
            "os.environ", {"A_TOKEN_MONITOR_NO_UPDATE_CHECK": "1"}, clear=False
        ):
            with mock.patch.object(
                updates.UpdateChecker, "should_refresh", return_value=False
            ):
                disabled = self._run("status")
        self.assertNotIn("发现新版本", disabled[2])

    def test_notice_calls_check_when_interactive(self) -> None:
        """stderr 是终端且缓存过期时，命令结束后补一次抓取并提醒。"""

        fetcher = FakeFetcher(
            **{"releases/latest": _release_payload(f"v{NEXT_VERSION}")}
        )
        with mock.patch.object(updates, "_fetch_json", side_effect=fetcher):
            _, _, stderr = self._run("status", tty=True)
        self.assertIn(f"发现新版本 v{NEXT_VERSION}", stderr)
        self.assertTrue(fetcher.calls)
        snapshot = updates.UpdateChecker(
            self.state_dir, current_version=__version__
        ).snapshot()
        self.assertEqual(snapshot["notified_version"], NEXT_VERSION)

    def test_upgrade_requires_confirmation_without_a_terminal(self) -> None:
        self._seed_cache()
        with mock.patch("sys.stdin.isatty", return_value=False):
            code, stdout, _ = self._run("update", "--cached", "--upgrade")
        self.assertEqual(code, 2)
        self.assertIn("非交互环境", stdout)

    def test_upgrade_runs_the_detected_command(self) -> None:
        self._seed_cache()
        completed = mock.MagicMock(returncode=0)
        hint = {
            "kind": "pip",
            "label": "pip 安装",
            "command": "python -m pip install --upgrade a-token-monitor",
            "restart_command": updates.RESTART_COMMAND,
        }
        with mock.patch.object(
            updates, "detect_upgrade", return_value=hint
        ), mock.patch("subprocess.run", return_value=completed) as run:
            code, stdout, _ = self._run("update", "--cached", "--upgrade", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("升级完成", stdout)
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0],
            "python -m pip install --upgrade a-token-monitor",
        )

    def test_upgrade_asks_for_confirmation_in_a_terminal(self) -> None:
        self._seed_cache()
        hint = {
            "kind": "pip",
            "label": "pip 安装",
            "command": "python -m pip install --upgrade a-token-monitor",
            "restart_command": updates.RESTART_COMMAND,
        }
        with mock.patch.object(updates, "detect_upgrade", return_value=hint), mock.patch(
            "sys.stdin.isatty", return_value=True
        ), mock.patch("builtins.input", return_value="n"), mock.patch(
            "subprocess.run"
        ) as run:
            code, stdout, _ = self._run("update", "--cached", "--upgrade")
        self.assertEqual(code, 0)
        self.assertIn("确认升级？[y/N]", stdout)
        self.assertIn("已取消升级。", stdout)
        run.assert_not_called()

    def test_upgrade_runs_after_confirmation(self) -> None:
        self._seed_cache()
        hint = {
            "kind": "pip",
            "label": "pip 安装",
            "command": "python -m pip install --upgrade a-token-monitor",
            "restart_command": updates.RESTART_COMMAND,
        }
        completed = mock.MagicMock(returncode=0)
        with mock.patch.object(updates, "detect_upgrade", return_value=hint), mock.patch(
            "sys.stdin.isatty", return_value=True
        ), mock.patch("builtins.input", return_value="y"), mock.patch(
            "subprocess.run", return_value=completed
        ) as run:
            code, stdout, _ = self._run("update", "--cached", "--upgrade")
        self.assertEqual(code, 0)
        self.assertIn("即将执行: python -m pip install --upgrade a-token-monitor", stdout)
        run.assert_called_once()

    def test_failed_upgrade_returns_two(self) -> None:
        self._seed_cache()
        completed = mock.MagicMock(returncode=1)
        with mock.patch("subprocess.run", return_value=completed):
            code, stdout, _ = self._run("update", "--cached", "--upgrade", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("升级失败", stdout)


class DashboardUpdateTests(unittest.TestCase):
    """/api/update 与 /api/state 的更新字段。"""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.servers: list[DashboardServer] = []
        self.addCleanup(self._close_servers)

    def _close_servers(self) -> None:
        for server in self.servers:
            server.close()

    def _server(self, **kwargs) -> DashboardServer:
        options = {
            "registries": {},
            "config": DashboardConfig(port=0),
            "homes": {key: () for key in home_keys()},
            "state_dir": self.state_dir,
        }
        options.update(kwargs)
        server = DashboardServer(**options)
        server.start()
        self.servers.append(server)
        return server

    @staticmethod
    def _get(base_url: str, path: str) -> tuple[int, dict]:
        with urlopen(f"{base_url}{path}", timeout=5) as response:
            return response.status, json.load(response)

    @staticmethod
    def _post(base_url: str, path: str, payload: dict) -> tuple[int, dict]:
        from urllib.error import HTTPError

        request = Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def _wait_for_release(self, base_url: str, version: str = NEXT_VERSION) -> dict:
        deadline = time.time() + 10
        payload: dict = {}
        while time.time() < deadline:
            _, payload = self._get(base_url, "/api/update")
            update = payload.get("update") or {}
            if update.get("latest_version") == version:
                return payload
            time.sleep(0.05)
        self.fail(f"后台检查没有在超时前写入结果: {payload}")

    def test_background_check_populates_state_and_endpoint(self) -> None:
        fetcher = FakeFetcher(
            **{"releases/latest": _release_payload(f"v{NEXT_VERSION}")}
        )
        with mock.patch.object(updates, "_fetch_json", side_effect=fetcher):
            server = self._server()
            host, port = server.address
            base_url = f"http://{host}:{port}"
            payload = self._wait_for_release(base_url)
            _, state = self._get(base_url, "/api/state")
        self.assertTrue(payload["available"])
        self.assertTrue(payload["update"]["update_available"])
        self.assertEqual(state["update"]["latest_version"], NEXT_VERSION)
        self.assertIn("command", state["update"]["upgrade"])
        # 缓存与 CLI 共用，跑完 daemon 后一次性命令也能立刻看到。
        cached = json.loads((self.state_dir / updates.CACHE_FILENAME).read_text("utf-8"))
        self.assertEqual(cached["latest_version"], NEXT_VERSION)

    def test_post_triggers_another_check(self) -> None:
        fetcher = FakeFetcher(
            **{"releases/latest": _release_payload(f"v{FAR_VERSION}")}
        )
        with mock.patch.object(updates, "_fetch_json", side_effect=fetcher):
            server = self._server()
            host, port = server.address
            base_url = f"http://{host}:{port}"
            status, payload = self._post(base_url, "/api/update", {"action": "check"})
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self._wait_for_release(base_url, FAR_VERSION)

    def test_post_rejects_unknown_action(self) -> None:
        server = self._server()
        host, port = server.address
        status, payload = self._post(
            f"http://{host}:{port}", "/api/update", {"action": "install"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_update_action")

    def test_disabled_checker_reports_unavailable(self) -> None:
        fetcher = FakeFetcher(**{"releases/latest": _release_payload()})
        with mock.patch.object(updates, "_fetch_json", side_effect=fetcher):
            server = self._server(config=DashboardConfig(port=0, update_check=False))
            host, port = server.address
            status, payload = self._get(f"http://{host}:{port}", "/api/update")
        self.assertEqual(status, 200)
        self.assertFalse(payload["available"])
        self.assertFalse(payload["update"]["enabled"])
        self.assertFalse(payload["update"]["update_available"])
        self.assertEqual(fetcher.calls, [])

    def test_without_state_dir_the_endpoint_is_unavailable(self) -> None:
        server = self._server(state_dir=None)
        host, port = server.address
        status, payload = self._get(f"http://{host}:{port}", "/api/update")
        self.assertEqual(status, 200)
        self.assertFalse(payload["available"])
        self.assertFalse(payload["update"]["enabled"])
        # 没有状态目录就不再后台联网，也不落缓存。
        self.assertIsNone(server.updates.cache_path)
        self.assertFalse(server.updates.enabled)

    def test_dashboard_page_keeps_the_update_button_visible(self) -> None:
        """按钮常驻：没发布新版本时也要在页面上（只是中性色），不能是 display:none。"""

        from a_token_monitor.dashboard import _DASHBOARD_HTML

        start = _DASHBOARD_HTML.index('id="update-indicator"')
        tag = _DASHBOARD_HTML[start - 20 : _DASHBOARD_HTML.index(">", start)]
        self.assertNotIn("display:none", tag)
        self.assertIn('id="update-indicator-label"', _DASHBOARD_HTML)
        self.assertIn('id="update-detail"', _DASHBOARD_HTML)
        # 详情面板与「复制命令」默认隐藏，由 /api/state 的 update 字段点亮。
        panel = _DASHBOARD_HTML[_DASHBOARD_HTML.index('id="update-detail"') :]
        self.assertIn('style="display:none"', panel[: panel.index(">")])

    def test_config_rejects_bad_intervals(self) -> None:
        with self.assertRaises(ValueError):
            DashboardConfig(update_interval=0)
        with self.assertRaises(ValueError):
            DashboardConfig(update_timeout=0)


if __name__ == "__main__":  # pragma: no cover - 便于单独运行
    unittest.main()
