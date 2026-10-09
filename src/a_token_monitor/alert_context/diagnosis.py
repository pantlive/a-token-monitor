"""告警上传原因分析：把会话日志折算成上传量，和观测到的外发流量对照。

模型 API 是无状态的：agent 每次请求都把整段对话（历史消息、工具输出、图片）重新
上传，提示缓存只省计费、不省网络流量。所以一次「异常上传」通常由两部分组成：

* **重发上下文**：时间窗内每次模型请求携带的上下文；上下文接近上限时单次就有几 MiB；
* **新增内容**：时间窗内新进入上下文的用户消息、图片和工具输出，会随下一次请求上传。

两者都解释不了观测流量时，外发多半来自 agent 启动的子进程（git push、curl、上传
脚本等，子进程流量计入父 agent）或日志里没有记录的连接。这里只做估算和归因，不读取、
不展示实际上传的内容。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..alerts import StoredAlert
from .common import _Extraction


# 中文注释：请求体是 JSON，按每 token 约 4 字节粗略折算成上传字节数（中文、
# 转义与协议开销会让实际值偏大，结论只用于判断量级和主因）。
_BYTES_PER_TOKEN = 4


# 中文注释：日志估算量达到观测峰值的这个比例，才认为日志能解释这次上传。
_EXPLAINED_RATIO = 0.3


# 中文注释：列出的最大新增内容条数。
_TOP_ITEMS = 3


# 中文注释：会把数据发到网络的行为；日志解释不了流量时，这些工具调用是首要线索。
_NETWORK_SUMMARIES = frozenset({"发送网络请求", "检索或读取网络内容", "操作本地代码仓库"})


_CONTENT_KINDS = frozenset({"user", "image", "tool", "tool_output"})


def diagnose_upload(
    alert: StoredAlert,
    extraction: _Extraction,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """归纳本次外发的主因，返回可直接 JSON 化的分析结果。

    ``events`` 是已按内容开关脱敏的事件，用于列出最大的几条新增内容。
    """

    observed = int(alert.peak_bytes or alert.bytes or 0)
    tokens = [count for _, count in extraction.requests]
    context_tokens = max(tokens, default=0)
    per_request_bytes = context_tokens * _BYTES_PER_TOKEN
    resend_bytes = sum(tokens) * _BYTES_PER_TOKEN
    new_bytes = int(extraction.input_bytes) + int(extraction.output_bytes)
    network = _network_activities(events)
    estimated = resend_bytes + new_bytes
    if estimated and estimated >= observed * _EXPLAINED_RATIO:
        cause = "context_resend" if resend_bytes >= new_bytes else "new_content"
    elif network:
        cause = "tool_network"
    else:
        cause = "unexplained"
    return {
        "cause": cause,
        "observed_bytes": observed,
        "estimated_bytes": estimated,
        "bytes_per_token": _BYTES_PER_TOKEN,
        "requests": len(tokens),
        "context_tokens": context_tokens,
        "per_request_bytes": per_request_bytes,
        "requests_to_peak": (
            -(-observed // per_request_bytes) if per_request_bytes and observed else None
        ),
        "resend_bytes": resend_bytes,
        "new_bytes": new_bytes,
        "new_before_window": extraction.fallback,
        "largest": _largest_items(events),
        "network_activities": network,
    }


def _largest_items(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """新增内容里最大的几条：图片、大文件读取、超长工具输出最常见。"""

    sized = [
        item
        for item in events
        if item.get("kind") in _CONTENT_KINDS and int(item.get("size") or 0) > 0
    ]
    sized.sort(key=lambda item: int(item.get("size") or 0), reverse=True)
    largest: list[dict[str, Any]] = []
    for item in sized[:_TOP_ITEMS]:
        activities = item.get("activities")
        summary = ""
        if isinstance(activities, list) and activities:
            first = activities[0]
            if isinstance(first, Mapping):
                summary = str(first.get("summary") or "")
        largest.append(
            {
                "t": item.get("t"),
                "kind": item.get("kind"),
                "label": item.get("label") or "",
                "summary": summary,
                "size": int(item.get("size") or 0),
            }
        )
    return largest


def _network_activities(events: Sequence[Mapping[str, Any]]) -> list[str]:
    """工具调用里与网络发送有关的行为（去重、保持出现顺序）。"""

    found: dict[str, None] = {}
    for item in events:
        # 中文注释：只看实际发起的调用；用户消息里提到「上传」不代表执行了上传。
        if item.get("kind") not in ("tool", "search"):
            continue
        activities = item.get("activities")
        if not isinstance(activities, list):
            continue
        for activity in activities:
            if not isinstance(activity, Mapping) or activity.get("phase") == "result":
                continue
            summary = str(activity.get("summary") or "")
            if summary in _NETWORK_SUMMARIES or "上传" in summary:
                found[summary] = None
    return list(found)
