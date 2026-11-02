"""agents.py — 三个职责隔离的 Agent + 单 Agent 基线

设计要点：
1. 每个 Agent 只能碰自己被授予的权限（permission），越权直接抛异常。
   这是"多智能体"在安全关键场景里唯一站得住的理由：出错可追责。
2. TriageAgent 的**判定**来自规则引擎，LLM 只负责把理由说人话。
3. ContactAgent 没有 read_profile 权限，所以它**天然不知道**老人是否服抗凝药，
   会给出"先扶起"这种通用建议 —— 由 Supervisor 的护栏否决。
   这不是凑戏，而是权限隔离的真实后果。
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

from .core import config

FORBIDDEN_PHRASES = ["扶起", "抱起", "搬动", "坐起", "扶他到", "让他坐"]


class PermissionDenied(Exception):
    pass


class BaseAgent:
    name = "agent"
    permissions: frozenset[str] = frozenset()

    def require(self, perm: str):
        if perm not in self.permissions:
            raise PermissionDenied(f"{self.name} 未被授予权限 [{perm}]")


# ---------------------------------------------------------------- LLM 适配（可选）

def llm_available() -> bool:
    return bool(os.environ.get("DEEPSEEK_API_KEY")) and config.use_llm


def llm_chat(prompt: str, system: str = "", timeout: float = 8.0) -> str | None:
    """只用标准库调 DeepSeek。失败返回 None，调用方必须能降级到模板。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key or not config.use_llm:
        return None
    body = json.dumps({
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "messages": ([{"role": "system", "content": system}] if system else [])
                    + [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 240,
    }).encode("utf-8")
    req = urllib.request.Request(
        os.environ.get("DEEPSEEK_BASE", "https://api.deepseek.com") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


# ---------------------------------------------------------------- 分诊 Agent

class TriageAgent(BaseAgent):
    """读档案 + 读规则 → 风险分级与处置计划。决策不经过 LLM。"""

    name = "triage"
    permissions = frozenset({"read_profile", "read_rules"})

    def __init__(self, engine, hub=None):
        self.engine = engine
        self.hub = hub

    def run(self, profile: dict, event: dict) -> dict:
        self.require("read_profile")
        self.require("read_rules")
        t0 = time.time()
        res = self.engine.evaluate(profile, event)

        # 知识库检索：规则库 / 文档库 / 经验库统一走 Hub
        hits: list[dict] = []
        if self.hub is not None:
            q = " ".join([
                str(profile.get("name", "")),
                *profile.get("conditions", []),
                *profile.get("medications", []),
                "跌倒", str(res.get("level", "")),
            ])
            try:
                hits = self.hub.search(q, k=3)
            except Exception:
                hits = []
        res["kb_hits"] = hits

        res["wording"] = self._wording(profile, event, res)
        res["duration_ms"] = int((time.time() - t0) * 1000)
        return res

    def _wording(self, profile: dict, event: dict, res: dict) -> str:
        base = "；".join(res.get("rationale", [])) or "常规跌倒处置"
        if llm_available():
            txt = llm_chat(
                f"老人{profile['name']}，{profile['age']}岁，病史{profile['conditions']}，"
                f"用药{profile['medications']}，跌倒检测置信度{event['confidence']}。"
                f"系统判定风险等级：{res['level']}。请用一句不超过40字的中文说明处置理由，不要给医学诊断。",
                system="你是养老应急调度系统的话术模块，只解释已有结论，不做诊断，不给具体药物建议。",
            )
            if txt:
                return txt
        return base


# ---------------------------------------------------------------- 联络 Agent

class ContactAgent(BaseAgent):
    """有外呼与推送权限，但没有档案读取权限 —— 因此无法判断用药禁忌。"""

    name = "contact"
    permissions = frozenset({"call", "push"})

    def draft_message(self, elder_name: str, level: str) -> str:
        self.require("push")
        if level in ("critical", "high"):
            return f"{elder_name} 可能发生跌倒，系统已触发紧急响应，建议立即回电确认。建议先扶起老人并安抚。"
        return f"{elder_name} 检测到疑似跌倒，请尽快联系确认。建议先扶起老人并安抚。"

    def call(self, contact: dict, timeout_s: float, answered: bool) -> dict:
        self.require("call")
        t0 = time.time()
        wait = min(timeout_s, 0.03) if not answered else 0.02
        time.sleep(wait)
        return {
            "contact": contact["name"],
            "phone": contact.get("phone", ""),
            "answered": answered,
            "duration_ms": int((time.time() - t0) * 1000),
        }


# ---------------------------------------------------------------- 升级 Agent

class EscalationAgent(BaseAgent):
    """有调度权限：决定下一个联系谁、何时转社区、何时提示 120。"""

    name = "escalate"
    permissions = frozenset({"dispatch_next", "page_community", "prompt_120"})

    def next_target(self, order: list[dict], tried: int) -> dict | None:
        self.require("dispatch_next")
        return order[tried] if tried < len(order) else None

    def community(self, community_contact: dict | None) -> dict | None:
        self.require("page_community")
        return community_contact

    def needs_120(self, triage: dict) -> bool:
        self.require("prompt_120")
        return triage.get("forbid_move", False) or triage.get("level") == "critical"


# ---------------------------------------------------------------- 单 Agent 基线（对照实验用）

class SingleAgentBaseline:
    """对照基线：一个 Agent + 全部工具，一次性决策，无升级、无重试、无禁忌护栏。

    诚实说明：mock 模式下这是**行为模拟**，不是真实 LLM 调用结果。
    正式提交前必须在 D12 用真实 API 重跑，否则数据不能对外写。
    """

    name = "single"

    def decide(self, profile: dict, event: dict) -> dict:
        # 典型单 Agent 行为：按字面理解任务，直接通知第一联系人，不查禁忌、不设升级
        first = profile["contacts"][0] if profile.get("contacts") else None
        return {
            "level": "mid",
            "forbid_move": False,
            "plan": ["通知家属"],
            "targets": [first] if first else [],
            "escalate": False,
            "retry_push": False,
            "guard": False,
        }
