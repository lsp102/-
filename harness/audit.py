"""audit.py — pack 健康检查

validate 只能告诉你“场景过没过”，不能告诉你：
  - 哪些规则从来没有被任何场景触发（死规则）
  - 哪个工具节点没有声明 agent（等于没有白名单）
  - 哪个节点从 entry 根本走不到
  - 哪些条件节点缺 else（假分支会直接抛错）

这些正是“文档里写了、实际没生效”的高发区。audit 把它们一次性列出来。
"""
from __future__ import annotations

import tempfile

from .engine import NODE_IMPL
from .memory import MemoryPolicy
from .pack import Pack
from .runner import run_scenario

MEMORY_OPS = {"get", "set", "incr", "append", "delete"}


def _reachable(workflow: dict) -> set[str]:
    nodes = workflow.get("nodes") or {}
    entry = workflow.get("entry")
    if entry not in nodes:
        return set()
    seen = {entry}
    stack = [entry]
    while stack:
        nid = stack.pop()
        n = nodes.get(nid) or {}
        targets = []
        t = n.get("type")
        if t == "condition":
            targets += [n.get("then"), n.get("else")]
        elif t == "parallel":
            targets += list(n.get("branches") or []) + [n.get("next")]
        elif t == "foreach":
            targets += [n.get("body"), n.get("on_break"), n.get("on_exhausted")]
        else:
            targets.append(n.get("next"))
        targets += [n.get("on_cancel"), n.get("on_error")]
        for to in targets:
            if to and to in nodes and to not in seen:
                seen.add(to)
                stack.append(to)
    return seen


def audit_pack(pack: Pack, run_scenarios: bool = True) -> dict:
    issues: list[dict] = []

    def add(level: str, code: str, msg: str, where: str = ""):
        issues.append({"level": level, "code": code, "msg": msg, "where": where})

    # ---------------------------------------------------------------- 静态
    for wf_name, wf in pack.workflows.items():
        nodes = wf.get("nodes") or {}
        for nid, n in nodes.items():
            t = n.get("type")
            where = f"{wf_name}/{nid}"
            if t not in NODE_IMPL:
                add("error", "BAD_TYPE", f"未知节点类型: {t}", where)
                continue

            if t == "tool":
                tool, agent = n.get("tool"), n.get("agent")
                if tool not in pack.tools:
                    add("error", "TOOL_MISSING", f"工具未注册: {tool}", where)
                elif not agent:
                    add("error", "TOOL_NO_AGENT", "工具节点未声明 agent，白名单形同虚设", where)
                elif agent not in pack.agents:
                    add("error", "AGENT_MISSING", f"Agent 未注册: {agent}", where)
                elif tool not in (pack.agents[agent].get("tools") or []):
                    add("error", "TOOL_DENIED", f"{agent} 的白名单里没有工具 {tool}", where)

            if t == "agent":
                aid = n.get("agent")
                if aid not in pack.agents:
                    add("error", "AGENT_MISSING", f"Agent 未注册: {aid}", where)
                elif nid != "receive" and not n.get("out"):
                    add("warn", "AGENT_NO_OUT", "agent 节点没有 out，结果会被写进以 agent 名命名的键", where)

            if t == "condition" and not n.get("else"):
                add("warn", "COND_NO_ELSE", "条件节点没有 else，假分支会抛错", where)
            if t == "foreach" and not n.get("on_exhausted"):
                add("error", "FOREACH_NO_EXHAUSTED", "foreach 缺少 on_exhausted", where)
            if t == "wait" and not n.get("on_cancel"):
                add("warn", "WAIT_NO_CANCEL", "wait 缺少 on_cancel，被撤销会抛错", where)

            if t == "memory":
                op = str(n.get("op") or "get")
                if op not in MEMORY_OPS:
                    add("error", "MEM_BAD_OP", f"memory 操作未知: {op}", where)
                if not (n.get("key") or n.get("key_expr")):
                    add("error", "MEM_NO_KEY", "memory 节点缺少 key / key_expr", where)
                if op == "set" and "value" not in n and "value_expr" not in n:
                    add("warn", "MEM_NO_VALUE", "memory set 没有 value / value_expr，会写入 null", where)
                if not n.get("out"):
                    add("warn", "MEM_NO_OUT", "memory 节点没有 out，结果写进 ctx.mem", where)

        reach = _reachable(wf)
        for nid in nodes:
            if nid not in reach:
                add("warn", "UNREACHABLE", "节点从 entry 不可达", f"{wf_name}/{nid}")

    # ---------------------------------------------------------------- 动态
    rules = (pack.data.get("rules") or {}).get("rules") or []
    matched_ever: set[str] = set()
    finals: set[str] = set()
    if run_scenarios and pack.scenarios:
        wf_name = pack.meta.get("default_workflow") or next(iter(pack.workflows), None)
        # 体检跑在隔离状态目录里，不碰真实的持久记忆
        with tempfile.TemporaryDirectory(prefix="harness_audit_") as sandbox:
            for sc in pack.scenarios:
                res, _eng = run_scenario(pack, sc, wf_name, state_dir=sandbox)
                finals.add(res.final)
                tri = res.ctx.get("triage") or {}
                matched_ever.update(tri.get("matched") or [])
                if res.error:
                    add("error", "SCENARIO_ERROR", res.error, str(sc.get("id")))
        for r in rules:
            if r.get("id") not in matched_ever:
                add("warn", "RULE_UNCOVERED",
                    f"规则 {r.get('id')} 从未被任何场景触发：{r.get('desc', '')}", str(r.get("id")))

    defined_ends = {n.get("status") for wf in pack.workflows.values()
                    for n in (wf.get("nodes") or {}).values()
                    if n.get("type") == "end"}
    for st in sorted(defined_ends - finals):
        add("warn", "END_UNUSED", f"结局 {st} 没有任何场景走到", st)

    policy = MemoryPolicy.from_config(pack.meta.get("memory") or pack.data.get("memory") or {})
    return {
        "pack": pack.id,
        "memory": policy.describe(),
        "issues": issues,
        "rules_total": len(rules),
        "rules_covered": sorted(matched_ever),
        "finals": sorted(finals),
    }
