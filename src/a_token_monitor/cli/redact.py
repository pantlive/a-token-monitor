"""redact 子命令：脱敏代理、hook 入口、安装/卸载与规则自测。"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from ..redact.hooks import AGENTS, run_hook
from ..redact.install import MODES, Installer, InstallError, proxy_url
from ..redact.proxy import RedactProxyServer
from ..redact.rules import RedactStore, Redactor, summarize


def _redactor(args: argparse.Namespace) -> Redactor:
    return Redactor(RedactStore(args.state_dir))


def _run_redact(args: argparse.Namespace) -> int:
    action = args.redact_action
    if action == "hook":
        return run_hook(args.agent, lambda: _redactor(args))
    if action == "proxy":
        return _run_proxy(args)
    if action == "install":
        return _install(args)
    if action == "uninstall":
        return _uninstall(args)
    if action == "status":
        return _status(args)
    if action == "scan":
        return _scan(args)
    if action == "upstream":
        return _upstream(args)
    raise ValueError(f"未知的 redact 动作: {action}")


def _run_proxy(args: argparse.Namespace) -> int:
    logger = logging.getLogger("a_token_monitor.redact")
    server = RedactProxyServer(_redactor(args), host=args.host, port=args.port, logger=logger)
    host, port = str(server.server_address[0]), int(server.server_address[1])
    sys.stdout.write(f"脱敏代理已启动: http://{host}:{port}/（Ctrl-C 退出）\n")
    for name, url in sorted(server.upstreams.items()):
        sys.stdout.write(f"  /{name}/ → {url}\n")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _print_line(line: str) -> None:
    sys.stdout.write(line + "\n")


def _installer(args: argparse.Namespace) -> Installer:
    return Installer(args.state_dir, port=args.port, log=_print_line)


def _install(args: argparse.Namespace) -> int:
    chosen = {agent: getattr(args, agent) for agent in AGENTS if getattr(args, agent)}
    if not chosen:
        raise ValueError("至少指定一个 agent，例如 --claude both --codex hook --grok hook")
    installer = _installer(args)
    RedactStore(args.state_dir).load_key()
    for agent, mode in chosen.items():
        installer.install(agent, mode)
    if any(mode in {"proxy", "both"} for mode in chosen.values()):
        sys.stdout.write(
            "代理模式需要代理常驻运行：a-token-monitor redact proxy，"
            "或在 daemon / service install 加 --redact-proxy。代理没运行时这些 agent 会连不上。\n"
        )
    sys.stdout.write("重启对应的 agent 后生效。\n")
    return 0


def _uninstall(args: argparse.Namespace) -> int:
    agents = args.agents or list(AGENTS)
    unknown = [agent for agent in agents if agent not in AGENTS]
    if unknown:
        raise ValueError(f"未知的 agent: {', '.join(unknown)}（可选 claude / codex / grok）")
    installer = _installer(args)
    for agent in agents:
        installer.uninstall(agent)
    sys.stdout.write("已移除；原文件备份在 " f"{installer.backup_dir}\n")
    return 0


def _status(args: argparse.Namespace) -> int:
    status = _installer(args).status()
    if args.json:
        sys.stdout.write(json.dumps(status, ensure_ascii=False, indent=2) + "\n")
        return 0
    for agent, state in status.items():
        hook = "已安装" if state["hook"] else "未安装"
        proxy = proxy_url(agent, args.port) if state["proxy"] else "未启用"
        sys.stdout.write(f"{agent:<7} hook: {hook}  代理: {proxy}\n")
    return 0


def _scan(args: argparse.Namespace) -> int:
    redactor = _redactor(args)
    text = sys.stdin.read() if args.text in (None, "-") else args.text
    redacted, findings = redactor.redact(text)
    # 中文注释：自测命令只演示替换效果，不把映射写进密钥表。
    sys.stdout.write(redacted if redacted.endswith("\n") else redacted + "\n")
    sys.stderr.write(f"命中 {len(findings)} 处{('：' + summarize(findings)) if findings else ''}\n")
    return 1 if findings else 0


def _upstream(args: argparse.Namespace) -> int:
    store = RedactStore(args.state_dir)
    if args.upstream_action == "set":
        store.save_config_upstreams({args.name: args.url})
        sys.stdout.write(f"上游已设置: /{args.name}/ → {args.url}\n")
    elif args.upstream_action == "unset":
        store.save_config_upstreams({args.name: None})
        sys.stdout.write(f"上游已删除: {args.name}\n")
    else:
        config = store.load_config()
        from ..redact.proxy import DEFAULT_UPSTREAMS

        merged = {**DEFAULT_UPSTREAMS, **config.upstreams}
        for name, url in sorted(merged.items()):
            sys.stdout.write(f"/{name}/ → {url}\n")
    return 0


def add_redact_parser(subparsers: argparse._SubParsersAction, shared: argparse.ArgumentParser, port_type: object) -> None:
    """注册 redact 子命令及其动作。"""

    from ..redact.proxy import DEFAULT_PORT

    parser = subparsers.add_parser(
        "redact",
        parents=[shared],
        help="发给模型前脱敏密钥：本地代理与 Claude Code / Codex / Grok hook",
    )
    actions = parser.add_subparsers(dest="redact_action", required=True)

    proxy = actions.add_parser("proxy", help="在前台运行脱敏代理")
    proxy.add_argument("--host", default="127.0.0.1", help="监听地址，只允许本机（默认: 127.0.0.1）")
    proxy.add_argument("--port", type=port_type, default=DEFAULT_PORT, help=f"监听端口（默认: {DEFAULT_PORT}）")

    hook = actions.add_parser("hook", help="hook 入口：从 stdin 读事件 JSON（由 agent 调用）")
    hook.add_argument("--agent", choices=AGENTS, default=None, help="强制指定运行方（默认按事件内容识别）")

    install = actions.add_parser("install", help="把 hook 或代理写进各 agent 的用户配置")
    for agent, label in (("claude", "Claude Code"), ("codex", "Codex"), ("grok", "Grok")):
        install.add_argument(f"--{agent}", choices=MODES, default=None, help=f"{label} 用 hook、proxy 还是 both")
    install.add_argument("--port", type=port_type, default=DEFAULT_PORT, help=f"代理端口（默认: {DEFAULT_PORT}）")

    uninstall = actions.add_parser("uninstall", help="移除本工具写入的 hook 和代理配置")
    uninstall.add_argument("agents", nargs="*", metavar="AGENT", help="claude / codex / grok，默认全部")
    uninstall.add_argument("--port", type=port_type, default=DEFAULT_PORT, help=argparse.SUPPRESS)

    status = actions.add_parser("status", help="查看各 agent 的 hook 与代理安装状态")
    status.add_argument("--json", action="store_true", help="以 JSON 输出")
    status.add_argument("--port", type=port_type, default=DEFAULT_PORT, help=f"代理端口（默认: {DEFAULT_PORT}）")

    scan = actions.add_parser("scan", help="自测规则：输出脱敏后的文本，命中时退出码为 1")
    scan.add_argument("text", nargs="?", default=None, help="要检查的文本；省略或 - 时读 stdin")

    upstream = actions.add_parser("upstream", help="查看或设置代理的上游地址")
    upstream_actions = upstream.add_subparsers(dest="upstream_action", required=True)
    upstream_actions.add_parser("list", help="列出全部上游")
    upstream_set = upstream_actions.add_parser("set", help="新增或覆盖一个上游")
    upstream_set.add_argument("name", help="路径前缀，例如 magpie")
    upstream_set.add_argument("url", help="上游地址，例如 http://127.0.0.1:3425/v1")
    upstream_unset = upstream_actions.add_parser("unset", help="删除一个自定义上游")
    upstream_unset.add_argument("name", help="路径前缀")


__all__ = ["InstallError", "_run_redact", "add_redact_parser"]
