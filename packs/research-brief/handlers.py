"""handlers.py — 调研简报 pack 的领域逻辑（故意与跌倒响应毫无关系）"""
from __future__ import annotations


def _src(rt, kind: str, points: list[str]) -> dict:
    rt.add_latency(120)
    return {"kind": kind, "points": points}


def src_policy(rt, ins):
    return _src(rt, "policy", ["政策：社区养老服务补贴", "政策：智慧健康监测试点"])


def src_stats(rt, ins):
    return _src(rt, "stats", ["数据：独居老人占比 14.2%"])


def src_case(rt, ins):
    return _src(rt, "case", ["案例：某市跌倒报警试点"])


def synth(rt, ins):
    merged = ins.get("merged") or []
    rt.add_latency(80)
    return {"text": "；".join(merged), "count": len(merged)}


def checker(rt, ins):
    text = ins.get("text") or ""
    need = int(rt.params.get("min_chars") or 10)
    force_fail = bool((rt.ctx.get("event") or {}).get("force_fail"))
    rt.add_latency(30)
    return {"ok": (len(text) >= need) and not force_fail,
            "chars": len(text), "forced": force_fail}


HANDLERS = {
    "src_policy": src_policy,
    "src_stats": src_stats,
    "src_case": src_case,
    "synth": synth,
    "checker": checker,
}
