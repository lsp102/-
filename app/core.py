"""core.py — 数据模型 / 规则引擎 / 档案 / 事件日志
零第三方依赖，只用标准库。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .knowledge import CaseKnowledge, DocKnowledge, KnowledgeHub, RuleKnowledge

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"

LEVEL_ORDER = {"low": 0, "mid": 1, "high": 2, "critical": 3}
LEVEL_CN = {"low": "低", "mid": "中", "high": "高", "critical": "危急"}


# ---------------------------------------------------------------- 配置

@dataclass
class Config:
    cancel_window_s: float = 15.0     # 老人端撤销窗口
    call_timeout_s: float = 30.0      # 单次外呼超时
    push_retry: int = 5               # 推送重试次数（网络中断）
    push_retry_gap_s: float = 0.4
    dedupe_window_s: float = 10.0     # 重复事件去重窗口
    confidence_threshold: float = 0.6 # 低于此值直接丢弃
    use_llm: bool = False             # LLM 只用于话术，不参与判定

    # ---- 仿真时延模型（离散事件仿真，不依赖真实外呼）----
    ring_timeout_s: float = 30.0      # 单次振铃等待上限 ← Self-Loop 的调优目标
    answer_ring_s: float = 6.0        # 接听前的平均振铃
    triage_ms: int = 40               # 分诊耗时
    kb_ms: int = 15                   # 知识检索耗时
    push_ms: int = 200                # 推送耗时
    escalate_ms: int = 50             # 一次升级调度耗时

    def fast(self) -> "Config":
        """实验模式：把等待压到毫秒级，逻辑路径完全不变。"""
        c = Config(**asdict(self))
        c.cancel_window_s = 0.0
        c.call_timeout_s = 0.03
        c.push_retry_gap_s = 0.01
        c.dedupe_window_s = 10.0
        return c


config = Config()


def apply_learned() -> dict:
    """把 Self-Loop 采纳的参数写进运行时配置。

    这是"反馈优化"真正生效的地方：learning.json 里的 ring_timeout_s
    会在下一次启动/刷新后改变状态机行为，而不只是页面上的一句话。
    """
    p = DATA / "learning.json"
    try:
        params = json.loads(p.read_text(encoding="utf-8")).get("params", {})
    except Exception:
        params = {}
    for k, v in params.items():
        if hasattr(config, k) and isinstance(v, (int, float)):
            setattr(config, k, v)
    return params


# ---------------------------------------------------------------- 规则引擎

class RulesEngine:
    """结构化规则表。决策只走这里，LLM 不参与。"""

    def __init__(self, rules: list[dict]):
        self.rules = rules

    @classmethod
    def load(cls, path: Path | None = None) -> "RulesEngine":
        p = path or (DATA / "rules.json")
        return cls(json.loads(p.read_text(encoding="utf-8"))["rules"])

    def evaluate(self, profile: dict, event: dict) -> dict:
        level = event.get("base_level", "mid")
        forbid_move = False
        plan: list[str] = []
        rationale: list[str] = []
        matched: list[str] = []

        for r in self.rules:
            if not self._match(r.get("when", {}), profile, event):
                continue
            matched.append(r["id"])
            t = r.get("then", {})
            lv = t.get("level")
            if lv and LEVEL_ORDER.get(lv, 0) > LEVEL_ORDER.get(level, 0):
                level = lv
            if t.get("forbid_move"):
                forbid_move = True
            plan.extend(t.get("plan_add", []))
            if t.get("rationale"):
                rationale.append(t["rationale"])

        return {
            "level": level,
            "forbid_move": forbid_move,
            "plan": plan,
            "rationale": rationale,
            "matched_rules": matched,
            # 外呼链只放家属/其他紧急联系人；社区是升级链耗尽后的兜底
            "contact_order": build_contact_order(profile),
            "community": community_of(profile),
        }

    def _match(self, when: dict, profile: dict, event: dict) -> bool:
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
                if bool(event.get("night")) != bool(v):
                    return False
            else:
                return False
        return True


def build_contact_order(profile: dict) -> list[dict]:
    """外呼链：家属 → 其他紧急联系人。社区不在链内，属于升级兜底。"""
    cs = profile.get("contacts", [])
    fam = [c for c in cs if c.get("role") == "family"]
    other = [c for c in cs if c.get("role") not in ("family", "community")]
    return fam + other


def community_of(profile: dict) -> dict | None:
    for c in profile.get("contacts", []):
        if c.get("role") == "community":
            return c
    return None


# ---------------------------------------------------------------- 档案

def load_profiles() -> dict[str, dict]:
    p = DATA / "profiles.json"
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 事件记录

@dataclass
class Step:
    ts: float
    actor: str
    action: str
    detail: str
    duration_ms: int = 0
    basis: str = ""
    level: str = "info"   # info | ok | warn | veto


@dataclass
class EventRecord:
    event_id: str
    elder_id: str
    elder_name: str
    source: str
    scenario: str
    mode: str
    started: float
    confidence: float
    state: str = "RECEIVED"
    level: str = ""
    forbid_move: bool = False
    outcome: str = ""
    escalated: int = 0
    notified: bool = False
    degraded: bool = False
    false_alarm: bool = False
    merged_into: str = ""
    matched_rules: list[str] = field(default_factory=list)
    plan: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    total_ms: int = 0
    sim_ms: int = 0          # 仿真时延（离散事件模型，单位毫秒）
    kb_hits: int = 0         # 本次事件命中的知识条目数

    def to_dict(self) -> dict:
        d = asdict(self)
        d["level_cn"] = LEVEL_CN.get(self.level, self.level)
        return d


class Store:
    def __init__(self):
        self._lock = threading.RLock()
        self.events: list[EventRecord] = []
        self.by_id: dict[str, EventRecord] = {}
        self.cancel_flags: dict[str, threading.Event] = {}
        self._seq = 0
        self.version = 0

    def reset(self):
        with self._lock:
            self.events.clear()
            self.by_id.clear()
            self.cancel_flags.clear()
            self._seq = 0
            self.version += 1

    def create(self, **kw) -> EventRecord:
        with self._lock:
            self._seq += 1
            eid = f"E{self._seq:04d}"
            rec = EventRecord(event_id=eid, **kw)
            self.events.append(rec)
            self.by_id[eid] = rec
            self.cancel_flags[eid] = threading.Event()
            self.version += 1
            return rec

    def step(self, rec: EventRecord, actor: str, action: str, detail: str,
             duration_ms: int = 0, basis: str = "", level: str = "info"):
        st = Step(ts=time.time(), actor=actor, action=action, detail=detail,
                  duration_ms=duration_ms, basis=basis, level=level)
        with self._lock:
            rec.steps.append(st)
            self.version += 1
        return st

    def cancel(self, event_id: str) -> bool:
        flag = self.cancel_flags.get(event_id)
        if not flag:
            return False
        flag.set()
        return True

    def summary_list(self) -> list[dict]:
        with self._lock:
            out = []
            for r in reversed(self.events):
                out.append({
                    "event_id": r.event_id,
                    "elder_name": r.elder_name,
                    "elder_id": r.elder_id,
                    "level": r.level,
                    "level_cn": LEVEL_CN.get(r.level, r.level),
                    "state": r.state,
                    "outcome": r.outcome,
                    "mode": r.mode,
                    "scenario": r.scenario,
                    "started": r.started,
                    "notified": r.notified,
                    "escalated": r.escalated,
                    "degraded": r.degraded,
                    "false_alarm": r.false_alarm,
                    "forbid_move": r.forbid_move,
                    "total_ms": r.total_ms,
                    "sim_ms": r.sim_ms,
                    "kb_hits": r.kb_hits,
                    "step_count": len(r.steps),
                    "latest_action": r.steps[-1].action if r.steps else "",
                    "latest": r.steps[-1].detail if r.steps else "",
                })
            return out


store = Store()
profiles = load_profiles()
rules_engine = RulesEngine.load()

# 知识库注册中心：三种知识源同一接口，业务层只依赖 Hub
knowledge_hub = KnowledgeHub()
knowledge_hub.register(RuleKnowledge(rules_engine.rules))
knowledge_hub.register(DocKnowledge())
knowledge_hub.register(CaseKnowledge())

# 启动时加载 Self-Loop 已采纳的参数
apply_learned()
