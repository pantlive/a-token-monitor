"""本地脱敏代理：请求正文脱敏后转发上游，响应里的占位符流式还原。

路径的第一段选上游，其余原样拼到上游地址后面::

    http://127.0.0.1:8790/claude/v1/messages  →  https://api.anthropic.com/v1/messages
    http://127.0.0.1:8790/codex/responses     →  https://chatgpt.com/backend-api/codex/responses
    http://127.0.0.1:8790/grok/v1/responses   →  https://cli-chat-proxy.grok.com/v1/responses

请求头（包括订阅登录的 Authorization）原样转发，代理自己不保存也不添加任何凭据。
只在本机地址监听，并拒绝浏览器发来的请求（带 ``Origin`` 或 Host 不是本机），
避免网页借 DNS rebinding 让代理替它还原占位符。
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .rules import Finding, Redactor, summarize
from .streaming import SseRestorer, iter_sse_events, restore_json_body


DEFAULT_PORT = 8790
DEFAULT_UPSTREAMS: Mapping[str, str] = {
    "claude": "https://api.anthropic.com",
    "codex": "https://chatgpt.com/backend-api/codex",
    "grok": "https://cli-chat-proxy.grok.com",
}
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_UPSTREAM_TIMEOUT = 600.0
_MAX_BODY = 64 * 1024 * 1024

# 中文注释：逐跳头不能转发；content-length 按脱敏后的正文重算，accept-encoding 固定为
# identity，响应才能逐行解析。
_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "accept-encoding",
    }
)
# 中文注释：请求里这些字段不脱敏：思考块带签名，改了上游会拒绝；base64 图片误报多且无意义。
_REQUEST_SKIP_KEYS = frozenset({"signature", "encrypted_content", "data", "image_url", "file_data"})
_REQUEST_SKIP_TYPES = frozenset({"thinking", "redacted_thinking", "reasoning", "image", "input_image", "document"})


class ProxyError(Exception):
    """代理配置错误（监听地址、上游地址）。"""


def _decode_request_body(body: bytes, encoding: str) -> bytes:
    """解开请求压缩；Codex 默认用 zstd 压缩请求。解不开就抛错，宁可失败也不发出原文。"""

    encoding = encoding.strip().lower()
    if not encoding or encoding == "identity":
        return body
    if encoding == "gzip":
        import gzip

        return gzip.decompress(body)
    if encoding == "deflate":
        import zlib

        return zlib.decompress(body)
    if encoding == "zstd":
        try:
            from compression import zstd  # type: ignore[import-not-found]

            return bytes(zstd.decompress(body))
        except ImportError:
            pass
        try:
            import zstandard  # type: ignore[import-not-found,unused-ignore]

            return bytes(zstandard.ZstdDecompressor().decompressobj().decompress(body))
        except ImportError as error:
            raise ValueError(
                "请求使用 zstd 压缩，但当前 Python 没有 zstd 支持；"
                "请在 Codex 配置里设置 features.enable_request_compression = false"
            ) from error
    raise ValueError(f"不支持的请求压缩: {encoding}")


def redact_request_payload(redactor: Redactor, payload: Any) -> tuple[Any, list[Finding]]:
    """递归脱敏请求 JSON，跳过思考块、签名和二进制附件。"""

    findings: list[Finding] = []

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            redacted, hits = redactor.redact(node)
            findings.extend(hits)
            return redacted
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            kind = node.get("type")
            if isinstance(kind, str) and kind in _REQUEST_SKIP_TYPES:
                return node
            return {
                key: (item if key in _REQUEST_SKIP_KEYS else walk(item))
                for key, item in node.items()
            }
        return node

    return walk(payload), findings


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: RedactProxyServer

    # ----- 入口 -----

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        self.server.logger.debug("%s - %s", self.address_string(), format % args)

    # ----- 主流程 -----

    def _reply_error(self, status: int, message: str) -> None:
        body = json.dumps({"error": {"type": "redact_proxy_error", "message": message}}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip()
        if host.startswith("["):
            name = host[1:].split("]", 1)[0]
        else:
            name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return name.lower() in LOOPBACK_HOSTS

    def _handle(self) -> None:
        if self.headers.get("Origin") or not self._host_allowed():
            self._reply_error(403, "只接受本机 agent 的请求")
            return
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            # 中文注释：Codex 先试 WebSocket，失败后改用 HTTP SSE；WebSocket 帧无法逐条脱敏。
            self._reply_error(426, "脱敏代理不支持 WebSocket，请改用 HTTP")
            return
        name, _, rest = self.path.lstrip("/").partition("/")
        upstream = self.server.upstreams.get(name)
        if upstream is None:
            self._reply_error(404, f"未知的上游: {name or '(空)'}")
            return
        try:
            body = self._read_body()
        except ValueError as error:
            self._reply_error(400, str(error))
            return
        try:
            body, findings = self._redact_body(body)
        except ValueError as error:
            self._reply_error(415, str(error))
            return
        if findings:
            self.server.logger.info(
                "%s %s 脱敏 %d 处: %s", self.command, name, len(findings), summarize(findings)
            )
        self.server.redactor.flush()
        self._forward(upstream, "/" + rest.split("?", 1)[0] if rest else "", body)

    def _read_body(self) -> bytes:
        length_header = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = io.BytesIO()
            while True:
                size_line = self.rfile.readline(65537)
                size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                        pass
                    break
                chunks.write(self.rfile.read(size))
                self.rfile.readline(3)
                if chunks.tell() > _MAX_BODY:
                    raise ValueError("请求体过大")
            return chunks.getvalue()
        if not length_header:
            return b""
        length = int(length_header)
        if length < 0 or length > _MAX_BODY:
            raise ValueError("请求体过大")
        return self.rfile.read(length)

    def _redact_body(self, body: bytes) -> tuple[bytes, list[Finding]]:
        if not body:
            return body, []
        encoding = self.headers.get("Content-Encoding", "")
        decoded = _decode_request_body(body, encoding)
        content_type = (self.headers.get("Content-Type") or "").lower()
        if "json" in content_type or decoded[:1] in (b"{", b"["):
            try:
                payload = json.loads(decoded)
            except (ValueError, UnicodeDecodeError):
                payload = None
            if payload is not None:
                redacted, findings = redact_request_payload(self.server.redactor, payload)
                if not findings:
                    return decoded, []
                return json.dumps(redacted, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), findings
        try:
            text = decoded.decode("utf-8")
        except UnicodeDecodeError:
            return decoded, []
        redacted_text, findings = self.server.redactor.redact(text)
        return redacted_text.encode("utf-8"), findings

    def _forward(self, upstream: str, path: str, body: bytes) -> None:
        parts = urlsplit(upstream)
        target = (parts.path.rstrip("/") + path) or "/"
        query = urlsplit(self.path).query
        if query:
            target = f"{target}?{query}"
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_HEADERS and key.lower() != "content-encoding"
        }
        headers["Host"] = parts.netloc
        headers["Accept-Encoding"] = "identity"
        if body or self.command in {"POST", "PUT", "PATCH"}:
            headers["Content-Length"] = str(len(body))
        connection = self.server.open_connection(parts.scheme, parts.netloc)
        try:
            connection.request(self.command, target, body=body or None, headers=headers)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            self.server.logger.warning("上游请求失败 %s: %s", parts.netloc, type(error).__name__)
            self._reply_error(502, f"上游请求失败: {type(error).__name__}")
            return
        try:
            self._relay(response)
        finally:
            connection.close()

    def _is_event_stream(self, response: http.client.HTTPResponse, content_type: str) -> bool:
        """ChatGPT 后端的 Codex 流不带 Content-Type，按请求的 Accept 和正文开头一起判断。"""

        if "text/event-stream" in content_type:
            return True
        if content_type or response.status >= 300:
            return False
        if "text/event-stream" not in (self.headers.get("Accept") or "").lower():
            return False
        head = response.peek(16) if hasattr(response, "peek") else b""
        return not head or head.lstrip().startswith((b"event:", b"data:", b":", b"id:", b"retry:"))

    def _relay(self, response: http.client.HTTPResponse) -> None:
        content_type = (response.getheader("Content-Type") or "").lower()
        encoded = (response.getheader("Content-Encoding") or "identity").lower() != "identity"
        redactor = self.server.redactor
        self.send_response(response.status, response.reason)
        for key, value in response.getheaders():
            if key.lower() in _HOP_HEADERS or key.lower() == "content-length":
                continue
            self.send_header(key, value)
        if not encoded and self._is_event_stream(response, content_type):
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            restorer = SseRestorer(redactor)
            try:
                for item in iter_sse_events(iter(response.readline, b"")):
                    self._write_chunk(restorer.feed(item))
                self._write_chunk(restorer.finish())
                self._write_chunk(b"", final=True)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        raw = response.read()
        if "json" in content_type and not encoded and redactor.config.restore:
            raw = restore_json_body(redactor, raw)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _write_chunk(self, data: bytes, *, final: bool = False) -> None:
        if data:
            self.wfile.write(f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n")
        if final:
            self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class RedactProxyServer(ThreadingHTTPServer):
    """监听本机端口的脱敏代理。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        redactor: Redactor,
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        upstreams: Mapping[str, str] | None = None,
        logger: logging.Logger | None = None,
        connection_factory: Callable[[str, str], http.client.HTTPConnection] | None = None,
    ) -> None:
        if host not in LOOPBACK_HOSTS:
            raise ProxyError("脱敏代理只能监听本机地址（127.0.0.1 / ::1 / localhost）")
        merged = dict(DEFAULT_UPSTREAMS)
        merged.update(redactor.config.upstreams)
        merged.update(upstreams or {})
        for name, url in merged.items():
            if urlsplit(url).scheme not in {"http", "https"}:
                raise ProxyError(f"上游地址必须是 http(s) URL: {name}")
        self.upstreams = merged
        self.redactor = redactor
        self.logger = logger or logging.getLogger(__name__)
        self._connection_factory = connection_factory
        self._ssl_context = ssl.create_default_context()
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__((host, port), _Handler)

    def open_connection(self, scheme: str, netloc: str) -> http.client.HTTPConnection:
        if self._connection_factory is not None:
            return self._connection_factory(scheme, netloc)
        if scheme == "https":
            return http.client.HTTPSConnection(netloc, timeout=_UPSTREAM_TIMEOUT, context=self._ssl_context)
        return http.client.HTTPConnection(netloc, timeout=_UPSTREAM_TIMEOUT)

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name="redact-proxy", daemon=True)
        thread.start()
        return thread
