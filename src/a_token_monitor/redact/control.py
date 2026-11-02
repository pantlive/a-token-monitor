"""Dashboard 设置页的脱敏管理：读状态、切换各 agent 的 hook/代理、热启停代理、编辑规则。

代理是否随 daemon 运行、监听哪个端口，保存在 ``<state_dir>/redact/web.json``，
优先于命令行的 ``--redact-proxy`` / ``--redact-proxy-port``（和扫描目录、历史数据一致：
Web 配置 > 命令行参数 > 默认值）。

写操作会改 ``~/.claude``、``~/.codex``、``~/.grok`` 下的配置文件，所以只有 Dashboard
监听本机地址时才允许；开放到局域网时设置页只读。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from .hooks import AGENTS
from .install import MODES, AgentPaths, Installer, InstallError, proxy_url
from .proxy import DEFAULT_PORT, DEFAULT_UPSTREAMS, LOOPBACK_HOSTS, RedactProxyServer
from .rules import (
    RULE_NAMES,
    RedactConfig,
    RedactStore,
    Redactor,
    _write_private_json,
    default_disabled_rules,
    summarize,
)


AGENT_LABELS = {"claude": "Claude Code", "codex": "Codex", "grok": "Grok"}
# 中文注释：设置页可编辑的规则字段；upstreams 单独用 upstream 动作改。
_CONFIG_FIELDS = (
    "disabled_rules",
    "enabled_rules",
    "extra_patterns",
    "allow_patterns",
    "literal_values",
    "dotenv_files",
    "scan_env",
    "restore",
    "prompt_action",
    "vault_ttl_days",
)
_SCAN_LIMIT = 20000


class RedactControlError(Exception):
    """请求参数不合法或当前模式不允许修改。"""


class RedactController:
    """把 ``redact`` 子命令的能力暴露给 Dashboard；所有方法线程安全。"""

    def __init__(
        self,
        state_dir: Path,
        *,
        cli_proxy: bool = False,
        cli_port: int = DEFAULT_PORT,
        writable: bool = True,
        paths: AgentPaths | None = None,
        logger: logging.Logger | None = None,
        server_factory: Callable[..., RedactProxyServer] = RedactProxyServer,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.store = RedactStore(self.state_dir)
        self.web_path = self.store.root / "web.json"
        self.cli_proxy = cli_proxy
        self.cli_port = cli_port
        self.writable = writable
        self.paths = paths or AgentPaths.default()
        self.logger = logger or logging.getLogger(__name__)
        self._server_factory = server_factory
        self._lock = threading.RLock()
        self._server: RedactProxyServer | None = None
        self._proxy_error: str | None = None

    # ----- 生效配置 -----

    def _web_settings(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.web_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            self.logger.warning("脱敏 Web 配置无法读取，已忽略: %s", self.web_path)
            return {}
        return raw if isinstance(raw, dict) else {}

    def effective_proxy(self) -> tuple[bool, int, str]:
        """返回 (是否启用, 端口, 来源)；来源是 web 或 cli。"""

        web = self._web_settings()
        if isinstance(web.get("proxy_enabled"), bool):
            port = web.get("proxy_port")
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                port = self.cli_port
            return web["proxy_enabled"], port, "web"
        return self.cli_proxy, self.cli_port, "cli"

    def _installer(self, port: int | None = None, messages: list[str] | None = None) -> Installer:
        sink = messages if messages is not None else []
        return Installer(
            self.state_dir,
            self.paths,
            port=port if port is not None else self.effective_proxy()[1],
            log=sink.append,
        )

    # ----- 代理生命周期 -----

    def start(self) -> None:
        """daemon 启动时调用：按生效配置启动代理。"""

        enabled, port, _ = self.effective_proxy()
        if enabled:
            self._start_proxy(port)

    def close(self) -> None:
        with self._lock:
            self._stop_proxy()

    def _start_proxy(self, port: int) -> None:
        with self._lock:
            if self._server is not None and self._server.server_address[1] == port:
                return
            self._stop_proxy()
            try:
                server = self._server_factory(Redactor(self.store), port=port, logger=self.logger)
            except (OSError, ValueError) as error:
                self._proxy_error = f"{type(error).__name__}: {error}"
                self.logger.error("脱敏代理启动失败: %s", self._proxy_error)
                return
            server.start_background()
            self._server = server
            self._proxy_error = None
            self.logger.info("脱敏代理已启动: http://127.0.0.1:%d/", server.server_address[1])

    def _stop_proxy(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.shutdown()
            server.server_close()
            self.logger.info("脱敏代理已停止")

    def _reload_redactor(self) -> None:
        """规则改了以后换掉运行中代理的 Redactor，不用重启。"""

        with self._lock:
            if self._server is not None:
                self._server.redactor = Redactor(self.store)

    # ----- 快照 -----

    def snapshot(self) -> dict[str, Any]:
        enabled, port, source = self.effective_proxy()
        with self._lock:
            running = self._server is not None
            error = self._proxy_error
        status = self._installer(port).status()
        try:
            config = self.store.load_config()
            config_error = None
        except ValueError as exc:
            config = RedactConfig()
            config_error = str(exc)
        agents = []
        for key in AGENTS:
            state = status[key]
            mode = "both" if state["hook"] and state["proxy"] else "hook" if state["hook"] else "proxy" if state["proxy"] else "off"
            agents.append(
                {
                    "key": key,
                    "name": AGENT_LABELS[key],
                    "mode": mode,
                    "hook": state["hook"],
                    "proxy": state["proxy"],
                    "proxy_url": proxy_url(key, port),
                }
            )
        return {
            "available": True,
            "writable": self.writable,
            "proxy": {
                "enabled": enabled,
                "running": running,
                "port": port,
                "source": source,
                "error": error,
            },
            "agents": agents,
            "upstreams": [
                {"name": name, "url": url, "builtin": name in DEFAULT_UPSTREAMS and name not in config.upstreams}
                for name, url in sorted({**DEFAULT_UPSTREAMS, **config.upstreams}.items())
            ],
            "config": {
                **{name: _jsonable(getattr(config, name)) for name in _CONFIG_FIELDS},
                "extra_patterns": [
                    {"name": name, "pattern": pattern} for name, pattern in config.extra_patterns
                ],
            },
            "config_error": config_error,
            "rules": list(RULE_NAMES),
            "default_disabled_rules": list(default_disabled_rules()),
        }

    # ----- 写操作 -----

    def apply(self, body: Mapping[str, Any]) -> dict[str, Any]:
        action = str(body.get("action") or "").strip()
        if action == "scan":
            return self._scan(body)
        if not self.writable:
            raise RedactControlError("Dashboard 开放到了非本机地址，脱敏设置只读；请在本机用命令行修改")
        with self._lock:
            messages: list[str] = []
            if action == "set-agent":
                messages = self._set_agent(body)
            elif action == "set-proxy":
                messages = self._set_proxy(body)
            elif action == "reset-proxy":
                self._require_confirm(body)
                if self.web_path.exists():
                    self.web_path.unlink()
                self._apply_proxy_state()
            elif action == "set-config":
                self._set_config(body)
            elif action == "set-upstream":
                self._set_upstream(body)
            else:
                raise RedactControlError(f"未知操作: {action or '(空)'}")
        return {"ok": True, "action": action, "messages": messages, **self.snapshot()}

    @staticmethod
    def _require_confirm(body: Mapping[str, Any]) -> None:
        if body.get("confirm") is not True:
            raise RedactControlError("该操作需要 confirm: true")

    def _set_agent(self, body: Mapping[str, Any]) -> list[str]:
        agent = body.get("agent")
        mode = body.get("mode")
        if agent not in AGENTS:
            raise RedactControlError("未知或未提供的 agent")
        if mode != "off" and mode not in MODES:
            raise RedactControlError("mode 只能是 off / hook / proxy / both")
        messages: list[str] = []
        installer = self._installer(messages=messages)
        try:
            if mode == "off":
                installer.uninstall(str(agent))
            else:
                # 中文注释：先卸再装，模式从 both 改成 hook 时代理配置也会被移除。
                installer.uninstall(str(agent))
                installer.install(str(agent), str(mode))
        except InstallError as error:
            raise RedactControlError(str(error)) from error
        self.store.load_key()
        if mode in {"proxy", "both"} and not self.effective_proxy()[0]:
            messages.append("代理当前没有运行，这个 agent 会连不上；请在上方打开「随 daemon 运行代理」。")
        return messages

    def _set_proxy(self, body: Mapping[str, Any]) -> list[str]:
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise RedactControlError("enabled 必须是布尔值")
        current_enabled, current_port, _ = self.effective_proxy()
        port = body.get("port", current_port)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise RedactControlError("port 必须是 1 到 65535 的整数")
        messages: list[str] = []
        if port != current_port:
            # 中文注释：各 agent 配置里写的是端口，改端口要同步改已装的代理地址。
            status = self._installer(current_port).status()
            moved = [agent for agent, state in status.items() if state["proxy"]]
            for agent in moved:
                mode = "both" if status[agent]["hook"] else "proxy"
                try:
                    self._installer(current_port, messages).uninstall(agent)
                    self._installer(port, messages).install(agent, mode)
                except InstallError as error:
                    raise RedactControlError(str(error)) from error
        if not enabled and current_enabled:
            using = [agent for agent, state in self._installer(port).status().items() if state["proxy"]]
            if using:
                messages.append(
                    "注意：" + " / ".join(AGENT_LABELS[agent] for agent in using)
                    + " 仍配置为走代理，代理停止后会连不上。"
                )
        self.store.ensure()
        _write_private_json(self.web_path, {"proxy_enabled": enabled, "proxy_port": port})
        self._apply_proxy_state()
        return messages

    def _apply_proxy_state(self) -> None:
        enabled, port, _ = self.effective_proxy()
        if enabled:
            self._start_proxy(port)
        else:
            self._stop_proxy()

    def _set_config(self, body: Mapping[str, Any]) -> None:
        updates = body.get("config")
        if not isinstance(updates, Mapping):
            raise RedactControlError("config 必须是对象")
        unknown = set(updates) - set(_CONFIG_FIELDS)
        if unknown:
            raise RedactControlError(f"不支持修改的字段: {', '.join(sorted(unknown))}")
        try:
            raw = json.loads(self.store.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RedactControlError(f"现有脱敏配置无法读取，未做修改: {error}") from error
        if not isinstance(raw, dict):
            raise RedactControlError("现有脱敏配置根节点不是对象，未做修改")
        merged = {**raw, **updates}
        try:
            RedactConfig.from_mapping(merged)
        except (ValueError, TypeError) as error:
            raise RedactControlError(str(error)) from error
        self.store.ensure()
        _write_private_json(self.store.config_path, merged)
        self._reload_redactor()

    def _set_upstream(self, body: Mapping[str, Any]) -> None:
        name = body.get("name")
        url = body.get("url")
        if not isinstance(name, str) or not name.strip():
            raise RedactControlError("name 必须是非空字符串")
        if url is not None and not isinstance(url, str):
            raise RedactControlError("url 必须是字符串或 null")
        try:
            self.store.save_config_upstreams({name.strip(): url.strip() if url else None})
        except ValueError as error:
            raise RedactControlError(str(error)) from error
        with self._lock:
            if self._server is not None:
                merged = dict(DEFAULT_UPSTREAMS)
                merged.update(self.store.load_config().upstreams)
                self._server.upstreams = merged
        self._reload_redactor()

    def _scan(self, body: Mapping[str, Any]) -> dict[str, Any]:
        text = body.get("text")
        if not isinstance(text, str):
            raise RedactControlError("text 必须是字符串")
        if len(text) > _SCAN_LIMIT:
            raise RedactControlError(f"测试文本最多 {_SCAN_LIMIT} 个字符")
        # 中文注释：测试不写密钥表，也不用把结果里的原值回传给页面。
        redactor = Redactor(self.store)
        redacted, findings = redactor.redact(text)
        return {
            "ok": True,
            "action": "scan",
            "redacted": redacted,
            "count": len(findings),
            "summary": summarize(findings),
            "kinds": sorted({finding.kind for finding in findings}),
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def dashboard_writable(host: str) -> bool:
    return host in LOOPBACK_HOSTS


__all__ = ["RedactControlError", "RedactController", "dashboard_writable"]
