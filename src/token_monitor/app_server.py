"""Codex App Server 的最小 JSON-RPC 客户端。

客户端只使用 stdio 传输：App Server 自己复用 Codex 的登录状态，监控器不
读取或记录 OAuth Token。JSON-RPC 的响应和通知均为每行一个 JSON 对象。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .quota import QuotaSnapshot, parse_rate_limits_result


class AppServerError(RuntimeError):
    """App Server 连接或 JSON-RPC 调用失败。"""


@dataclass(frozen=True)
class AppServerConfig:
    """App Server 启动配置。"""

    codex_path: str = "codex"
    request_timeout: float = 15.0
    client_name: str = "token_monitor"
    client_version: str = "0.5.0"
    codex_home: Path | None = None

    def __post_init__(self) -> None:
        """验证超时配置。"""

        if self.request_timeout <= 0:
            raise ValueError("request_timeout 必须大于 0")


NotificationHandler = Callable[[Mapping[str, Any]], None]


class AppServerClient:
    """通过 stdio 连接一个 Codex App Server。"""

    def __init__(
        self,
        config: AppServerConfig | None = None,
        logger: logging.Logger | None = None,
        notification_handler: NotificationHandler | None = None,
    ) -> None:
        self.config = config or AppServerConfig()
        self.logger = logger or logging.getLogger(__name__)
        self.notification_handler = notification_handler
        self.process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[Mapping[str, Any]] = queue.Queue()
        self._pending: dict[int, Mapping[str, Any]] = {}
        self._request_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._reader_thread: threading.Thread | None = None
        self._reader_error: str | None = None
        self._closed = False

    def start(self) -> None:
        """启动并完成 App Server 初始化握手。"""

        if self.process is not None:
            return
        command = [self.config.codex_path, "app-server", "--listen", "stdio://"]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=self._environment(),
            )
        except (FileNotFoundError, OSError) as error:
            raise AppServerError(f"无法启动 Codex App Server: {error}") from error

        self._closed = False
        self._reader_thread = threading.Thread(
            target=self._read_loop,
            name="codex-app-server-reader",
            daemon=True,
        )
        self._reader_thread.start()
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": self.config.client_name,
                        "title": "Token Monitor",
                        "version": self.config.client_version,
                    }
                },
            )
            self.notify("initialized", {})
        except AppServerError:
            # 握手失败时及时回收半启动的子进程，避免多账号模式重复泄漏进程。
            self.close()
            raise

    def close(self) -> None:
        """关闭 App Server 子进程和读取线程。"""

        self._closed = True
        process = self.process
        self.process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1)
        self._reader_thread = None

    def __enter__(self) -> "AppServerClient":
        """进入上下文并启动连接。"""

        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        """退出上下文并关闭连接。"""

        self.close()

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        """发送一个不等待响应的 JSON-RPC 通知。"""

        self._send({"method": method, "params": dict(params or {})})

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        """发送 JSON-RPC 请求并返回 result 对象。"""

        if self.process is None or self.process.poll() is not None:
            raise AppServerError("Codex App Server 尚未运行")
        wait_timeout = timeout if timeout is not None else self.config.request_timeout
        with self._request_lock:
            request_id = self._next_id
            self._next_id += 1
            self._send(
                {
                    "id": request_id,
                    "method": method,
                    "params": dict(params or {}),
                }
            )
            message = self._pending.pop(request_id, None)
            deadline = time.monotonic() + wait_timeout
            while message is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppServerError(f"App Server 请求超时: {method}")
                try:
                    message = self._messages.get(timeout=remaining)
                except queue.Empty as error:
                    raise AppServerError(f"App Server 请求超时: {method}") from error
                if "id" not in message:
                    self._dispatch_notification(message)
                    message = None
                    continue
                response_id = message.get("id")
                if response_id != request_id:
                    if isinstance(response_id, int):
                        self._pending[response_id] = message
                    message = None

            error = message.get("error")
            if isinstance(error, Mapping):
                code = error.get("code")
                detail = error.get("message", "未知错误")
                raise AppServerError(f"{method} 失败（{code}）: {detail}")
            result = message.get("result", {})
            if not isinstance(result, Mapping):
                raise AppServerError(f"{method} 返回的 result 不是对象")
            return result

    def read_rate_limits(self, now: float | None = None) -> QuotaSnapshot:
        """读取当前账户额度窗口。"""

        observed_at = now if now is not None else time.time()
        result = self.request("account/rateLimits/read")
        return parse_rate_limits_result(result, observed_at=observed_at)

    def _environment(self) -> dict[str, str] | None:
        """为当前账号构造子进程环境，隔离 ``CODEX_HOME``。"""

        if self.config.codex_home is None:
            return None
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.config.codex_home.expanduser())
        return environment

    def list_threads(
        self,
        source_kinds: Sequence[str] | None = None,
        include_archived: bool = False,
        page_size: int = 100,
    ) -> list[Mapping[str, Any]]:
        """分页读取所有来源的会话摘要。"""

        if page_size <= 0:
            raise ValueError("page_size 必须大于 0")
        kinds = list(
            source_kinds
            or (
                "cli",
                "vscode",
                "exec",
                "appServer",
                "subAgent",
                "subAgentReview",
                "subAgentCompact",
                "subAgentThreadSpawn",
                "subAgentOther",
                "unknown",
            )
        )
        cursor: str | None = None
        threads: list[Mapping[str, Any]] = []
        while True:
            params: dict[str, Any] = {
                "limit": page_size,
                "sourceKinds": kinds,
                "archived": include_archived,
            }
            if cursor is not None:
                params["cursor"] = cursor
            result = self.request("thread/list", params)
            data = result.get("data", [])
            if isinstance(data, list):
                threads.extend(item for item in data if isinstance(item, Mapping))
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        return threads

    def drain_notifications(self) -> int:
        """处理当前队列中已经到达的通知，返回处理数量。"""

        handled = 0
        while True:
            try:
                message = self._messages.get_nowait()
            except queue.Empty:
                break
            if "id" in message and isinstance(message.get("id"), int):
                self._pending[message["id"]] = message
            else:
                self._dispatch_notification(message)
            handled += 1
        return handled

    def _send(self, message: Mapping[str, Any]) -> None:
        """写入一行 JSON-RPC 消息。"""

        process = self.process
        if process is None or process.stdin is None:
            raise AppServerError("App Server 输入管道不可用")
        serialized = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                process.stdin.write(f"{serialized}\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                raise AppServerError("写入 App Server 失败") from error

    def _read_loop(self) -> None:
        """后台读取 App Server 的 JSONL 输出。"""

        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self.logger.debug("忽略 App Server 非 JSON 输出")
                    continue
                if isinstance(message, Mapping):
                    self._messages.put(message)
        except (OSError, ValueError) as error:
            self._reader_error = str(error)
        finally:
            if not self._closed:
                self._messages.put({"method": "app-server/closed", "params": {}})

    def _dispatch_notification(self, message: Mapping[str, Any]) -> None:
        """把通知交给调用方，并维护额度稀疏更新。"""

        if self.notification_handler is not None:
            self.notification_handler(message)
