"""supervisor.py — 确定性状态机（安全关键路径，不经过 LLM）

状态流转：
RECEIVED → (置信度闸门) → DISCARDED
         → (去重闸门)   → MERGED
         → TRIAGE
         → CANCEL_WINDOW（可撤销）→ ABORTED
         → DISPATCH（推送 + 外呼链）
         → ESCALATE（无应答则下一联系人）
         → COMMUNITY（转社区 / 提示 120）
         → CLOSED

所有超时、重试、升级顺序都是硬编码，LLM 无法改变；LLM 只影响话术。
"""
from __future__ import annotations

import os
import threading
import time
import urllib.request

from .agents import (
    FORBIDDEN_PHRASES,
    ContactAgent,
    EscalationAgent,
    SingleAgentBaseline,
    TriageAgent,
)
from .core import (
    LEVEL_CN,
    EventRecord,
    config,
    knowledge_hub,
    profiles,
    rules_engine,
    store,
)


# ---------------------------------------------------------------- 通知通道

class Notifier:
    """推送通道链：主通道 → 备用通道 → 本地控制台。
    真实短信/外呼 API 只需在这里加一个实现，其余代码不动。
    """

    def __init__(self):
        self.primary = os.environ.get("WEBHOOK_PRIMARY", "")
        self.fallback = os.environ.get("WEBHOOK_FALLBACK", "")

    def send(self, text: str, *, down_until: float = 0.0,
             primary_down: bool = False) -> dict:
        attempts = 0
        degraded = False
        deadline = max(down_until, time.time()) + 2.0   # 重试上限，避免永久挂起
        while True:
            attempts += 1
            now = time.time()
            if now < down_until:                        # 网络中断：退避重试
                degraded = True
                if now >= deadline:
                    return {"ok": False, "channel": "none",
                            "attempts": attempts, "degraded": True}
                time.sleep(config.push_retry_gap_s)
                continue
            if primary_down:                            # 主通道故障：切备用
                ok = self._post(self.fallback, text) if self.fallback else True
                return {"ok": ok, "channel": "fallback",
                        "attempts": attempts, "degraded": True}
            if self.primary:
                if self._post(self.primary, text):
                    return {"ok": True, "channel": "primary",
                            "attempts": attempts, "degraded": degraded}
                degraded = True
                if now >= deadline:
                    return {"ok": False, "channel": "none",
                            "attempts": attempts, "degraded": True}
                time.sleep(config.push_retry_gap_s)
                continue
            return {"ok": True, "channel": "console",
                    "attempts": attempts, "degraded": degraded}

    @staticmethod
    def _post(url: str, text: str) -> bool:
        try:
            body = urllib.request.Request(
                url,
                data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(body, timeout=3):
                return True
        except Exception:
            return False


# ---------------------------------------------------------------- 护栏

def guard_message(draft: str, forbid_move: bool) -> tuple[str, bool]:
    """冲突消解：禁区规则命中时，强制剥离"扶起/搬动"类指令。"""
    if not forbid_move:
        return draft, False
    hit = [p for p in FORBIDDEN_PHRASES if p in draft]
    if not hit:
        return draft, False
    cleaned = draft
    for p in hit:
        cleaned = cleaned.replace(f"建议先{p}老人并安抚。", "")
        cleaned = cleaned.replace(p, "（已屏蔽不当指令）")
    cleaned += " ⚠ 该老人存在禁忌：禁止搬动、抱起或让其坐起。"
    return cleaned, True


# ---------------------------------------------------------------- Supervisor

class Supervisor:
    def __init__(self):
        self.triage = TriageAgent(rules_engine, knowledge_hub)
        self.contact = ContactAgent()
        self.escalate = EscalationAgent()
        self.baseline = SingleAgentBaseline()
        self.notifier = Notifier()

    # ---------------------------------------------------------- 入口

    def submit(self, sc: dict, mode: str = "multi", sync: bool = False) -> EventRecord:
        prof = profiles[sc["elder_id"]]
        rec = store.create(
            elder_id=sc["elder_id"],
            elder_name=prof["name"],
            source=sc.get("source", "phone_sensor"),
            scenario=sc.get("id", "custom"),
            mode=mode,
            started=time.time(),
            confidence=sc.get("confidence", 0.9),
        )
        if sync:
            self._run(rec, sc, mode)
        else:
            threading.Thread(target=self._run, args=(rec, sc, mode), daemon=True).start()
        return rec

    # ---------------------------------------------------------- 主流程

    def _run(self, rec: EventRecord, sc: dict, mode: str):
        t0 = time.time()
        prof = profiles[rec.elder_id]
        flags = sc.get("flags", {})
        down_until = t0 + flags.get("network_down_ms", 0) / 1000.0
        cw = float(sc.get("cancel_window_s", config.cancel_window_s))
        ct = float(sc.get("call_timeout_s", config.call_timeout_s))

        # 场景脚本可以模拟"老人在 X 毫秒后按键撤销"
        if flags.get("cancel_after_ms"):
            def _press():
                time.sleep(flags["cancel_after_ms"] / 1000.0)
                store.cancel_flags[rec.event_id].set()
            threading.Thread(target=_press, daemon=True).start()

        store.step(rec, "system", "接收信号",
                   f"来源={rec.source}，置信度={rec.confidence:.2f}",
                   basis="端侧三段式状态机（失重→冲击→静止）")

        # 闸门 1：置信度
        if rec.confidence < config.confidence_threshold:
            rec.state, rec.outcome = "DISCARDED", "LOW_CONFIDENCE"
            store.step(rec, "supervisor", "丢弃事件",
                       f"置信度 {rec.confidence:.2f} 低于阈值 {config.confidence_threshold}",
                       basis="避免误报打扰家属", level="warn")
            self._finish(rec, t0)
            return

        # 闸门 2：重复事件去重（多智能体链路才做）
        if mode == "multi":
            dup = self._find_duplicate(rec)
            if dup:
                rec.state, rec.outcome, rec.merged_into = "MERGED", "DUPLICATE", dup.event_id
                store.step(rec, "supervisor", "事件去重",
                           f"与 {dup.event_id} 合并，不重复通知",
                           basis=f"{config.dedupe_window_s:.0f}s 内同一老人重复触发",
                           level="warn")
                self._finish(rec, t0)
                return

        # 分诊
        rec.sim_ms += config.triage_ms
        triage = self.triage.run(prof, sc)
        rec.level = triage["level"]
        rec.forbid_move = triage["forbid_move"]
        rec.plan = triage["plan"]
        rec.matched_rules = triage["matched_rules"]
        rec.kb_hits = len(triage.get("kb_hits") or [])
        store.step(
            rec, "triage", "风险分级",
            f"{LEVEL_CN.get(rec.level, rec.level)}｜命中规则 {rec.matched_rules or '无'}｜"
            f"联系顺序 {'→'.join(c['name'] for c in triage['contact_order'])}",
            duration_ms=triage["duration_ms"],
            basis=triage["wording"],
        )

        hits = triage.get("kb_hits") or []
        if hits:
            rec.sim_ms += config.kb_ms
            store.step(
                rec, "triage", "知识检索",
                "｜".join(f"[{h['kind']}] {h['title']}({h['score']})" for h in hits),
                duration_ms=config.kb_ms,
                basis="知识库注册中心：规则库 / 文档库 / 经验库统一检索",
            )
        for p in rec.plan:
            store.step(rec, "triage", "处置建议", p, basis="规则表")

        if mode == "single":
            self._run_single(rec, sc, prof, flags, down_until, t0, ct)
            return

        # 撤销窗口
        if cw > 0:
            store.step(rec, "supervisor", "开启撤销窗口",
                       f"{cw:.1f}s 内老人可撤销",
                       basis="误报治理：避免打扰家属")
            deadline = time.time() + cw
            flag = store.cancel_flags[rec.event_id]
            while time.time() < deadline:
                if flag.is_set():
                    rec.state, rec.outcome = "ABORTED", "CANCELLED_BY_ELDER"
                    rec.sim_ms += int((cw - max(deadline - time.time(), 0)) * 1000)
                    store.step(rec, "system", "老人撤销报警",
                               "未发出任何通知", level="ok")
                    self._finish(rec, t0)
                    return
                time.sleep(min(0.05, max(deadline - time.time(), 0.001)))
            rec.sim_ms += int(cw * 1000)
            store.step(rec, "supervisor", "撤销窗口结束",
                       "未撤销，进入派发", basis="状态机超时")

        self._dispatch(rec, prof, triage, flags, down_until, ct)
        self._finish(rec, t0)

    # ---------------------------------------------------------- 派发 + 升级链

    def _dispatch(self, rec: EventRecord, prof: dict, triage: dict,
                  flags: dict, down_until: float, ct: float):
        draft = self.contact.draft_message(prof["name"], triage["level"])
        msg, vetoed = guard_message(draft, triage["forbid_move"])
        if vetoed:
            store.step(rec, "supervisor", "冲突消解 · 否决危险指令",
                       "ContactAgent 的「建议先扶起老人」与用药禁忌冲突，已剥离并追加警示",
                       basis=f"规则 {rec.matched_rules} 命中 forbid_move；"
                             "ContactAgent 无档案读取权限，故不掌握禁忌",
                       level="veto")
        else:
            store.step(rec, "contact", "生成通知内容", msg.split("。")[0] + "。",
                       basis="话术生成")

        rec.sim_ms += config.push_ms
        res = self.notifier.send(msg, down_until=down_until,
                                primary_down=flags.get("primary_down", False))
        rec.notified = res["ok"]
        rec.degraded = rec.degraded or res["degraded"]
        store.step(
            rec, "contact", "推送家属",
            f"通道={res['channel']}，尝试 {res['attempts']} 次"
            + ("（已降级）" if res["degraded"] else ""),
            duration_ms=0, basis="推送通道链",
            level="ok" if res["ok"] else "warn",
        )
        if not res["ok"]:
            rec.state, rec.outcome = "CLOSED", "PUSH_FAILED"
            store.step(rec, "supervisor", "推送彻底失败",
                       "进入本地告警 + 定时重试队列", level="warn")
            return

        answers = flags.get("answer", [])
        answer_at = flags.get("answer_at", [])
        ring = float(config.ring_timeout_s)
        order = triage["contact_order"]
        tried = 0
        while True:
            target = self.escalate.next_target(order, tried)
            if target is None:
                break
            wants = bool(answers[tried]) if tried < len(answers) else False
            at = float(answer_at[tried]) if tried < len(answer_at) else 0.0
            answered = wants and (at <= ring)
            store.step(rec, "contact", f"外呼 {target['name']}",
                       f"振铃上限 {ring:.0f}s"
                       + (f"｜家属预计 {at:.0f}s 接听" if wants else "｜预计无法接通"),
                       basis="升级链顺序：家属 → 其他 → 社区")
            r = self.contact.call(target, ct, answered)
            rec.sim_ms += int((min(at, ring) if answered else ring) * 1000)
            if r["answered"]:
                rec.notified = True
                rec.outcome = "ANSWERED"
                rec.state = "CLOSED"
                store.step(rec, "contact", f"{target['name']} 已接听",
                           "家属确认，事件闭环", duration_ms=r["duration_ms"],
                           level="ok")
                return
            tried += 1
            rec.escalated += 1
            rec.sim_ms += config.escalate_ms
            if wants and not answered:
                store.step(rec, "supervisor", "接听超时被判为无人接听",
                           f"家属预计 {at:.0f}s 接听，振铃上限只有 {ring:.0f}s —— 参数过紧，"
                           "系统把一个本来会接听的电话当成了无人接听",
                           basis="Self-Loop 调优边界：振铃过短会误伤", level="veto")
            store.step(rec, "escalate", "无人接听，升级",
                       f"第 {tried} 次升级", basis="超时未接听", level="warn")

        # 升级链耗尽
        rec.sim_ms += config.escalate_ms
        com = self.escalate.community(triage.get("community"))
        if com:
            store.step(rec, "escalate", f"转 {com['name']}",
                       "家属全部未接听，转社区上门", basis="升级链耗尽")
            rec.notified = True
        if self.escalate.needs_120(triage):
            store.step(rec, "escalate", "提示呼叫 120",
                       "危急等级或存在搬动禁忌，建议直接呼叫急救",
                       basis=f"等级={LEVEL_CN.get(triage['level'])}，forbid_move={triage['forbid_move']}")
        rec.state = "CLOSED"
        rec.outcome = "COMMUNITY_ESCALATED"

    # ---------------------------------------------------------- 单 Agent 基线

    def _run_single(self, rec: EventRecord, sc: dict, prof: dict,
                    flags: dict, down_until: float, t0: float, ct: float):
        d = self.baseline.decide(prof, sc)
        rec.level = d["level"]
        rec.forbid_move = d["forbid_move"]
        store.step(rec, "single", "一次性决策",
                   "单 Agent 直接选择动作，无规则校验、无升级链",
                   basis="基线：一个 Agent + 全部工具")
        if not d["targets"]:
            rec.state, rec.outcome = "CLOSED", "NO_CONTACT"
            self._finish(rec, t0)
            return
        target = d["targets"][0]
        draft = f"{prof['name']} 可能发生跌倒，建议先扶起老人并安抚。"
        store.step(rec, "single", "推送+外呼",
                   f"仅联系 {target['name']}，不重试、不升级", basis="基线策略")
        res = self.notifier.send(draft, down_until=down_until,
                                 primary_down=flags.get("primary_down", False))
        rec.notified = res["ok"]
        rec.degraded = rec.degraded or res["degraded"]
        answered = (flags.get("answer") or [False])[0]
        rec.sim_ms += int(config.answer_ring_s * 1000) if answered else int(config.ring_timeout_s * 1000)
        r = self.contact.call(target, ct, answered)
        if r["answered"]:
            rec.outcome, rec.state = "ANSWERED", "CLOSED"
            store.step(rec, "single", "已接听", "事件闭环", level="ok")
        else:
            rec.state, rec.outcome = "CLOSED", "NO_ANSWER_NO_ESCALATION"
            store.step(rec, "single", "无人接听",
                       "基线无升级机制，事件到此终止", level="warn")
        self._finish(rec, t0)

    # ---------------------------------------------------------- 辅助

    def _find_duplicate(self, rec: EventRecord) -> EventRecord | None:
        now = time.time()
        for e in reversed(store.events):
            if e is rec:
                continue
            if (e.elder_id == rec.elder_id and e.mode == rec.mode
                    and now - e.started <= config.dedupe_window_s
                    and e.state not in ("MERGED",)):
                return e
        return None

    @staticmethod
    def _finish(rec: EventRecord, t0: float):
        rec.total_ms = int((time.time() - t0) * 1000)
        store.step(rec, "system", "事件结束",
                   f"状态={rec.state}｜结局={rec.outcome}｜总耗时 {rec.total_ms}ms",
                   level="ok")


supervisor = Supervisor()
