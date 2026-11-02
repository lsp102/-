"""handlers.py — 跌倒响应 pack 的领域逻辑

这里全部是业务：规则匹配、禁忌判断、话术、外呼仿真。
平台不认识「跌倒」「抗凝药」，只负责按工作流把它们串起来。
"""
from __future__ import annotations

LEVEL_ORDER = {"low": 0, "mid": 1, "high": 2, "critical": 3}
FORBIDDEN_PHRASES = ["扶起", "抱起", "搬动", "坐起"]


# ---------------------------------------------------------------- 跌倒核验

def _num(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


_SIG_FIELD = {
    "impact_g_gte": "impact_g",
    "impact_g_lt": "impact_g",
    "freefall_ms_lt": "freefall_ms",
    "still_ms_lt": "still_ms",
    "orientation_deg_lt": "orientation_delta_deg",
}


def _hard_hit(when: dict, sig: dict, ev: dict) -> bool:
    """硬否决条件：任一谓词不满足即不命中。

    还要求事件里真的带了对应传感器字段——老场景只有 confidence，
    缺字段被当成 0 会让"坐下"这类否决条件误命中。
    """
    for k, v in when.items():
        field = _SIG_FIELD.get(k)
        if field is None or field not in ev:
            return False
        if k == "impact_g_gte":
            if not sig["impact_g"] >= v:
                return False
        elif k == "impact_g_lt":
            if not sig["impact_g"] < v:
                return False
        elif k == "freefall_ms_lt":
            if not sig["freefall_ms"] < v:
                return False
        elif k == "still_ms_lt":
            if not sig["still_ms"] < v:
                return False
        elif k == "orientation_deg_lt":
            if not sig["orientation_deg"] < v:
                return False
    return True


def verify(rt, ins):
    """把“是不是真摔”从单一置信度升级成多信号加权判定。

    输出 verdict ∈ confirmed / likely / suspected / negligible，
    并顺便给出该等级对应的振铃时长与升级总预算——反应速度由判定结果驱动，
    而不是所有事件都等满 30s 振铃。
    """
    rt.require_cap("read_profile")
    rt.require_cap("score_fall")

    profile = ins.get("profile") or {}
    ev = ins.get("event") or {}
    model = rt.pack.data.get("fall_model") or {}
    weights = model.get("weights") or {}
    norms = model.get("norms") or {}
    th = model.get("thresholds") or {}
    policy = model.get("escalation") or {}
    voice = model.get("voice") or {}
    bias = model.get("safety_bias") or {}

    sig = {
        "impact_g": _num(ev.get("impact_g")),
        "freefall_ms": _num(ev.get("freefall_ms")),
        "still_ms": _num(ev.get("still_ms")),
        "orientation_deg": _num(ev.get("orientation_delta_deg")),
        "confidence": _num(ev.get("confidence")),
        "prior_false_alarms": int(_num(ev.get("prior_false_alarms"))),
    }
    has_motion = any(k in ev for k in
                     ("impact_g", "freefall_ms", "still_ms", "orientation_delta_deg"))

    reasons: list[str] = []
    if has_motion:
        sub = {
            "impact": min(sig["impact_g"] / max(_num(norms.get("impact_g"), 6.0), 1e-6), 1.0),
            "freefall": min(sig["freefall_ms"] / max(_num(norms.get("freefall_ms"), 300), 1e-6), 1.0),
            "still": min(sig["still_ms"] / max(_num(norms.get("still_ms"), 5000), 1e-6), 1.0),
            "orientation": min(sig["orientation_deg"] / max(_num(norms.get("orientation_deg"), 60), 1e-6), 1.0),
            "confidence": min(max(sig["confidence"], 0.0), 1.0),
        }
        score = sum(sub[k] * _num(weights.get(k)) for k in sub)
        reasons.append(
            f"冲击 {sig['impact_g']:.1f}g｜失重 {sig['freefall_ms']:.0f}ms｜"
            f"静止 {sig['still_ms'] / 1000:.1f}s｜姿态变化 {sig['orientation_deg']:.0f}°｜"
            f"端侧置信 {sig['confidence']:.2f}"
        )
    else:
        # 老场景只给置信度：退化为单信号，行为与升级前一致
        score = min(max(sig["confidence"], 0.0), 1.0)
        sub = {}
        reasons.append(f"无运动学特征，仅按端侧置信度 {sig['confidence']:.2f} 判定")

    if sig["prior_false_alarms"] >= 2:
        score -= 0.05
        reasons.append(f"近期误报 {sig['prior_false_alarms']} 次，下调 0.05")

    verdict = "negligible"
    for rule in model.get("hard_negatives") or []:
        if _hard_hit(rule.get("when") or {}, sig, ev):
            verdict = "negligible"
            reasons.append(f"硬否决 {rule.get('id')}：{rule.get('desc', '')}")
            break
    else:
        if score >= _num(th.get("confirmed"), 0.85):
            verdict = "confirmed"
        elif score >= _num(th.get("likely"), 0.70):
            verdict = "likely"
        elif score >= _num(th.get("suspected"), 0.50):
            verdict = "suspected"

    # 安全偏置：吃抗凝药的老人，宁可当真的处理
    meds = bias.get("medications_any") or []
    if verdict in (bias.get("from") or []) and meds and set(meds) & set(profile.get("medications") or []):
        reasons.append(f"抗凝用药命中安全偏置：{verdict} → {bias.get('upgrade_to')}")
        verdict = str(bias.get("upgrade_to") or verdict)

    esc = policy.get(verdict) or {}
    ring = _num(esc.get("ring_s"), _num(rt.params.get("ring_timeout_s"), 30.0))
    budget = _num(esc.get("budget_s"), 0.0)
    needs_voice = (verdict == "suspected"
                   or sig["confidence"] < _num(voice.get("low_confidence_below"), 0.8))

    rt.add_latency(int(_num(model.get("latency_ms"), 8)))
    return {
        "verdict": verdict,
        "score": round(score, 3),
        "subscores": {k: round(v, 3) for k, v in (sub or {}).items()},
        "signals": sig,
        "reasons": reasons,
        "ring_s": ring,
        "budget_s": budget,
        "needs_voice": needs_voice,
        "voice_wait_s": _num(voice.get("suspected_wait_s"), 3.0),
    }


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
        elif k == "night":
            # 旧版 app/core.py 支持这个谓词，pack 版漏了，导致 R-NIGHT 永远是死规则
            if bool(event.get("night")) != bool(v):
                return False
        else:
            return False
    return True


def triage(rt, ins):
    # 自查权限：以前 agents.json 里的 permissions 从没被读过，纯装饰
    rt.require_cap("read_profile")
    rt.require_cap("read_rules")
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
    """注意：这个 Agent 没有 read_profile 权限，因此不掌握用药禁忌。

    refs 来自知识检索节点 —— 以前 hits 写进 ctx 后没人消费，检索等于空转。
    """
    name = ins.get("name") or "老人"
    level = ins.get("level") or "mid"
    refs = ins.get("refs") or []
    urgent = "系统已触发紧急响应" if level in ("critical", "high") else "检测到疑似跌倒"
    text = f"{name} 可能发生跌倒，{urgent}，建议立即回电确认。建议先扶起老人并安抚。"
    titles = [str(r.get("title", "")) for r in refs if r.get("title")][:2]
    if titles:
        text += f"（处置依据：{'、'.join(titles)}）"
    return {"text": text}


def guard(rt, ins):
    """Supervisor 护栏：命中搬动禁忌时剥离危险指令。"""
    rt.require_cap("veto")
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
    rt.require_cap("call")
    contact = ins.get("contact") or {}
    idx = int(ins.get("index") or 0)
    ring = float(ins.get("ring") or 30.0)
    budget = float(ins.get("budget") or 0.0)
    answers = ins.get("answer") or []
    answer_at = ins.get("answer_at") or []

    # 升级链总时长封顶：以前每个联系人都等满 ring_timeout_s(30s)，
    # 两位家属全不接就要 60s 才能转社区。现在振铃时长由核验等级给出，
    # 且整条链共享一个 budget，超出就把最后一位的振铃裁剪掉。
    st = rt.ctx.setdefault("_escalation", {"elapsed": 0.0})
    remaining = max(budget - st["elapsed"], 1.0) if budget else ring
    eff_ring = min(ring, remaining) if budget else ring

    wants = bool(answers[idx]) if idx < len(answers) else False
    at = float(answer_at[idx]) if idx < len(answer_at) else 0.0
    answered = wants and (at <= eff_ring)
    spent = min(at, eff_ring) if answered else eff_ring
    st["elapsed"] += spent
    rt.add_latency(int(spent * 1000))

    budget_hit = bool(budget) and not answered and st["elapsed"] >= budget - 1e-6
    return {
        "contact": contact.get("name", ""),
        "answered": answered,
        "expected_answer_s": at,
        "ring_s": eff_ring,
        "overrun": bool(wants and not answered),
        "budget_hit": budget_hit,
        "elapsed_s": round(st["elapsed"], 3),
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
    "verify": verify,
    "triage": triage,
    "draft": draft,
    "guard": guard,
    "call": call,
    "baseline_triage": baseline_triage,
    "call_first": call_first,
    "noop": noop,
}
