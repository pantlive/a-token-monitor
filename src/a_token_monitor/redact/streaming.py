"""把模型响应里的占位符还原成原值：SSE 流与普通 JSON 响应。

三种上游协议：

* Anthropic Messages：``content_block_delta`` 里的 ``text_delta`` / ``input_json_delta``；
* OpenAI Responses（Codex、Grok）：``response.*.delta`` 事件和随后的 ``.done`` 全量事件；
* Chat Completions（dsh 等）：``choices[].delta.content`` 与 ``tool_calls[].function.arguments``。

占位符可能被切在两个增量之间。遇到「末尾像半个占位符」的增量时先扣住整条事件，
和同一位置的下一条增量合并后再发；内容块结束前把扣住的事件原样放出，不会丢字。
思考过程（thinking / reasoning）不还原：Anthropic 会校验 thinking 块的签名，
客户端下一轮原样发回时内容必须和模型生成的一致。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from .rules import Redactor, split_partial_tail


# 中文注释：这些字段不还原。thinking/signature/encrypted_content 是带签名或加密的思考内容；
# summary 是 Responses 推理摘要，和 thinking 一样保持模型原文。
RESTORE_SKIP_KEYS = frozenset({"thinking", "signature", "encrypted_content", "summary"})
# 中文注释：值本身是 JSON 文本的字段（工具参数），插入原值时要做 JSON 转义。
JSON_STRING_KEYS = frozenset({"arguments", "partial_json"})


def _restore_value(redactor: Redactor, node: Any, key: str | None = None) -> Any:
    if isinstance(node, str):
        return redactor.restore(node, json_escape=key in JSON_STRING_KEYS)
    if isinstance(node, list):
        return [_restore_value(redactor, item, key) for item in node]
    if isinstance(node, dict):
        kind = node.get("type")
        if isinstance(kind, str) and kind in {"reasoning", "thinking", "redacted_thinking"}:
            return node
        return {
            name: (item if name in RESTORE_SKIP_KEYS else _restore_value(redactor, item, name))
            for name, item in node.items()
        }
    return node


def restore_json_body(redactor: Redactor, body: bytes) -> bytes:
    """非流式响应：整份 JSON 还原；不是 JSON 时原样返回。"""

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body
    return json.dumps(_restore_value(redactor, payload), ensure_ascii=False).encode("utf-8")


@dataclass
class _SseEvent:
    """一条 SSE 事件：保留 ``event:``、``id:`` 等行，``data`` 单独解析。"""

    other_lines: list[str] = field(default_factory=list)
    data_lines: list[str] = field(default_factory=list)

    def data_text(self) -> str:
        return "\n".join(self.data_lines)

    def render(self, data: str | None = None) -> bytes:
        lines = list(self.other_lines)
        text = self.data_text() if data is None else data
        if self.data_lines or data is not None:
            lines.extend(f"data: {line}" for line in text.split("\n"))
        return ("\n".join(lines) + "\n\n").encode("utf-8")


def iter_sse_events(lines: Iterable[bytes]) -> Iterator[_SseEvent | bytes]:
    """把逐行读到的字节流切成事件；无法识别的内容按原字节透传。"""

    current = _SseEvent()
    has_content = False
    for raw in lines:
        try:
            line = raw.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            if has_content:
                yield current
                current, has_content = _SseEvent(), False
            yield raw
            continue
        if line == "":
            if has_content:
                yield current
            current, has_content = _SseEvent(), False
            continue
        has_content = True
        if line.startswith("data:"):
            value = line[5:]
            current.data_lines.append(value[1:] if value.startswith(" ") else value)
        else:
            current.other_lines.append(line)
    if has_content:
        yield current


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass
class _Held:
    event: _SseEvent
    payload: dict[str, Any]
    text: str
    path: list[str]
    is_json: bool


class SseRestorer:
    """逐事件还原 SSE；``feed`` 返回本次可以发给客户端的字节。"""

    def __init__(self, redactor: Redactor) -> None:
        self.redactor = redactor
        self._held: dict[tuple[Any, ...], _Held] = {}
        # 中文注释：Chat Completions 的待补尾巴，键为 (choice, 字段, tool index)。
        self._chat_tail: dict[tuple[Any, ...], str] = {}

    # ----- 公共入口 -----

    def feed(self, item: _SseEvent | bytes) -> bytes:
        if isinstance(item, bytes):
            return self.flush() + item
        text = item.data_text()
        if not text:
            return self.flush() + item.render()
        if text.strip() == "[DONE]":
            return self.flush() + self._flush_chat(None) + item.render()
        try:
            payload = json.loads(text)
        except ValueError:
            return self.flush() + item.render()
        if not isinstance(payload, dict):
            return self.flush() + item.render()
        if "choices" in payload and isinstance(payload.get("choices"), list):
            return self.flush() + self._chat(item, payload)
        delta = self._delta_target(payload)
        if delta is not None:
            return self._delta(item, payload, *delta)
        kind = payload.get("type")
        if isinstance(kind, str) and ("reasoning" in kind or "thinking" in kind):
            # 中文注释：推理过程保持模型原文（见模块说明）。
            return self.flush() + item.render()
        restored = _restore_value(self.redactor, payload)
        return self.flush() + item.render(_dumps(restored))

    def flush(self) -> bytes:
        """放出所有扣住的事件（内容块结束、流结束或遇到其它事件时）。"""

        if not self._held:
            return b""
        output = b""
        for held in self._held.values():
            output += self._render_held(held)
        self._held.clear()
        return output

    def finish(self) -> bytes:
        return self.flush() + self._flush_chat(None)

    # ----- Anthropic / Responses 增量 -----

    def _delta_target(self, payload: dict[str, Any]) -> tuple[tuple[Any, ...], list[str], bool] | None:
        """识别需要还原的增量事件：返回 (位置键, 文本字段路径, 是否 JSON 片段)。"""

        kind = payload.get("type")
        if kind == "content_block_delta":
            delta = payload.get("delta")
            if not isinstance(delta, dict):
                return None
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                return ("anthropic", payload.get("index")), ["delta", "text"], False
            if delta.get("type") == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                return ("anthropic", payload.get("index")), ["delta", "partial_json"], True
            return None
        if isinstance(kind, str) and kind.startswith("response.") and kind.endswith(".delta"):
            if "reasoning" in kind or not isinstance(payload.get("delta"), str):
                return None
            key = (
                "responses",
                kind,
                payload.get("item_id"),
                payload.get("output_index"),
                payload.get("content_index"),
            )
            return key, ["delta"], "arguments" in kind
        return None

    @staticmethod
    def _get(payload: dict[str, Any], path: list[str]) -> str:
        node: Any = payload
        for name in path:
            node = node[name]
        return str(node)

    @staticmethod
    def _set(payload: dict[str, Any], path: list[str], value: str) -> dict[str, Any]:
        copy = json.loads(json.dumps(payload))
        node = copy
        for name in path[:-1]:
            node = node[name]
        node[path[-1]] = value
        return copy

    def _delta(self, item: _SseEvent, payload: dict[str, Any], key: tuple[Any, ...], path: list[str], is_json: bool) -> bytes:
        output = b""
        # 中文注释：别的位置的增量插进来了，先把那些位置扣住的事件放掉，保持顺序。
        for other in [name for name in self._held if name != key]:
            output += self._render_held(self._held.pop(other))
        text = self._get(payload, path)
        held = self._held.pop(key, None)
        if held is not None:
            item, payload, text = held.event, held.payload, held.text + text
        restored = self.redactor.restore(text, json_escape=is_json)
        _, tail = split_partial_tail(restored)
        if tail and self.redactor.config.restore:
            self._held[key] = _Held(item, payload, text, path, is_json)
            return output
        return output + item.render(_dumps(self._set(payload, path, restored)))

    def _render_held(self, held: _Held) -> bytes:
        restored = self.redactor.restore(held.text, json_escape=held.is_json)
        return held.event.render(_dumps(self._set(held.payload, held.path, restored)))

    # ----- Chat Completions -----

    def _chat(self, item: _SseEvent, payload: dict[str, Any]) -> bytes:
        payload = json.loads(json.dumps(payload))
        finishing: list[Any] = []
        for choice in payload["choices"]:
            if not isinstance(choice, dict):
                continue
            index = choice.get("index", 0)
            delta = choice.get("delta")
            if isinstance(delta, dict):
                if isinstance(delta.get("content"), str):
                    delta["content"] = self._chat_text((index, "content", None), delta["content"], False)
                for call in delta.get("tool_calls") or []:
                    function = call.get("function") if isinstance(call, dict) else None
                    if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                        function["arguments"] = self._chat_text(
                            (index, "arguments", call.get("index", 0)), function["arguments"], True
                        )
            message = choice.get("message")
            if isinstance(message, dict):
                choice["message"] = _restore_value(self.redactor, message)
            if choice.get("finish_reason"):
                finishing.append(index)
        for index in finishing:
            self._append_chat_tails(payload, index)
        return item.render(_dumps(payload))

    def _chat_text(self, key: tuple[Any, ...], text: str, is_json: bool) -> str:
        text = self._chat_tail.pop(key, "") + text
        restored = self.redactor.restore(text, json_escape=is_json)
        head, tail = split_partial_tail(restored)
        if tail and self.redactor.config.restore:
            self._chat_tail[key] = tail
            return head
        return restored

    def _append_chat_tails(self, payload: dict[str, Any], index: Any) -> None:
        for key in [name for name in self._chat_tail if name[0] == index]:
            tail = self._chat_tail.pop(key)
            for choice in payload["choices"]:
                if choice.get("index", 0) != index:
                    continue
                delta = choice.setdefault("delta", {})
                if key[1] == "content":
                    delta["content"] = (delta.get("content") or "") + tail
                else:
                    calls = delta.setdefault("tool_calls", [])
                    calls.append({"index": key[2], "function": {"arguments": tail}})

    def _flush_chat(self, index: Any) -> bytes:
        if not self._chat_tail:
            return b""
        payload: dict[str, Any] = {"choices": []}
        for key, tail in list(self._chat_tail.items()):
            if index is not None and key[0] != index:
                continue
            delta: dict[str, Any] = (
                {"content": tail}
                if key[1] == "content"
                else {"tool_calls": [{"index": key[2], "function": {"arguments": tail}}]}
            )
            payload["choices"].append({"index": key[0], "delta": delta})
            del self._chat_tail[key]
        if not payload["choices"]:
            return b""
        return _SseEvent().render(_dumps(payload))
