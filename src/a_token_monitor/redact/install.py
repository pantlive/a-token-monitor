"""把脱敏 hook 或代理写进各 agent 的用户级配置，以及卸载。

每个 agent 可以单独选 ``hook``、``proxy`` 或 ``both``：

* Claude Code：hook 写 ``~/.claude/settings.json`` 的 ``hooks``；代理写同文件 ``env`` 里的
  ``ANTHROPIC_BASE_URL``（不设凭据变量，订阅登录照常生效）。
* Codex：hook 写 ``~/.codex/hooks.json``（需要在 Codex 里 ``/hooks`` 审核信任）；代理在
  ``~/.codex/config.toml`` 顶层写 ``openai_base_url``，ChatGPT 登录的请求也走这里。
* Grok：hook 写 ``~/.grok/hooks/a-token-monitor-redact.json``；代理写 ``~/.grok/config.toml``
  的 ``[endpoints] cli_chat_proxy_base_url``。

改动前把原文件备份到 ``<state_dir>/redact/backups/``。TOML 只做按行插入，插入的行都带
``# a-token-monitor redact`` 标记，卸载时按标记删除，不碰用户自己的内容。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..local_time import to_local
from .proxy import DEFAULT_PORT


MARKER = "# a-token-monitor redact"
MODES = ("hook", "proxy", "both")
HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse")
_HOOK_TAG = " redact hook"


class InstallError(Exception):
    """配置冲突或文件无法解析。"""


@dataclass(frozen=True)
class AgentPaths:
    claude_settings: Path
    codex_hooks: Path
    codex_config: Path
    grok_hooks_dir: Path
    grok_config: Path

    @classmethod
    def default(cls, home: Path | None = None) -> AgentPaths:
        home = home or Path.home()
        return cls(
            claude_settings=home / ".claude" / "settings.json",
            codex_hooks=home / ".codex" / "hooks.json",
            codex_config=home / ".codex" / "config.toml",
            grok_hooks_dir=home / ".grok" / "hooks",
            grok_config=home / ".grok" / "config.toml",
        )

    @property
    def grok_hook_file(self) -> Path:
        return self.grok_hooks_dir / "a-token-monitor-redact.json"


def hook_command(state_dir: Path, python: str | None = None) -> str:
    """三家共用的 hook 命令：绝对路径的解释器，不依赖 PATH。"""

    interpreter = python or sys.executable
    return f'"{interpreter}" -m a_token_monitor --state-dir "{state_dir}" redact hook'


def proxy_url(agent: str, port: int = DEFAULT_PORT) -> str:
    base = f"http://127.0.0.1:{port}"
    return {
        "claude": f"{base}/claude",
        "codex": f"{base}/codex",
        "grok": f"{base}/grok/v1",
    }[agent]


class Installer:
    def __init__(
        self,
        state_dir: Path,
        paths: AgentPaths | None = None,
        *,
        port: int = DEFAULT_PORT,
        python: str | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        # 中文注释：hook 在 agent 的工作目录里运行，写进配置的状态目录必须是绝对路径。
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.paths = paths or AgentPaths.default()
        self.port = port
        self.command = hook_command(self.state_dir, python)
        self.backup_dir = self.state_dir / "redact" / "backups"
        self.log = log

    # ----- 公共入口 -----

    def install(self, agent: str, mode: str) -> None:
        if mode not in MODES:
            raise InstallError(f"未知的模式: {mode}")
        want_hook = mode in {"hook", "both"}
        want_proxy = mode in {"proxy", "both"}
        handlers = {
            "claude": (self._claude_hook, self._claude_proxy),
            "codex": (self._codex_hook, self._codex_proxy),
            "grok": (self._grok_hook, self._grok_proxy),
        }
        if agent not in handlers:
            raise InstallError(f"不支持自动安装的 agent: {agent}")
        hook_fn, proxy_fn = handlers[agent]
        # 中文注释：先检查冲突再动文件，避免装了一半。
        proxy_fn(enable=want_proxy, dry_run=True)
        hook_fn(enable=want_hook)
        proxy_fn(enable=want_proxy, dry_run=False)

    def uninstall(self, agent: str) -> None:
        handlers = {
            "claude": (self._claude_hook, self._claude_proxy),
            "codex": (self._codex_hook, self._codex_proxy),
            "grok": (self._grok_hook, self._grok_proxy),
        }
        if agent not in handlers:
            raise InstallError(f"不支持的 agent: {agent}")
        hook_fn, proxy_fn = handlers[agent]
        hook_fn(enable=False)
        proxy_fn(enable=False, dry_run=False)

    def status(self) -> dict[str, dict[str, bool]]:
        result: dict[str, dict[str, bool]] = {}
        settings = _read_json(self.paths.claude_settings)
        result["claude"] = {
            "hook": _has_hook(settings),
            "proxy": (settings.get("env") or {}).get("ANTHROPIC_BASE_URL") == proxy_url("claude", self.port),
        }
        result["codex"] = {
            "hook": _has_hook(_read_json(self.paths.codex_hooks)),
            "proxy": MARKER in _read_text(self.paths.codex_config),
        }
        result["grok"] = {
            "hook": self.paths.grok_hook_file.is_file(),
            "proxy": MARKER in _read_text(self.paths.grok_config),
        }
        return result

    # ----- 备份与写入 -----

    def _backup(self, path: Path) -> None:
        if not path.is_file():
            return
        self.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = to_local(time.time()).strftime("%Y%m%d-%H%M%S")
        target = self.backup_dir / f"{path.parent.name.lstrip('.')}-{path.name}.{stamp}"
        shutil.copy2(path, target)
        target.chmod(0o600)

    def _write_text(self, path: Path, text: str) -> None:
        self._backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        temporary = path.with_name(f".{path.name}.redact.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.chmod(mode)
        temporary.replace(path)

    def _write_json(self, path: Path, payload: Any) -> None:
        self._write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    # ----- hook 配置（Claude 与 Codex 同一格式） -----

    def _hook_groups(self) -> dict[str, list[dict[str, Any]]]:
        handler = {"type": "command", "command": self.command, "timeout": 30}
        return {
            "UserPromptSubmit": [{"hooks": [dict(handler)]}],
            "PreToolUse": [{"matcher": "*", "hooks": [dict(handler)]}],
            "PostToolUse": [{"matcher": "*", "hooks": [dict(handler)]}],
        }

    def _apply_hooks(self, payload: dict[str, Any], enable: bool) -> dict[str, Any]:
        hooks = payload.get("hooks")
        if hooks is None:
            hooks = {}
        if not isinstance(hooks, dict):
            raise InstallError("hooks 字段不是对象，无法自动修改")
        hooks = _strip_hooks(hooks)
        if enable:
            for event, groups in self._hook_groups().items():
                existing = hooks.get(event)
                if existing is not None and not isinstance(existing, list):
                    raise InstallError(f"hooks.{event} 不是列表，无法自动修改")
                hooks[event] = list(existing or []) + groups
        result = dict(payload)
        if hooks:
            result["hooks"] = hooks
        else:
            result.pop("hooks", None)
        return result

    def _claude_hook(self, enable: bool) -> None:
        path = self.paths.claude_settings
        payload = _read_json(path, strict=True)
        updated = self._apply_hooks(payload, enable)
        if updated != payload:
            self._write_json(path, updated)
            self.log(f"Claude Code hook 已{'安装' if enable else '移除'}: {path}")

    def _codex_hook(self, enable: bool) -> None:
        path = self.paths.codex_hooks
        if not path.exists() and not enable:
            return
        payload = _read_json(path, strict=True)
        updated = self._apply_hooks(payload, enable)
        if updated != payload:
            self._write_json(path, updated)
            self.log(f"Codex hook 已{'安装' if enable else '移除'}: {path}")
            if enable:
                self.log("  Codex 需要审核新 hook：启动后运行 /hooks 并信任这三条 a-token-monitor hook。")

    def _grok_hook(self, enable: bool) -> None:
        path = self.paths.grok_hook_file
        if enable:
            payload = {"hooks": self._hook_groups()}
            if _read_json(path) != payload:
                self._write_json(path, payload)
                self.log(f"Grok hook 已安装: {path}")
        elif path.exists():
            self._backup(path)
            path.unlink()
            self.log(f"Grok hook 已移除: {path}")

    # ----- 代理配置 -----

    def _claude_proxy(self, enable: bool, dry_run: bool) -> None:
        path = self.paths.claude_settings
        payload = _read_json(path, strict=True)
        env = payload.get("env") or {}
        if not isinstance(env, dict):
            raise InstallError("settings.json 的 env 不是对象，无法自动修改")
        target = proxy_url("claude", self.port)
        current = env.get("ANTHROPIC_BASE_URL")
        if enable:
            if current not in (None, "", target):
                raise InstallError(
                    f"ANTHROPIC_BASE_URL 已设为 {current}；请先把它登记为上游"
                    "（redact upstream set claude <URL>）并删掉这一项，再安装代理"
                )
            for name in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
                if env.get(name):
                    self.log(f"  注意：{name} 已设置，请求会用它而不是订阅登录。")
        if dry_run:
            return
        updated_env = dict(env)
        if enable:
            updated_env["ANTHROPIC_BASE_URL"] = target
        elif current == target:
            updated_env.pop("ANTHROPIC_BASE_URL")
        if updated_env == env:
            return
        updated = dict(payload)
        if updated_env:
            updated["env"] = updated_env
        else:
            updated.pop("env", None)
        self._write_json(path, updated)
        self.log(f"Claude Code 代理已{'启用' if enable else '移除'}: ANTHROPIC_BASE_URL={target if enable else '(未设置)'}")

    def _codex_proxy(self, enable: bool, dry_run: bool) -> None:
        path = self.paths.codex_config
        text = _read_text(path)
        clean = _strip_marked(text)
        if enable and re.search(r"(?m)^\s*openai_base_url\s*=", _top_level(clean)):
            raise InstallError("~/.codex/config.toml 已有 openai_base_url；请先把它登记为上游再删掉这一行")
        if dry_run:
            return
        updated = clean
        if enable:
            updated = _insert_top_level(updated, f'openai_base_url = "{proxy_url("codex", self.port)}" {MARKER}')
            # 中文注释：Codex 默认用 zstd 压缩请求体；标准库 3.14 之前没有 zstd，关掉更稳。
            if not _has_zstd():
                updated = _insert_in_table(updated, "features", f"enable_request_compression = false {MARKER}")
        if updated != text:
            self._write_text(path, updated)
            self.log(f"Codex 代理已{'启用' if enable else '移除'}: {path}")

    def _grok_proxy(self, enable: bool, dry_run: bool) -> None:
        path = self.paths.grok_config
        text = _read_text(path)
        clean = _strip_marked(text)
        if enable and re.search(r"(?m)^\s*cli_chat_proxy_base_url\s*=", clean):
            raise InstallError("~/.grok/config.toml 已设置 cli_chat_proxy_base_url；请先把它登记为上游再删掉这一行")
        if dry_run:
            return
        updated = clean
        if enable:
            updated = _insert_in_table(updated, "endpoints", f'cli_chat_proxy_base_url = "{proxy_url("grok", self.port)}" {MARKER}')
        if updated != text:
            self._write_text(path, updated)
            self.log(f"Grok 代理已{'启用' if enable else '移除'}: {path}")


# ----- 小工具 -----


def _has_zstd() -> bool:
    try:
        from compression import zstd  # type: ignore[import-not-found]  # noqa: F401

        return True
    except ImportError:
        return False


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _read_json(path: Path, *, strict: bool = False) -> dict[str, Any]:
    text = _read_text(path)
    if not text.strip():
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        if strict:
            raise InstallError(f"{path} 不是合法 JSON，未做修改: {error}") from error
        return {}
    if not isinstance(payload, dict):
        if strict:
            raise InstallError(f"{path} 根节点不是对象，未做修改")
        return {}
    return payload


def _is_ours(handler: Any) -> bool:
    return isinstance(handler, dict) and _HOOK_TAG in str(handler.get("command", "")) and "a_token_monitor" in str(handler.get("command", ""))


def _has_hook(payload: dict[str, Any]) -> bool:
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        return False
    return any(
        _is_ours(handler)
        for groups in hooks.values()
        if isinstance(groups, list)
        for group in groups
        if isinstance(group, dict)
        for handler in group.get("hooks") or []
    )


def _strip_hooks(hooks: dict[str, Any]) -> dict[str, Any]:
    """去掉本工具加的 handler；组里只剩它的整组删除，其他人的 hook 原样保留。"""

    result: dict[str, Any] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            result[event] = groups
            continue
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept.append(group)
                continue
            handlers = [handler for handler in group["hooks"] if not _is_ours(handler)]
            if handlers:
                kept.append({**group, "hooks": handlers})
            elif not group["hooks"]:
                kept.append(group)
        if kept:
            result[event] = kept
    return result


_TABLE_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")


def _strip_marked(text: str) -> str:
    """删掉带标记的行；本工具新建的表头只在表里没有别的键时才删。"""

    lines = [
        line
        for line in text.splitlines(keepends=True)
        if MARKER not in line or line.rstrip().endswith(f"{MARKER}-table")
    ]
    kept: list[str] = []
    for index, line in enumerate(lines):
        if line.rstrip().endswith(f"{MARKER}-table"):
            rest = lines[index + 1:]
            body = []
            for following in rest:
                if _TABLE_HEADER.match(following):
                    break
                body.append(following)
            if not any(item.strip() and not item.lstrip().startswith("#") for item in body):
                # 中文注释：去掉表头前面那行本工具加的空行。
                if kept and kept[-1].strip() == "":
                    kept.pop()
                continue
        kept.append(line)
    return "".join(kept)


def _top_level(text: str) -> str:
    output = []
    for line in text.splitlines():
        if _TABLE_HEADER.match(line):
            break
        output.append(line)
    return "\n".join(output)


def _insert_top_level(text: str, line: str) -> str:
    """顶层键必须写在第一个表头之前。"""

    return f"{line}\n{text}" if text else f"{line}\n"


def _insert_in_table(text: str, table: str, line: str) -> str:
    lines = text.splitlines(keepends=True)
    for index, raw in enumerate(lines):
        match = _TABLE_HEADER.match(raw)
        if match and match.group(1) == table and not raw.lstrip().startswith("[["):
            lines.insert(index + 1, f"{line}\n")
            return "".join(lines)
    suffix = "" if not text or text.endswith("\n") else "\n"
    return f"{text}{suffix}\n[{table}] {MARKER}-table\n{line}\n"
