"""tools.py — 跌倒响应 pack 的工具实现

工具是平台的插件：注册进 TOOLS 即可被工作流调用，调用时受 Agent 的 tools 白名单约束。
"""
from __future__ import annotations


def event_dedupe(rt, elder_id: str = ""):
    """同一会话内连续重复触发 → 合并。用运行序号而非墙钟，便于确定性复现。"""
    seq = int(rt.shared.get("seq", 0)) + 1
    rt.shared["seq"] = seq
    last = rt.shared.setdefault("last", {})
    prev = last.get(elder_id)
    last[elder_id] = seq
    found = prev is not None and (seq - prev) <= 1
    return {"found": found, "prev_seq": prev, "seq": seq}


def push(rt, text: str = ""):
    p = rt.params
    down_ms = float(p.get("network_down_ms") or 0)
    primary_down = bool(p.get("primary_down"))

    rt.add_latency(int(p.get("push_ms") or 200))
    if down_ms:
        rt.add_latency(int(down_ms))          # 退避重试的时间成本
    if primary_down:
        return {"ok": True, "channel": "fallback", "attempts": 2,
                "degraded": True, "text": text}
    return {"ok": True, "channel": "console", "attempts": 1,
            "degraded": bool(down_ms), "text": text}


def page_community(rt, contact=None):
    rt.add_latency(50)
    return {"paged": True, "contact": (contact or {}).get("name", "")}


TOOLS = {
    "event.dedupe": event_dedupe,
    "push": push,
    "page_community": page_community,
}
