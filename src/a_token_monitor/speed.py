"""每个账号的实时输出速度：TPS（token/秒）与 TTFT（首 token 延迟）。

数据只来自本地会话日志，不嗅探网络：

* **Codex rollout** 里有精确的流式窗口——``event_msg/item_completed`` 带
  ``started_at_ms`` / ``completed_at_ms``，配上 ``token_usage_record`` 的
  ``usage.output_tokens``，可以算出「回合开始 → 首个输出 item」的 TTFT 和
  「输出 token ÷ 生成窗口」的 TPS。
* **Claude Code** 的新版 transcript 会写 ``api-request``（请求开始）和 assistant
  行上的 ``requestRef``，可以按「请求开始 → 消息最后一段写完」算每条请求的 TPS；
  没有这两类记录的旧 transcript 退回「相邻消息完成时间差」的墙钟近似。
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
# 中文注释：卡片上的迷你走势条取最近多少次测量。
_HISTORY_POINTS = 12
# 中文注释：第一次见到会话文件时回读这么多尾部字节，重建最近一次 Codex 请求的
# TTFT/TPS——否则守护进程刚启动时什么都要等下一次生成才出现。
BACKFILL_BYTES = 256 * 1024
# 中文注释：一次最多读这么多新增字节（约几百行），避免异常文件拖慢状态构建。
MAX_READ_BYTES = 512 * 1024
# 中文注释：首次见到文件时回读尾部用于重建基线，只看最后这么多行。
_BACKFILL_TAIL_LINES = 200
# 中文注释：近似测量（Claude）两次消息之间可能隔着工具执行，12 秒窗口会让卡片
# 大部分时间都显示成灰色「x 秒前」；一分钟内的测量仍算「正在出 token」。
_APPROX_LIVE_SECONDS = 60.0
# 中文注释：生成窗口小于这个值直接丢弃——多半是同一时刻的重复记录。
MIN_GENERATION_SECONDS = 0.05
# 中文注释：近似速率（Claude 的请求窗口 / 消息间隔）至少要这么长的窗口，
# 否则时间戳的毫秒级抖动会把速率放大成天文数字。
_MIN_APPROX_SECONDS = 0.5
# 中文注释：记住最近多少个 Claude 请求的开始时间，供随后的 assistant 行回填。
_MAX_REQUEST_STARTS = 2000
# 中文注释：Codex rollout 里由模型生成、带输出 token 的 item 类型（统一小写比较）。
# ``Extension`` 是联网搜索这类工具产物，窗口覆盖的是搜索耗时，不能算作生成。
_CODEX_OUTPUT_ITEM_TYPES = frozenset({"agentmessage", "reasoning", "plan"})
# 中文注释：真正会触发一次新请求的 response_item——工具输出回到模型。
_CODEX_TRIGGER_RESPONSE_ITEMS = frozenset(
    {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}
)


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
        # 中文注释：近似测量（Claude 的请求窗口 / 消息间隔）的走势；卡片上的
        # 迷你条不能只依赖滚动采样点，否则这类账号的走势条永远是空的。
        self._approx_history: dict[str, deque[float]] = {}
        # 中文注释：采样账号的最近一次速率；没有它，Claude 这类 provider 只在
        # 正在出 token 的十几秒里才有数字，用户切过去看时早没了。
        self._last_live: dict[str, tuple[float, float]] = {}
        # 中文注释：Claude 这类日志只在消息完成时写一条 usage，且同一响应可能重复写；
        # 记住上一条消息的 (id, 时间, 输出 token) 用于去重和「墙钟近似速率」。
        self._last_measure: dict[str, tuple[str, float, int]] = {}
        # 中文注释：Claude 的请求窗口：api-request 的 id → 开始时间，assistant
        # 行用 requestRef 回填到对应请求。
        self._request_starts: dict[str, float] = {}
        self._approx: set[str] = set()
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
            # 中文注释：优先用账号 ID 作归组键，与会话记录、账号卡片上的身份
            # 一致；展示名只作兜底，否则同一账号会以两个键各采一次。
            account = (
                session.get("account_id")
                or session.get("account")
                or session.get("product")
            )
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
                    if product != "codex":
                        # 中文注释：回读尾部，先建立「上一条消息」基线、填一点
                        # 走势，下一条消息完成时就能立刻算出近似速率。按写入
                        # 顺序处理，api-request 先登记，随后的消息才能用上窗口。
                        for line in lines[-_BACKFILL_TAIL_LINES:]:
                            item = _load_json(line)
                            if item is None:
                                continue
                            self._observe_request(item)
                            output = _line_output_tokens(item)
                            if output > 0:
                                self._observe_measure(account, key, item, output)
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
            item_type = str(inner.get("type") or "").lower()
            if item_type == "usermessage":
                with self._lock:
                    self._last_event_at[key] = timestamp
                return
            if item_type not in _CODEX_OUTPUT_ITEM_TYPES:
                # 中文注释：工具类 item 是这一轮的产物，不是下一轮请求的触发点；
                # 用它当起点会把生成窗口压到只剩工具执行之后的几毫秒。
                return
            started = _millis(payload.get("started_at_ms"))
            completed = _millis(payload.get("completed_at_ms"))
            if started is None or completed is None or completed <= started:
                return
            with self._lock:
                windows = self._windows.setdefault(key, [])
                if not windows:
                    # 中文注释：本次请求由上一行触发事件（用户消息 / 工具输出）开始。
                    previous = self._last_event_at.get(key)
                    if previous is not None:
                        self._request_start[key] = previous
                windows.append((started, completed))
            return
        if kind == "response_item" and payload_type in _CODEX_TRIGGER_RESPONSE_ITEMS:
            with self._lock:
                self._last_event_at[key] = timestamp
            return
        if kind != "token_usage_record":
            return
        if kind == "token_usage_record":
            usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
            output = _int(usage.get("output_tokens"))
            with self._lock:
                windows = self._windows.pop(key, [])
                request_start = self._request_start.pop(key, None)
                # 中文注释：用量记录结束了这次请求；下一次请求必须有新的触发事件
                # （工具输出 / 用户消息），否则会拿上一次的旧时间当起点。
                self._last_event_at.pop(key, None)
            if output <= 0 or not windows:
                return
            window_start = min(start for start, _ in windows)
            window_end = max(end for _, end in windows)
            # 中文注释：``output_tokens`` 是整个响应（含推理）的合计，而单个 item 的
            # ``started_at_ms``/``completed_at_ms`` 往往只覆盖最后一段输出（推理 item
            # 甚至只有 1 毫秒），拿它当分母会把 TPS 放大几十倍。有触发事件时用
            # 「请求开始 → 最后一个输出 item 结束」；回读尾部缺触发事件时才退回 item 窗口。
            if request_start is not None and window_end > request_start:
                generation = window_end - request_start
            else:
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

        item = _load_json(line)
        if item is None:
            return
        if self._observe_request(item):
            # 中文注释：Claude 的 api-request 行只记录请求开始时间，本身没有用量。
            return
        output = _line_output_tokens(item)
        if output <= 0:
            return
        if self._observe_measure(account, key, item, output):
            return
        with self._lock:
            samples = self._samples.setdefault(key, deque(maxlen=256))
            previous = samples[-1][1] if samples else 0
            samples.append((moment, previous + output))
            while samples and moment - samples[0][0] > self.window_seconds:
                samples.popleft()
            if len(samples) >= 2:
                span = samples[-1][0] - samples[0][0]
                delta = samples[-1][1] - samples[0][1]
                if span > 0 and delta > 0:
                    self._last_live[account] = (moment, delta / span)

    def _observe_request(self, item: Mapping[str, Any]) -> bool:
        """记录 Claude transcript 里的请求开始时间；返回 True 表示这一行已消费。"""

        if item.get("type") != "api-request":
            return False
        ident = str(item.get("id") or "")
        at = _parse_timestamp(item.get("timestamp"))
        if ident and at is not None:
            with self._lock:
                self._request_starts[ident] = at
                if len(self._request_starts) > _MAX_REQUEST_STARTS:
                    # 中文注释：长会话会累积上千条请求，只保留最近的即可——
                    # 消息行紧跟在对应请求之后，旧请求不会再被引用。
                    for stale in list(self._request_starts)[
                        : len(self._request_starts) - _MAX_REQUEST_STARTS
                    ]:
                        self._request_starts.pop(stale, None)
        return True

    def _observe_measure(
        self,
        account: str,
        key: str,
        item: Mapping[str, Any] | None,
        output: int,
    ) -> bool:
        """按「消息完成」测量近似速率；没有消息 id/时间戳时返回 False 走累计采样。

        新版 Claude transcript 同时写了 ``api-request`` 和 assistant 行的
        ``requestRef``，这时用「请求开始 → 最后一段写完」当生成窗口，工具执行和
        等待时间不会被算进去；旧 transcript 没有这些记录，退回相邻消息完成的墙钟差。
        """

        if item is None:
            return False
        message = item.get("message") if isinstance(item.get("message"), Mapping) else {}
        ident = str(message.get("id") or item.get("requestId") or item.get("uuid") or "")
        at = _parse_timestamp(item.get("timestamp"))
        if not ident or at is None:
            return False
        request_ref = item.get("requestRef")
        with self._lock:
            previous = self._last_measure.get(key)
            self._last_measure[key] = (ident, at, output)
            request_start = (
                self._request_starts.get(request_ref)
                if isinstance(request_ref, str) and request_ref
                else None
            )
        duplicate = previous is not None and previous[0] == ident
        rate: float | None = None
        if request_start is not None:
            span = at - request_start
            if span >= _MIN_APPROX_SECONDS:
                rate = output / span
        if rate is None and previous is not None and not duplicate:
            span = at - previous[1]
            if span >= _MIN_APPROX_SECONDS:
                rate = output / span
        if rate is not None:
            with self._lock:
                # 中文注释：同一响应的多段写入会依次更新同一个测量，走势条上
                # 只保留最终值，避免一条消息塞进三四个点。
                self._last_live[account] = (at, rate)
                history = self._approx_history.setdefault(
                    account, deque(maxlen=_HISTORY_POINTS)
                )
                if duplicate and history:
                    history[-1] = round(rate, 1)
                else:
                    history.append(round(rate, 1))
                self._approx.add(account)
        return True

    # ---------------------------------------------------------------- 输出

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        """返回每个账号的速度，以及给概览 KPI 用的合计。"""

        moment = time.time() if now is None else float(now)
        with self._lock:
            offsets = dict(self._offsets)
            samples = {key: list(value) for key, value in self._samples.items()}
            responses = {key: list(value) for key, value in self._responses.items()}
            last_live = dict(self._last_live)
            approx_history_by_account = {
                key: list(value) for key, value in self._approx_history.items()
            }
        live: dict[str, float] = {}
        for key in offsets:
            account = key.split("|", 1)[0]
            points = samples.get(key) or []
            if len(points) < 2:
                continue
            # 中文注释：窗口外的采样点不算「实时」，否则停下来几分钟后还会显示成 live。
            if moment - points[-1][0] > self.window_seconds:
                continue
            span = points[-1][0] - points[0][0]
            delta = points[-1][1] - points[0][1]
            if span <= 0 or delta <= 0:
                continue
            live[account] = max(live.get(account, 0.0), delta / span)
        accounts: dict[str, dict[str, Any]] = {}
        for account in set(live) | set(responses) | set(last_live):
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
                    "history": [
                        round(item[2], 1) for item in history[-_HISTORY_POINTS:]
                    ],
                    "live": live_tps is not None and moment - at <= self.window_seconds,
                }
                if entry["live"] and live_tps is not None:
                    entry["tps_live"] = round(live_tps, 1)
            elif live_tps is not None or account in last_live:
                if live_tps is not None:
                    at_live, tps_value = moment, live_tps
                else:
                    at_live, tps_value = last_live[account]
                # 中文注释：Claude 这类按消息完成测量的账号没有滚动采样点，
                # live 只能按「最近一次测量是否够新」判断；走势条用近似历史。
                approx_history = list(approx_history_by_account.get(account) or [])
                entry = {
                    "source": "sampled",
                    "approx": True,
                    "ttft_ms": None,
                    "tps": round(tps_value, 1),
                    "output_tokens": None,
                    "updated_at": at_live,
                    "age_seconds": round(max(0.0, moment - at_live), 1),
                    "history": approx_history or _sampled_history(samples, account),
                    "live": moment - at_live <= max(
                        self.window_seconds, _APPROX_LIVE_SECONDS
                    ),
                }
                if entry["live"]:
                    entry["tps_live"] = round(tps_value, 1)
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


def _sampled_history(
    samples: Mapping[str, list[tuple[float, int]]],
    account: str,
) -> list[float]:
    """把采样点的累计值换算成相邻区间的速率，作为走势。"""

    points: list[tuple[float, int]] = []
    for key in sorted(samples):
        if key.split("|", 1)[0] == account:
            points.extend(samples[key])
    rates: list[float] = []
    ordered = sorted(points)
    for (start, first), (end, second) in zip(ordered, ordered[1:]):
        span = end - start
        if span <= 0:
            continue
        rates.append(round(max(0, second - first) / span, 1))
    return rates[-_HISTORY_POINTS:]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 1)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 1)
