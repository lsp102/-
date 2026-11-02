"""handlers.py — 跌倒响应 pack 的领域逻辑

这里全部是业务：规则匹配、禁忌判断、话术、外呼仿真。
平台不认识「跌倒」「抗凝药」，只负责按工作流把它们串起来。
"""
from __future__ import annotations

LEVEL_ORDER = {"low": 0, "mid": 1, "high": 2, "critical": 3}
FORBIDDEN_PHRASES = ["扶起", "抱起", "搬动", "坐起"]


# ---------------------------------------------------------------- 规则匹配

def _match(when: dict, profile: dict, event: dict) -> bool:
    for k, v in when.items():
        if k == "medications_any":
            if not set(v) & set(profile.get("medications", [])):
                return False
        elif k == "conditions_any":
            if not set(v) & set(profile.get("conditions", [])):
                return False
        elif k == "lives_alone":
            if bool(profile.get("lives_alone")) != bool(v):
                return False
        elif k == "age_gte":
            if profile.get("age", 0) < v:
                return False
        elif k == "no_family_contact":
            has = any(c.get("role") == "family" for c in profile.get("contacts", []))
            if has == bool(v):
                return False
        elif k == "confidence_gte":
            if event.get("confidence", 0) < v:
                return False
        else:
            return False
    return True


def triage(rt, ins):
    profile = ins.get("profile") or {}
    event = ins.get("event") or {}
    rules = (rt.pack.data.get("rules") or {}).get("rules") or []

    level = str(event.get("base_level") or "mid")
    forbid = False
    plan, rationale, matched = [], [], []

    for r in rules:
        if not _match(r.get("when", {}), profile, event):
            continue
        matched.append(r["id"])
        t = r.get("then", {})
        lv = t.get("level")
        if lv and LEVEL_ORDER.get(lv, 0) > LEVEL_ORDER.get(level, 0):
            level = lv
        if t.get("forbid_move"):
            forbid = True
        plan.extend(t.get("plan_add", []))
        if t.get("rationale"):
            rationale.append(t["rationale"])

    contacts = [c for c in profile.get("contacts", []) if c.get("role") != "community"]
    community = next((c for c in profile.get("contacts", [])
                      if c.get("role") == "community"), None)

    rt.add_latency(40)
    query = " ".join([
        str(profile.get("name", "")),
        *profile.get("conditions", []),
        *profile.get("medications", []),
        "跌倒", level,
    ])
    return {
        "level": level,
        "forbid_move": forbid,
        "plan": plan,
        "rationale": rationale,
        "matched": matched,
        "contacts": contacts,
        "community": community,
        "need_120": forbid or level == "critical",
        "query": query,
    }


# ---------------------------------------------------------------- 联络

def draft(rt, ins):
    """注意：这个 Agent 没有 read_profile 权限，因此不掌握用药禁忌。"""
    name = ins.get("name") or "老人"
    level = ins.get("level") or "mid"
    urgent = "系统已触发紧急响应" if level in ("critical", "high") else "检测到疑似跌倒"
    return {"text": f"{name} 可能发生跌倒，{urgent}，建议立即回电确认。建议先扶起老人并安抚。"}


def guard(rt, ins):
    """Supervisor 护栏：命中搬动禁忌时剥离危险指令。"""
    text = ins.get("text") or ""
    forbid = bool(ins.get("forbid_move"))
    hit = [p for p in FORBIDDEN_PHRASES if p in text] if forbid else []
    if not hit:
        return {"text": text, "vetoed": False}
    cleaned = text
    for p in hit:
        cleaned = cleaned.replace(f"建议先{p}老人并安抚。", "")
        cleaned = cleaned.replace(p, "（已屏蔽不当指令）")
    cleaned = cleaned.rstrip() + " ⚠ 该老人存在禁忌：禁止搬动、抱起或让其坐起。"
    return {"text": cleaned, "vetoed": True, "hit": hit, "_status": "veto"}


def call(rt, ins):
    contact = ins.get("contact") or {}
    idx = int(ins.get("index") or 0)
    ring = float(ins.get("ring") or 30.0)
    answers = ins.get("answer") or []
    answer_at = ins.get("answer_at") or []

    wants = bool(answers[idx]) if idx < len(answers) else False
    at = float(answer_at[idx]) if idx < len(answer_at) else 0.0
    answered = wants and (at <= ring)
    rt.add_latency(int((min(at, ring) if answered else ring) * 1000))

    return {
        "contact": contact.get("name", ""),
        "answered": answered,
        "expected_answer_s": at,
        "ring_s": ring,
        "overrun": bool(wants and not answered),
    }


# ---------------------------------------------------------------- 基线

def baseline_triage(rt, ins):
    profile = ins.get("profile") or {}
    contacts = [c for c in profile.get("contacts", []) if c.get("role") != "community"]
    rt.add_latency(10)
    return {"level": "mid", "forbid_move": False, "need_120": False,
            "contacts": contacts, "plan": ["通知家属"], "matched": []}


def call_first(rt, ins):
    contact = ins.get("contact") or {}
    answers = ins.get("answer") or []
    ring = float(ins.get("ring") or 30.0)
    answered = bool(answers[0]) if answers else False
    rt.add_latency(int((ring if not answered else min(ring, 6.0)) * 1000))
    return {"contact": contact.get("name", ""), "answered": answered}


def noop(rt, ins):
    return {}


HANDLERS = {
    "triage": triage,
    "draft": draft,
    "guard": guard,
    "call": call,
    "baseline_triage": baseline_triage,
    "call_first": call_first,
    "noop": noop,
}
