"""Web 端扫描目录配置、校验和优先级解析的测试。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from token_monitor.scan_dirs import (
    PROVIDER_SPECS,
    EffectiveScanDirs,
    ProviderSpec,
    ScanDirsConfig,
    ScanDirsController,
    ScanDirsError,
    resolve_effective,
    validate_directory,
)


def _codex_spec(default_home: Path) -> ProviderSpec:
    """构造指向临时目录的 Codex spec,避免测试触碰真实主目录。"""

    base = PROVIDER_SPECS["codex"]
    return ProviderSpec(
        key=base.key,
        display_name=base.display_name,
        cli_option=base.cli_option,
        markers=base.markers,
        default_home=lambda: default_home,
        resolver=lambda homes: (
            tuple(Path(item) for item in homes)
            if homes is not None
            else ((default_home,) if default_home.exists() else ())
        ),
    )


class ValidateDirectoryTests(unittest.TestCase):
    """校验目录存在性、可读性、结构和路径范围限制。"""

    def test_valid_codex_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            codex = home / ".codex"
            (codex / "sessions").mkdir(parents=True)

            result = validate_directory("codex", codex, home_dir=home)

            self.assertTrue(result.ok)
            self.assertTrue(result.structure_ok)
            self.assertEqual(result.errors, ())

    def test_missing_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            home.mkdir()

            result = validate_directory(
                "codex", home / ".codex-missing", home_dir=home
            )

            self.assertFalse(result.ok)
            self.assertTrue(any("不存在" in item for item in result.errors))

    def test_regular_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            home.mkdir()
            target = home / "file.txt"
            target.write_text("x", encoding="utf-8")

            result = validate_directory("codex", target, home_dir=home)

            self.assertFalse(result.ok)
            self.assertFalse(result.exists)

    def test_path_outside_home_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            home.mkdir()
            outside = root / "elsewhere"
            outside.mkdir()

            result = validate_directory("codex", outside, home_dir=home)

            self.assertFalse(result.ok)
            self.assertTrue(any("主目录" in item for item in result.errors))

    def test_home_root_itself_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            home.mkdir()

            result = validate_directory("codex", home, home_dir=home)

            self.assertFalse(result.ok)

    def test_sensitive_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            ssh = home / ".ssh"
            ssh.mkdir(parents=True)

            result = validate_directory("codex", ssh, home_dir=home)

            self.assertFalse(result.ok)
            self.assertTrue(any("敏感目录" in item for item in result.errors))

    def test_state_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            home.mkdir()
            state_dir = home / ".token-monitor"
            state_dir.mkdir()

            result = validate_directory(
                "codex", state_dir, home_dir=home, state_dir=state_dir
            )

            self.assertFalse(result.ok)
            self.assertTrue(any("状态目录" in item for item in result.errors))

    @unittest.skipIf(os.geteuid() == 0, "root 可以读取任何目录")
    def test_unreadable_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            target = home / ".codex"
            target.mkdir(parents=True)
            target.chmod(0)
            try:
                result = validate_directory("codex", target, home_dir=home)
            finally:
                target.chmod(stat.S_IRWXU)

            self.assertFalse(result.ok)
            self.assertTrue(any("不可读" in item for item in result.errors))

    def test_structure_mismatch_is_warning_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            plain = home / "plain-data"
            plain.mkdir(parents=True)

            result = validate_directory("codex", plain, home_dir=home)

            self.assertTrue(result.ok)
            self.assertFalse(result.structure_ok)
            self.assertTrue(result.warnings)

    def test_unknown_provider_raises(self) -> None:
        with self.assertRaises(ScanDirsError):
            validate_directory("unknown", Path("/tmp"))


class ScanDirsConfigTests(unittest.TestCase):
    """配置持久化与严格校验。"""

    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "scan-dirs.json"
            config = ScanDirsConfig(
                overrides={"codex": (root / ".codex",), "grok": ()}
            )

            config.save(path)
            loaded = ScanDirsConfig.load(path)

            self.assertEqual(set(loaded.overrides), {"codex", "grok"})
            self.assertEqual(loaded.overrides["codex"], (root / ".codex",))
            self.assertEqual(loaded.overrides["grok"], ())
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_missing_file_means_no_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            loaded = ScanDirsConfig.load(
                Path(temporary_directory) / "scan-dirs.json"
            )
            self.assertEqual(dict(loaded.overrides), {})

    def test_bad_schema_version_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "scan-dirs.json"
            path.write_text(
                json.dumps({"schema_version": 99, "overrides": {}}),
                encoding="utf-8",
            )
            with self.assertRaises(ScanDirsError):
                ScanDirsConfig.load(path)

    def test_unknown_provider_in_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "scan-dirs.json"
            path.write_text(
                json.dumps(
                    {"schema_version": 1, "overrides": {"mystery": ["/tmp"]}}
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ScanDirsError):
                ScanDirsConfig.load(path)

    def test_non_string_entry_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "scan-dirs.json"
            path.write_text(
                json.dumps(
                    {"schema_version": 1, "overrides": {"codex": [12]}}
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ScanDirsError):
                ScanDirsConfig.load(path)

    def test_add_remove_reset_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = ScanDirsConfig()

            config = config.with_added("codex", root / ".codex")
            config = config.with_added("codex", root / ".codex")
            self.assertEqual(len(config.overrides["codex"]), 1)

            config = config.with_added("codex", root / ".codex-work")
            self.assertEqual(len(config.overrides["codex"]), 2)

            config = config.with_removed("codex", root / ".codex")
            self.assertEqual(
                config.overrides["codex"], (root / ".codex-work",)
            )

            config = config.without_provider("codex")
            self.assertNotIn("codex", config.overrides)

            with self.assertRaises(ScanDirsError):
                config.with_added("unknown", root / "x")


class ResolveEffectiveTests(unittest.TestCase):
    """Web 配置 > 命令行参数 > 自动探测的优先级。"""

    def test_web_override_beats_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            web_home = root / "web-codex"
            cli_home = root / "cli-codex"
            config = ScanDirsConfig(overrides={"codex": (web_home,)})

            effective = resolve_effective(config, {"codex": [cli_home]})

            state = effective.state("codex")
            self.assertEqual(state.source, "web")
            self.assertEqual(state.effective, (web_home,))
            self.assertEqual(state.cli_dirs, (cli_home,))

    def test_cli_beats_auto_detect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            default_home = root / "default-codex"
            default_home.mkdir()
            cli_home = root / "cli-codex"
            specs = {"codex": _codex_spec(default_home)}
            with patch.dict(PROVIDER_SPECS, specs):
                effective = resolve_effective(
                    ScanDirsConfig(), {"codex": [cli_home]}
                )

            state = effective.state("codex")
            self.assertEqual(state.source, "cli")
            self.assertEqual(state.effective, (cli_home,))

    def test_auto_detect_when_nothing_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            default_home = root / "default-codex"
            default_home.mkdir()
            specs = {"codex": _codex_spec(default_home)}
            with patch.dict(PROVIDER_SPECS, specs):
                effective = resolve_effective(ScanDirsConfig(), {"codex": None})

            state = effective.state("codex")
            self.assertEqual(state.source, "auto")
            self.assertEqual(state.effective, (default_home,))
            self.assertTrue(state.enabled)

    def test_empty_web_override_disables_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = ScanDirsConfig(overrides={"codex": ()})

            effective = resolve_effective(config, {"codex": [root / "cli"]})

            state = effective.state("codex")
            self.assertEqual(state.source, "web")
            self.assertEqual(state.effective, ())
            self.assertFalse(state.enabled)

    def test_unknown_state_raises(self) -> None:
        effective = EffectiveScanDirs(states=())
        with self.assertRaises(ScanDirsError):
            effective.state("codex")


class ScanDirsControllerTests(unittest.TestCase):
    """控制器的快照、修改流程和热重载回调。"""

    def _environment(
        self,
        root: Path,
    ) -> tuple[Path, Path, Path]:
        home = root / "home"
        codex = home / ".codex"
        (codex / "sessions").mkdir(parents=True)
        state_dir = root / "state"
        return home, codex, state_dir

    def test_snapshot_contains_all_providers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home, _, state_dir = self._environment(root)
            controller = ScanDirsController(
                state_dir, {"codex": None}, home_dir=home
            )

            snapshot = controller.snapshot()

            keys = [item["key"] for item in snapshot["providers"]]
            self.assertEqual(
                keys, ["codex", "claude", "commandcode", "dsh", "grok", "kimi"]
            )
            self.assertEqual(snapshot["priority"], ["web", "cli", "auto"])

    def test_add_persists_and_invokes_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home, codex, state_dir = self._environment(root)
            reloaded: list[dict[str, tuple[Path, ...]]] = []
            controller = ScanDirsController(
                state_dir,
                {"codex": None},
                reload_callback=lambda effective: reloaded.append(
                    dict(effective)
                ),
                home_dir=home,
            )

            snapshot = controller.apply("add", "codex", codex)

            codex_state = snapshot["providers"][0]
            self.assertEqual(codex_state["source"], "web")
            self.assertEqual(
                [item["path"] for item in codex_state["directories"]],
                [str(codex.resolve())],
            )
            self.assertEqual(len(reloaded), 1)
            self.assertEqual(reloaded[0]["codex"], (codex.resolve(),))
            loaded = ScanDirsConfig.load(state_dir / "scan-dirs.json")
            self.assertEqual(loaded.overrides["codex"], (codex.resolve(),))

    def test_add_invalid_path_persists_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home, _, state_dir = self._environment(root)
            controller = ScanDirsController(
                state_dir, {"codex": None}, home_dir=home
            )

            with self.assertRaises(ScanDirsError):
                controller.apply("add", "codex", Path("/etc"))

            self.assertFalse((state_dir / "scan-dirs.json").exists())

    def test_remove_then_reset_restores_cli_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home, codex, state_dir = self._environment(root)
            cli_home = home / ".codex-cli"
            controller = ScanDirsController(
                state_dir, {"codex": [cli_home]}, home_dir=home
            )

            controller.apply("add", "codex", codex)
            snapshot = controller.apply("remove", "codex", codex)
            codex_state = snapshot["providers"][0]
            self.assertEqual(codex_state["override_dirs"], [])
            self.assertFalse(codex_state["enabled"])

            snapshot = controller.apply("reset", "codex")
            codex_state = snapshot["providers"][0]
            self.assertEqual(codex_state["source"], "cli")
            self.assertEqual(codex_state["cli_dirs"], [str(cli_home)])
            self.assertIsNone(codex_state["override_dirs"])

    def test_unknown_action_and_provider_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home, codex, state_dir = self._environment(root)
            controller = ScanDirsController(
                state_dir, {"codex": None}, home_dir=home
            )

            with self.assertRaises(ScanDirsError):
                controller.apply("rename", "codex", codex)
            with self.assertRaises(ScanDirsError):
                controller.apply("add", "unknown", codex)
            with self.assertRaises(ScanDirsError):
                controller.apply("add", "codex")

    def test_corrupt_config_raises_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_dir = root / "state"
            state_dir.mkdir()
            (state_dir / "scan-dirs.json").write_text(
                "{ not json", encoding="utf-8"
            )
            with self.assertRaises(ScanDirsError):
                ScanDirsController(state_dir, {})


if __name__ == "__main__":
    unittest.main()
