"""版本/安装残留：扫描规则、保留判定与清理接口。"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from a_token_monitor import packages
from a_token_monitor.dashboard import DashboardConfig, DashboardServer
from a_token_monitor.housekeeping import AuditTarget, DiskThresholds, HousekeepingMonitor
from a_token_monitor.providers import home_keys

_DAY = 86400.0


def _make_home(root: Path, product: str, label: str, variant: str = "standalone") -> AuditTarget:
    home = root / product
    home.mkdir(parents=True, exist_ok=True)
    releases = home / "packages" / variant / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    return AuditTarget(label, product, home, sessions_root=None)


def _make_release(releases: Path, name: str, *, size: int = 1024, age_days: float = 30.0) -> Path:
    """造一份发布副本：bin/ + 一个指向它的内部符号链接（模拟真实布局）。"""

    target = releases / name
    (target / "bin").mkdir(parents=True, exist_ok=True)
    (target / "bin" / "codex").write_bytes(b"x" * size)
    (target / "codex").symlink_to("bin/codex")
    moment = time.time() - age_days * _DAY
    import os

    os.utime(target, (moment, moment))
    return target


def _mark_current(variant_dir: Path, name: str) -> None:
    """写 current 符号链接与 auto-update-version 标记。"""

    link = variant_dir / "current"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(variant_dir / "releases" / name, target_is_directory=True)
    (variant_dir / "auto-update-version").write_text(name, encoding="utf-8")


class ScanTests(unittest.TestCase):
    """保留规则：current / auto-update-version / 同组最新 / 太新 / 无法解析。"""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def _scan(self, targets, **kwargs):
        return packages.scan_release_leftovers(targets, **kwargs)

    def test_keeps_current_and_flags_older_versions(self) -> None:
        target = _make_home(self.root, "codex", "Codex (codex)")
        variant = target.path / "packages" / "standalone"
        releases = variant / "releases"
        for name in (
            "0.160.0-x86_64-unknown-linux-musl",
            "0.161.0-x86_64-unknown-linux-musl",
            "0.162.1-x86_64-unknown-linux-musl",
        ):
            _make_release(releases, name, size=2048)
        _mark_current(variant, "0.162.1-x86_64-unknown-linux-musl")

        report = self._scan([target], min_age_days=1.0)
        by_name = {item["name"]: item for item in report["entries"]}
        self.assertEqual(len(by_name), 3)
        self.assertFalse(by_name["0.162.1-x86_64-unknown-linux-musl"]["removable"])
        self.assertIn("正在使用", by_name["0.162.1-x86_64-unknown-linux-musl"]["reason"])
        self.assertTrue(by_name["0.161.0-x86_64-unknown-linux-musl"]["removable"])
        self.assertTrue(by_name["0.160.0-x86_64-unknown-linux-musl"]["removable"])
        self.assertEqual(
            report["totals"]["removable_bytes"],
            by_name["0.161.0-x86_64-unknown-linux-musl"]["bytes"]
            + by_name["0.160.0-x86_64-unknown-linux-musl"]["bytes"],
        )

    def test_symlinked_binary_is_counted_once(self) -> None:
        """真实布局里 ``codex -> bin/codex``；跟随符号链接会把同一份二进制算两遍。"""

        target = _make_home(self.root, "codex", "Codex (codex)")
        variant = target.path / "packages" / "standalone"
        _make_release(variant / "releases", "0.1.0-x86_64", size=4096)
        report = self._scan([target])
        entry = report["entries"][0]
        self.assertEqual(entry["bytes"], 4096)
        self.assertEqual(entry["files"], 1)

    def test_newest_version_is_kept_even_without_markers(self) -> None:
        target = _make_home(self.root, "codex", "Codex (codex)")
        releases = target.path / "packages" / "standalone" / "releases"
        _make_release(releases, "0.1.0-x86_64")
        _make_release(releases, "0.2.0-x86_64")
        report = self._scan([target])
        by_name = {item["name"]: item for item in report["entries"]}
        self.assertFalse(by_name["0.2.0-x86_64"]["removable"])
        self.assertIn("版本最高", by_name["0.2.0-x86_64"]["reason"])
        self.assertTrue(by_name["0.1.0-x86_64"]["removable"])

    def test_auto_update_version_keeps_the_recorded_release(self) -> None:
        """current 指向 0.1.0：它和「同组最新」都保留，只有更旧的 0.0.9 可清理。"""

        target = _make_home(self.root, "codex", "Codex (codex)")
        variant = target.path / "packages" / "standalone"
        releases = variant / "releases"
        for name in ("0.0.9-x86_64", "0.1.0-x86_64", "0.2.0-x86_64"):
            _make_release(releases, name)
        _mark_current(variant, "0.1.0-x86_64")
        report = self._scan([target])
        by_name = {item["name"]: item for item in report["entries"]}
        self.assertFalse(by_name["0.1.0-x86_64"]["removable"])
        self.assertIn("正在使用", by_name["0.1.0-x86_64"]["reason"])
        # 比 current 新但还没启用的那份可能是「正在激活的更新」，同样保留。
        self.assertFalse(by_name["0.2.0-x86_64"]["removable"])
        self.assertIn("版本最高", by_name["0.2.0-x86_64"]["reason"])
        self.assertTrue(by_name["0.0.9-x86_64"]["removable"])

    def test_fresh_release_is_kept(self) -> None:
        """刚落地不到一天的副本先不动（可能正在激活）。"""

        target = _make_home(self.root, "codex", "Codex (codex)")
        variant = target.path / "packages" / "standalone"
        releases = variant / "releases"
        _make_release(releases, "0.1.0-x86_64", age_days=30.0)
        _make_release(releases, "0.2.0-x86_64", age_days=0.01)
        _make_release(releases, "0.3.0-x86_64", age_days=30.0)
        _mark_current(variant, "0.3.0-x86_64")
        report = self._scan([target], min_age_days=1.0)
        by_name = {item["name"]: item for item in report["entries"]}
        self.assertFalse(by_name["0.2.0-x86_64"]["removable"])
        self.assertIn("天内改动过", by_name["0.2.0-x86_64"]["reason"])
        self.assertTrue(by_name["0.1.0-x86_64"]["removable"])

    def test_unparsable_name_is_reported_but_kept(self) -> None:
        target = _make_home(self.root, "codex", "Codex (codex)")
        releases = target.path / "packages" / "standalone" / "releases"
        _make_release(releases, "nightly")
        _make_release(releases, "0.1.0-x86_64")
        report = self._scan([target])
        by_name = {item["name"]: item for item in report["entries"]}
        self.assertFalse(by_name["nightly"]["removable"])
        self.assertIn("没有版本号", by_name["nightly"]["reason"])

    def test_symlink_escaping_the_home_is_never_removable(self) -> None:
        target = _make_home(self.root, "codex", "Codex (codex)")
        outside = self.root / "outside" / "0.0.1-x86_64"
        (outside / "bin").mkdir(parents=True)
        (outside / "bin" / "codex").write_bytes(b"x" * 32)
        releases = target.path / "packages" / "standalone" / "releases"
        _make_release(releases, "0.9.9-x86_64")
        (releases / "0.0.1-x86_64").symlink_to(outside, target_is_directory=True)
        report = self._scan([target])
        by_name = {item["name"]: item for item in report["entries"]}
        self.assertFalse(by_name["0.0.1-x86_64"]["removable"])
        self.assertIn("监控目录之外", by_name["0.0.1-x86_64"]["reason"])

    def test_grok_downloads_use_the_launcher_symlink(self) -> None:
        home = self.root / "grok"
        (home / "downloads").mkdir(parents=True)
        (home / "bin").mkdir(parents=True)
        import os

        moment = time.time() - 30 * _DAY
        for name, size in (
            ("grok-1.0.44-linux-x86_64", 111),
            ("grok-1.0.46-linux-x86_64", 222),
            ("grok-1.0.50-linux-x86_64", 333),
            ("grok-linux-x86_64", 444),
        ):
            path = home / "downloads" / name
            path.write_bytes(b"x" * size)
            os.utime(path, (moment, moment))
        (home / "bin" / "grok").symlink_to("../downloads/grok-1.0.50-linux-x86_64")
        target = AuditTarget("Grok", "grok", home)

        report = self._scan([target])
        by_name = {item["name"]: item for item in report["entries"]}
        self.assertEqual(len(by_name), 4)
        self.assertFalse(by_name["grok-1.0.50-linux-x86_64"]["removable"])
        self.assertTrue(by_name["grok-1.0.46-linux-x86_64"]["removable"])
        self.assertTrue(by_name["grok-1.0.44-linux-x86_64"]["removable"])
        # 名字里没有版本号的那份只报告，不标可清理。
        self.assertFalse(by_name["grok-linux-x86_64"]["removable"])

    def test_products_without_specs_are_ignored(self) -> None:
        home = self.root / "kimi"
        (home / "downloads").mkdir(parents=True)
        (home / "downloads" / "kimi-1.0.0").write_bytes(b"x")
        report = self._scan([AuditTarget("Kimi Code", "kimi", home)])
        self.assertEqual(report["entries"], [])
        self.assertEqual(report["totals"]["count"], 0)

    def test_missing_home_is_skipped(self) -> None:
        report = self._scan([AuditTarget("Codex", "codex", self.root / "missing")])
        self.assertEqual(report["entries"], [])


class CleanTests(unittest.TestCase):
    """清理只删「此刻仍然可清理」的条目。"""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.target = _make_home(self.root, "codex", "Codex (codex)")
        variant = self.target.path / "packages" / "standalone"
        self.releases = variant / "releases"
        _make_release(self.releases, "0.1.0-x86_64", size=128)
        _make_release(self.releases, "0.2.0-x86_64", size=256)
        _mark_current(variant, "0.2.0-x86_64")

    def test_removes_only_removable_entries(self) -> None:
        old = self.releases / "0.1.0-x86_64"
        current = self.releases / "0.2.0-x86_64"
        result = packages.clean_release_entries(
            [str(old), str(current), str(self.root / "nope")], [self.target]
        )
        self.assertEqual([item["name"] for item in result["removed"]], ["0.1.0-x86_64"])
        self.assertEqual(result["freed_bytes"], 128)
        self.assertFalse(old.exists())
        self.assertTrue(current.exists())
        reasons = {item["path"]: item["reason"] for item in result["skipped"]}
        self.assertIn("正在使用", reasons[str(current)])
        self.assertIn("不在可管理的发布副本里", reasons[str(self.root / "nope")])

    def test_rechecks_before_deleting(self) -> None:
        """预览之后 current 换了目标：旧判定作废，不能删成「正在使用」的那份。"""

        old = self.releases / "0.1.0-x86_64"
        variant = self.target.path / "packages" / "standalone"
        _mark_current(variant, "0.1.0-x86_64")
        result = packages.clean_release_entries([str(old)], [self.target])
        self.assertEqual(result["removed"], [])
        self.assertTrue(old.exists())
        self.assertIn("正在使用", result["skipped"][0]["reason"])

    def test_report_is_refreshed_after_cleanup(self) -> None:
        old = self.releases / "0.1.0-x86_64"
        result = packages.clean_release_entries([str(old)], [self.target])
        names = [item["name"] for item in result["report"]["entries"]]
        self.assertNotIn("0.1.0-x86_64", names)


class PackagesApiTests(unittest.TestCase):
    """/api/packages：默认只报告，清理需要显式开启。"""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        target = _make_home(self.root, "codex", "Codex (codex)")
        variant = target.path / "packages" / "standalone"
        self.releases = variant / "releases"
        _make_release(self.releases, "0.1.0-x86_64", size=64)
        _make_release(self.releases, "0.2.0-x86_64", size=64)
        _mark_current(variant, "0.2.0-x86_64")
        self.monitor = HousekeepingMonitor(
            targets=[target],
            thresholds=DiskThresholds.from_gb(single_warn_gb=5.0, total_warn_gb=10.0),
            archive_dir=self.root / "archives",
        )
        self.servers: list[DashboardServer] = []
        self.addCleanup(self._close)

    def _close(self) -> None:
        for server in self.servers:
            server.close()

    def _server(self, **kwargs) -> DashboardServer:
        options = {
            "registries": {},
            "config": DashboardConfig(port=0, warm_english_pages=False),
            "homes": {key: () for key in home_keys()},
            "housekeeping": self.monitor,
            "state_dir": self.root / "state",
        }
        options.update(kwargs)
        server = DashboardServer(**options)
        server.start()
        self.servers.append(server)
        return server

    @staticmethod
    def _request(base_url: str, path: str, payload: dict | None = None):
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        if payload is None:
            request = Request(f"{base_url}{path}")
        else:
            request = Request(
                f"{base_url}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_get_reports_entries_and_enabled_cleanup(self) -> None:
        server = self._server()
        host, port = server.address
        status, payload = self._request(f"http://{host}:{port}", "/api/packages")
        self.assertEqual(status, 200)
        self.assertTrue(payload["available"])
        # 中文注释：默认就能在网页上清理；只有显式 --no-package-cleanup 才关闭。
        self.assertTrue(payload["cleanup_enabled"])
        totals = payload["report"]["totals"]
        self.assertEqual(totals["count"], 2)
        self.assertEqual(totals["removable_count"], 1)

    def test_post_is_refused_while_cleanup_is_disabled(self) -> None:
        server = self._server(
            config=DashboardConfig(port=0, allow_package_cleanup=False)
        )
        host, port = server.address
        status, payload = self._request(
            f"http://{host}:{port}",
            "/api/packages",
            {"action": "clean", "items": [str(self.releases / "0.1.0-x86_64")], "confirm": True},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "package_cleanup_disabled")
        self.assertTrue((self.releases / "0.1.0-x86_64").exists())

    def test_post_cleans_with_the_default_config(self) -> None:
        server = self._server()
        host, port = server.address
        base = f"http://{host}:{port}"
        _, payload = self._request(base, "/api/packages")
        self.assertTrue(payload["cleanup_enabled"])
        status, result = self._request(
            base,
            "/api/packages",
            {"action": "clean", "items": [str(self.releases / "0.1.0-x86_64")], "confirm": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["freed_bytes"], 64)
        self.assertFalse((self.releases / "0.1.0-x86_64").exists())
        self.assertTrue((self.releases / "0.2.0-x86_64").exists())

    def test_post_requires_confirmation_and_items(self) -> None:
        server = self._server()
        host, port = server.address
        base = f"http://{host}:{port}"
        status, payload = self._request(
            base, "/api/packages", {"action": "clean", "items": ["x"]}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "confirmation_required")
        status, payload = self._request(
            base, "/api/packages", {"action": "clean", "confirm": True, "items": []}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "no_items_selected")
        status, payload = self._request(
            base,
            "/api/packages",
            {"action": "explode", "confirm": True, "items": ["x"]},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_package_action")

    def test_post_without_housekeeping_returns_503(self) -> None:
        server = self._server(housekeeping=None)
        host, port = server.address
        status, payload = self._request(
            f"http://{host}:{port}", "/api/packages", {"action": "clean", "confirm": True}
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "package_scan_unavailable")

    def test_page_contains_the_packages_block(self) -> None:
        from a_token_monitor.dashboard import _DASHBOARD_HTML

        self.assertIn('id="packages-block"', _DASHBOARD_HTML)
        self.assertIn('id="packages-content"', _DASHBOARD_HTML)
        self.assertIn("清理已关闭", _DASHBOARD_HTML)

    def test_report_is_cached_between_requests(self) -> None:
        """连点刷新不应反复遍历磁盘：第二次请求命中 10 秒缓存。"""

        server = self._server()
        host, port = server.address
        base = f"http://{host}:{port}"
        context = server._server.RequestHandlerClass.context  # noqa: SLF001 - 测试内部状态
        # 中文注释：handler 在导入时就把函数绑到自己模块里了，要 patch 那一份引用。
        from a_token_monitor.dashboard import handler as handler_module

        with mock.patch.object(
            handler_module,
            "scan_release_leftovers",
            wraps=packages.scan_release_leftovers,
        ) as scanner:
            context.packages_cache.clear()
            self._request(base, "/api/packages")
            self._request(base, "/api/packages")
        self.assertEqual(scanner.call_count, 1)


if __name__ == "__main__":  # pragma: no cover - 便于单独运行
    unittest.main()
