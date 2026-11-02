"""Claude Code / Codex / Grok 的 hook 入口：``a-token-monitor redact hook``。

一个命令处理三个事件，按 stdin 里的事件名分派：

* ``UserPromptSubmit``：提示词里有密钥时拦下（三家都不支持改写提示词）；
* ``PreToolUse``：工具参数里有占位符时还原成原值，命令和写入的文件拿到的是真实值；
* ``PostToolUse``：工具输出里的密钥换成占位符后再交给模型。

各家能力不同：Claude Code 与 Grok 用 ``updatedToolOutput`` 替换模型看到的输出；
Codex 不支持替换，只能用 ``decision: block`` 让脱敏后的文本代替原输出。
Grok 会兼容加载 ``~/.claude/settings.json`` 里的 hook，所以这里按负载形状识别
运行方：带 camelCase ``hookEventName`` 的是 Grok。

hook 失败时一律放行（exit 0、无输出），不让脱敏问题卡住 agent；错误写到 stderr。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping, TextIO

from .rules import Finding, Redactor, summarize


AGENTS = ("claude", "codex", "grok")

_EVENT_ALIASES = {
    "user_prompt_submit": "UserPromptSubmit",
    "pre_tool_use": "PreToolUse",
    "post_tool_use": "PostToolUse",
    "beforeSubmitPrompt": "UserPromptSubmit",
    "preToolUse": "PreToolUse",
    "postToolUse": "PostToolUse",
}


def _detect_agent(payload: Mapping[str, Any], default: str | None) -> str:
    """按负载形状识别运行方，三家共用同一条 hook 命令。

    同一条命令字符串让 Grok 兼容加载 ``~/.claude/settings.json`` 时能和它自己的 hook
    去重，不会对同一次工具调用跑两遍。
    """

    if default:
        return default
    # 中文注释：Grok 同时发 camelCase 的 hookEventName（snake_case 值）和 Claude 风格的
    # hook_event_name；Codex 带 turn_id；剩下的是 Claude Code。
    if "hookEventName" in payload or "workspaceRoot" in payload:
        return "grok"
    if "turn_id" in payload:
        return "codex"
    return "claude"


def _event_name(payload: Mapping[str, Any]) -> str:
    for key in ("hook_event_name", "hookEventName"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return _EVENT_ALIASES.get(value, value)
    return ""


def _block_reason(findings: list[Finding]) -> str:
    return (
        f"a-token-monitor 发现提示词里有疑似密钥（{summarize(findings)}），已拦下，没有发给模型。"
        "请删掉密钥后重发，或改用环境变量 / 文件路径引用它。"
    )


def handle_event(payload: Mapping[str, Any], redactor: Redactor, agent: str | None = None) -> dict[str, Any] | None:
    """处理一个 hook 事件，返回要写到 stdout 的 JSON；``None`` 表示不干预。"""

    detected = _detect_agent(payload, agent)
    event = _event_name(payload)
    if event == "UserPromptSubmit":
        return _on_prompt(payload, redactor, detected)
    if event == "PreToolUse":
        return _on_pre_tool(payload, redactor, detected)
    if event == "PostToolUse":
        return _on_post_tool(payload, redactor, detected)
    return None


def _on_prompt(payload: Mapping[str, Any], redactor: Redactor, agent: str) -> dict[str, Any] | None:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        prompt = payload.get("userPrompt") if isinstance(payload.get("userPrompt"), str) else ""
    text = str(prompt)
    _, findings = redactor.redact(text)
    if not findings:
        return None
    action = "warn" if redactor.config.prompt_action == "warn" else "block"
    redactor.record_event(
        agent=agent,
        channel="prompt",
        action=action,
        findings=findings,
        payload_bytes=len(text.encode("utf-8")),
    )
    reason = _block_reason(findings)
    if action == "warn":
        return {"systemMessage": f"a-token-monitor: 提示词里有疑似密钥（{summarize(findings)}），已按配置放行。"}
    result: dict[str, Any] = {"decision": "block", "reason": reason}
    if agent == "claude":
        result["hookSpecificOutput"] = {
            "hookEventName": "UserPromptSubmit",
            "suppressOriginalPrompt": True,
        }
    return result


def _tool_input(payload: Mapping[str, Any]) -> Any:
    for key in ("tool_input", "toolInput"):
        if key in payload:
            return payload[key]
    return None


def _tool_output(payload: Mapping[str, Any]) -> tuple[str | None, Any]:
    for key in ("toolResult", "tool_response", "tool_result"):
        if key in payload:
            return key, payload[key]
    return None, None


def _on_pre_tool(payload: Mapping[str, Any], redactor: Redactor, agent: str) -> dict[str, Any] | None:
    tool_input = _tool_input(payload)
    if not isinstance(tool_input, dict) or not redactor.config.restore:
        return None
    restored = redactor.restore_json(tool_input)
    if restored == tool_input:
        return None
    output: dict[str, Any] = {"hookEventName": "PreToolUse", "updatedInput": restored}
    if agent == "claude":
        # 中文注释：Claude 的 updatedInput 要配 allow 或 ask。allow 会跳过权限确认，
        # 等于替用户批准，所以除非用户本来就开着 bypassPermissions，一律用 ask。
        mode = payload.get("permission_mode")
        output["permissionDecision"] = "allow" if mode == "bypassPermissions" else "ask"
        output["permissionDecisionReason"] = "a-token-monitor 已把参数里的密钥占位符还原成原值"
    elif agent == "codex":
        # 中文注释：Codex 只接受 allow + updatedInput；它不绕过沙箱和审批（那是 PermissionRequest）。
        output["permissionDecision"] = "allow"
    # 中文注释：Grok 省略 decision 即「放行并应用改写」，且不会替用户自动批准。
    return {"hookSpecificOutput": output}


def _model_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def _on_post_tool(payload: Mapping[str, Any], redactor: Redactor, agent: str) -> dict[str, Any] | None:
    key, output = _tool_output(payload)
    if key is None:
        return None
    redacted, findings = redactor.redact_json(output)
    if not findings:
        return None
    redactor.record_event(
        agent=agent,
        channel="tool_output",
        action="redact",
        findings=findings,
        payload_bytes=len(_model_text(output).encode("utf-8")),
    )
    note = f"a-token-monitor 已把工具输出里的 {len(findings)} 处疑似密钥换成占位符（{summarize(findings)}）。"
    if agent == "codex":
        # 中文注释：Codex 不能替换输出，block 会用 reason 代替原工具结果交给模型。
        return {
            "decision": "block",
            "reason": f"{note}脱敏后的输出：\n{_model_text(redacted)}",
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": redacted,
            "additionalContext": note + "占位符形如 [[SECRET:类别:编号]]，在命令或文件里照原样使用即可，执行前会自动还原。",
        }
    }


def run_hook(agent: str | None, redactor_factory: Any, stdin: TextIO | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """读 stdin、写 stdout；任何异常都放行，并把类型写到 stderr。"""

    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    try:
        raw = stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
        redactor = redactor_factory()
        result = handle_event(payload, redactor, agent)
        redactor.flush()
    except Exception as error:  # noqa: BLE001 - hook 必须失败放行
        stderr.write(f"a-token-monitor redact hook 出错，已放行: {type(error).__name__}: {error}\n")
        return 0
    if result is not None:
        stdout.write(json.dumps(result, ensure_ascii=False))
        stdout.write("\n")
    return 0
