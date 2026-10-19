"""每个账号的实时输出速度：TPS（token/秒）与 TTFT（首 token 延迟）。

数据只来自本地会话日志，不嗅探网络：

* **Codex rollout** 里有精确的流式窗口——``event_msg/item_completed`` 带
  ``started_at_ms`` / ``completed_at_ms``，配上 ``token_usage_record`` 的
  ``usage.output_tokens``，可以算出「回合开始 → 首个输出 item」的 TTFT 和
  「输出 token ÷ 生成窗口」的 TPS。
* 其它 provider 的日志只有「每条消息完成时的时间戳 + token 数」，没有请求开始时刻，
  所以 TTFT 不给（显示 —）；TPS 用**新增输出 token / 时间差**的滚动速率近似。

采样器每个状态刷新被调用一次（页面上约 2 秒一次），只读文件新增的字节，代价很小。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from collections import deque
from pathlib import Path
from typing import Any, Iterable, Mapping

# 中文注释：滚动窗口与新鲜度。页面 2 秒刷新一次，窗口取 12 秒够平滑又不迟钝；
# 最后一次测量会一直保留展示（带「x 分钟前」），超过这个时长才不再计入合计。
DEFAULT_WINDOW_SECONDS = 12.0
DEFAULT_FRESH_SECONDS = 600.0
# 中文注释：第一次见到会话文件时回读这么多尾部字节，重建最近一次 Codex 请求的
# TTFT/TPS——否则守护进程刚启动时什么都要等下一次生成才出现。
BACKFILL_BYTES = 256 * 1024
# 中文注释：一次最多读这么多新增字节（约几百行），避免异常文件拖慢状态构建。
MAX_READ_BYTES = 512 * 1024
# 中文注释：生成窗口小于这个值直接丢弃——多半是同一时刻的重复记录。
MIN_GENERATION_SECONDS = 0.05


class SpeedTracker:
    """线程安全的账号速度采样器。"""

    def __init__(
        self,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        fresh_seconds: float = DEFAULT_FRESH_SECONDS,
    ) -> None:
        self.window_seconds = max(2.0, float(window_seconds))
        self.fresh_seconds = max(self.window_seconds, float(fresh_seconds))
        # 中文注释：sample_sessions 持锁期间会调用同样加锁的观察函数，必须可重入。
        self._lock = threading.RLock()
        # 中文注释：每个会话的读取进度；用 (大小, 修改时间) 判断是否追加过。
        self._offsets: dict[str, int] = {}
        self._sizes: dict[str, int] = {}
        # 中文注释：每个会话的 (时间, 累计输出 token) 采样点。
        self._samples: dict[str, deque[tuple[float, int]]] = {}
        # 中文注释：账号 → 最近若干次精确测量 (时间, ttft 秒, tps, 输出 token)。
        self._responses: dict[str, deque[tuple[float, float, float, int]]] = {}
        # 中文注释：Codex 单次请求的中间状态。一轮里可能有多条请求（工具调用后继续），
        # 所以 TTFT 从「触发本次请求的上一条事件」算起，而不是整个回合的开始。
        self._last_event_at: dict[str, float] = {}
        self._request_start: dict[str, float] = {}
        self._windows: dict[str, list[tuple[float, float]]] = {}

    # ---------------------------------------------------------------- 采样

    def sample_sessions(
        self,
        sessions: Iterable[Mapping[str, Any]],
        now: float | None = None,
    ) -> None:
        """按会话 JSONL 的新增内容推进采样；没有新内容时只做一次 stat。"""

        moment = time.time() if now is None else float(now)
        for session in sessions:
            if not isinstance(session, Mapping):
                continue
            raw_path = session.get("jsonl_path")
            account = session.get("account")
            if not raw_path or not account:
                continue
            path = Path(str(raw_path))
            key = f"{account}|{path}"
            try:
                size = path.stat().st_size
            except OSError:
                continue
            with self._lock:
                previous = self._sizes.get(key)
                if previous is None:
                    # 中文注释：第一次见到这个文件：回读尾部重建最近一次精确测量，
                    # 但不把历史内容算进「实时流量」。
                    self._sizes[key] = size
                    self._offsets[key] = max(0, size - BACKFILL_BYTES)
                    lines = self._read_new_lines(path, size, key)
                    product = str(session.get("product") or "")
                    if product == "codex":
                        # 中文注释：回读是从文件中间截断的，TTFT 需要「触发本次请求的
                        # 上一条事件」，这里可能缺失；只回填 TPS，不猜 TTFT。
                        for line in lines:
                            self._observe_codex(
                                account, key, line, moment, allow_ttft=False
                            )
                    continue
                if size <= previous:
                    continue
            lines = self._read_new_lines(path, size, key)
            with self._lock:
                self._sizes[key] = size
                product = str(session.get("product") or "")
                for line in lines:
                    if product == "codex":
                        self._observe_codex(account, key, line, moment)
                    self._observe_tokens(account, key, product, line, moment)

    def _read_new_lines(self, path: Path, size: int, key: str) -> list[str]:
        """从上次的偏移量读到文件末尾，只返回完整的行（不完整的一行下次再读）。"""

        with self._lock:
            offset = self._offsets.get(key, size)
        start = max(offset, size - MAX_READ_BYTES)
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                payload = handle.read(MAX_READ_BYTES)
        except OSError:
            return []
        end = payload.rfind(b"\n")
        if end < 0:
            return []
        chunk = payload[: end + 1]
        with self._lock:
            self._offsets[key] = start + len(chunk)
        text = chunk.decode("utf-8", errors="replace")
        return [line for line in text.splitlines() if line.strip()]

    # ---------------------------------------------------------------- Codex

    def _observe_codex(
        self,
        account: str,
        key: str,
        line: str,
        moment: float,
        *,
        allow_ttft: bool = True,
    ) -> None:
        """解析 Codex rollout 的流式窗口与用量，得到精确 TTFT / TPS。"""

        item = _load_json(line)
        if item is None:
            return
        kind = str(item.get("type") or "")
        payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
        payload_type = str(payload.get("type") or "")
        timestamp = _parse_timestamp(item.get("timestamp")) or moment
        if kind == "event_msg" and payload_type == "task_started":
            with self._lock:
                self._windows[key] = []
                self._request_start.pop(key, None)
                self._last_event_at[key] = timestamp
            return
        if kind == "event_msg" and payload_type == "item_completed":
            inner = payload.get("item") if isinstance(payload.get("item"), Mapping) else {}
            if str(inner.get("type") or "") not in {"AgentMessage", "reasoning"}:
                with self._lock:
                    self._last_event_at[key] = timestamp
                return
            started = _millis(payload.get("started_at_ms"))
            completed = _millis(payload.get("completed_at_ms"))
            if started is None or completed is None or completed <= started:
                with self._lock:
                    self._last_event_at[key] = timestamp
                return
            with self._lock:
                windows = self._windows.setdefault(key, [])
                if not windows:
                    # 中文注释：本次请求由上一行事件触发（用户消息 / 工具输出）。
                    previous = self._last_event_at.get(key)
                    if previous is not None:
                        self._request_start[key] = previous
                windows.append((started, completed))
                self._last_event_at[key] = timestamp
            return
        if kind != "token_usage_record":
            with self._lock:
                self._last_event_at[key] = timestamp
        if kind == "token_usage_record":
            usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
            output = _int(usage.get("output_tokens"))
            with self._lock:
                windows = self._windows.pop(key, [])
                request_start = self._request_start.pop(key, None)
                self._last_event_at[key] = timestamp
            if output <= 0 or not windows:
                return
            window_start = min(start for start, _ in windows)
            window_end = max(end for _, end in windows)
            generation = window_end - window_start
            if generation < MIN_GENERATION_SECONDS:
                return
            tps = output / generation
            ttft = None
            if (
                allow_ttft
                and request_start is not None
                and window_start >= request_start
            ):
                ttft = window_start - request_start
            self._record_response(account, timestamp, ttft, tps, output)

    def _record_response(
        self,
        account: str,
        at: float,
        ttft: float | None,
        tps: float,
        output: int,
    ) -> None:
        with self._lock:
            history = self._responses.setdefault(account, deque(maxlen=20))
            history.append((at, -1.0 if ttft is None else ttft, tps, output))

    def _observe_tokens(
        self,
        account: str,
        key: str,
        product: str,
        line: str,
        moment: float,
    ) -> None:
        """通用采样：取这一行的输出 token 数累加，给所有 provider 一个实时速率。"""

        output = _line_output_tokens(_load_json(line))
        if output <= 0:
            return
        with self._lock:
            samples = self._samples.setdefault(key, deque(maxlen=256))
            previous = samples[-1][1] if samples else 0
            samples.append((moment, previous + output))
            while samples and moment - samples[0][0] > self.window_seconds:
                samples.popleft()

    # ---------------------------------------------------------------- 输出

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        """返回每个账号的速度，以及给概览 KPI 用的合计。"""

        moment = time.time() if now is None else float(now)
        with self._lock:
            offsets = dict(self._offsets)
            samples = {key: list(value) for key, value in self._samples.items()}
            responses = {key: list(value) for key, value in self._responses.items()}
        live: dict[str, float] = {}
        for key in offsets:
            account = key.split("|", 1)[0]
            points = samples.get(key) or []
            if len(points) < 2:
                continue
            span = points[-1][0] - points[0][0]
            delta = points[-1][1] - points[0][1]
            if span <= 0 or delta <= 0:
                continue
            live[account] = max(live.get(account, 0.0), delta / span)
        accounts: dict[str, dict[str, Any]] = {}
        for account in set(live) | set(responses):
            history = responses.get(account) or []
            latest = max(history, key=lambda item: item[0]) if history else None
            live_tps = live.get(account)
            if latest is not None:
                at, ttft, tps, output = latest
                entry = {
                    "source": "codex-log",
                    "ttft_ms": None if ttft < 0 else round(ttft * 1000, 1),
                    "tps": round(tps, 1),
                    "output_tokens": output,
                    "updated_at": at,
                    "age_seconds": round(max(0.0, moment - at), 1),
                    "live": live_tps is not None and moment - at <= self.window_seconds,
                }
                if entry["live"] and live_tps is not None:
                    entry["tps_live"] = round(live_tps, 1)
            elif live_tps is not None:
                entry = {
                    "source": "sampled",
                    "ttft_ms": None,
                    "tps": round(live_tps, 1),
                    "tps_live": round(live_tps, 1),
                    "output_tokens": None,
                    "updated_at": moment,
                    "age_seconds": 0.0,
                    "live": True,
                }
            else:
                continue
            accounts[account] = entry
        active = [
            item
            for item in accounts.values()
            if float(item.get("age_seconds") or 0) <= self.fresh_seconds
        ]
        return {
            "sampled_at": moment,
            "window_seconds": self.window_seconds,
            "accounts": accounts,
            "totals": {
                "tps": round(sum(float(item.get("tps") or 0.0) for item in active), 1),
                "accounts": len(accounts),
                "live_accounts": len(active),
                "ttft_ms": _median(
                    [item["ttft_ms"] for item in active if item.get("ttft_ms")]
                ),
            },
        }


# ---------------------------------------------------------------- 解析工具


def _load_json(line: str) -> Mapping[str, Any] | None:
    try:
        item = json.loads(line)
    except ValueError:
        return None
    return item if isinstance(item, Mapping) else None


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _millis(value: object) -> float | None:
    """``started_at_ms`` 是毫秒时间戳；转成秒。"""

    if not isinstance(value, (int, float)):
        return None
    return float(value) / 1000.0


def _parse_timestamp(value: object) -> float | None:
    """ISO-8601（``2026-10-19T03:31:41.714Z``）转 Unix 时间。"""

    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _line_output_tokens(item: Mapping[str, Any] | None) -> int:
    """从一行日志里取「本次响应」的输出 token，跳过累计口径的字段。

    中文注释：``event_msg/token_count`` 与 ``token_usage_record`` 会记录同一份数字，
    后者更结构化；这里优先用量记录，其次消息级 usage，最后才是 last_token_usage。
    """

    if item is None:
        return 0
    kind = str(item.get("type") or "")
    payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
    payload_type = str(payload.get("type") or "")
    if kind == "event_msg" and payload_type == "token_count":
        return 0  # 与 token_usage_record 重复，避免双计
    for holder in (
        payload.get("usage"),
        (payload.get("message") or {}).get("usage") if isinstance(payload.get("message"), Mapping) else None,
        item.get("usage"),
        (item.get("message") or {}).get("usage") if isinstance(item.get("message"), Mapping) else None,
    ):
        if isinstance(holder, Mapping):
            value = _int(holder.get("output_tokens"))
            if value:
                return value
    info = payload.get("info") if isinstance(payload.get("info"), Mapping) else {}
    last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), Mapping) else None
    if last is not None:
        return _int(last.get("output_tokens"))
    return _int(payload.get("output_tokens"))


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 1)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 1)
