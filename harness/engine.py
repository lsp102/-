"""engine.py — 工作流执行引擎

节点类型（平台的编排原语）：
    input      入口
    agent      调用一个已注册 Agent（LLM 驱动或代码驱动）
    tool       调用一个已注册工具（受 Agent 权限约束）
    condition  条件分支
    parallel   并行扇出，全部完成后汇合
    foreach    遍历 + 中断条件（升级链、重试链）
    wait       可取消等待（人工确认窗口、超时）
    knowledge  知识库检索
    transform  写入上下文
    end        终止，标记最终状态

引擎不认识任何业务：所有领域逻辑都在 pack 的 handlers / tools 里。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .expr import Evaluator, ExprError
from .knowledge import CaseKnowledge, DocKnowledge, KnowledgeHub, RuleKnowledge
from .pack import Pack
from .trace import Trace

MAX_STEPS = 500


class PermissionDenied(Exception):
    pass


class WorkflowError(Exception):
    pass


# ---------------------------------------------------------------- 结果

@dataclass
class RunResult:
    pack_id: str
    scenario_id: str
    workflow: str
    final: str = ""
    ctx: dict = field(default_factory=dict)
    trace: Trace | None = None
    sim_ms: int = 0
    error: str = ""

    def steps(self) -> list[dict]:
        return self.trace.to_list() if self.trace else []

    def to_dict(self) -> dict:
        return {
            "pack": self.pack_id,
            "scenario": self.scenario_id,
            "workflow": self.workflow,
            "final": self.final,
            "sim_ms": self.sim_ms,
            "error": self.error,
            "ctx": self.ctx,
            "trace": self.steps(),
            "trace_summary": self.trace.summary() if self.trace else {},
        }


def _brief(v, n: int = 60) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + "…"


# ---------------------------------------------------------------- 引擎

class Engine:
    def __init__(self, pack: Pack, params: dict | None = None,
                 shared: dict | None = None, mode: str = "mock",
                 on_step=None):
        self.pack = pack
        self.on_step = on_step
        self.pace_s = 0.0
        self.base_params = pack.params()
        if params:
            self.base_params.update(params)
        self.params: dict = dict(self.base_params)
        self.shared: dict = shared if shared is not None else {}
        self.mode = mode

        self.trace = Trace()
        self.ctx: dict = {}
        self.sim_ms = 0
        self._final = ""
        self._cancel = threading.Event()
        self._ev: Evaluator | None = None
        self._lock = threading.RLock()

        self.hub = self._build_kb()

    # ---------------------------------------------------------- 知识库

    def _build_kb(self) -> KnowledgeHub:
        hub = KnowledgeHub()
        rules = (self.pack.data.get("rules") or {}).get("rules") or []
        if rules:
            hub.register(RuleKnowledge(rules))
        if self.pack.kb_dir is not None:
            hub.register(DocKnowledge(self.pack.kb_dir, name="文档知识库"))
        if "cases" in self.pack.data:
            hub.register(CaseKnowledge(items=(self.pack.data["cases"] or {}).get("lessons", [])))
        return hub

    # ---------------------------------------------------------- 对外

    def cancel(self):
        self._cancel.set()

    def add_latency(self, ms: int):
        with self._lock:
            self.sim_ms += max(int(ms), 0)

    def require(self, agent_id: str, cap: str):
        ad = self.pack.agents.get(agent_id) or {}
        if cap not in ad.get("permissions", []):
            raise PermissionDenied(f"{agent_id} 未被授予权限 [{cap}]")

    def call_tool(self, agent_id: str, tool: str, **kw):
        fn = self.pack.tools.get(tool)
        if fn is None:
            raise KeyError(f"工具未注册: {tool}")
        ad = self.pack.agents.get(agent_id)
        if ad is not None and tool not in ad.get("tools", []):
            raise PermissionDenied(f"{agent_id} 无 [{tool}] 工具权限")
        return fn(self, **kw)

    # ---------------------------------------------------------- 执行

    def run(self, scenario: dict, workflow: str | None = None) -> RunResult:
        wf_name = workflow or self.pack.meta.get("default_workflow") or next(iter(self.pack.workflows))
        wf = self.pack.workflows.get(wf_name)
        if wf is None:
            raise WorkflowError(f"工作流不存在: {wf_name}")
        self._wf = wf

        self.params = dict(self.base_params)
        self.params.update(scenario.get("params") or {})
        self.pace_s = max(float(self.params.get("pace_ms") or 0), 0.0) / 1000.0
        self.trace = Trace()
        self.sim_ms = 0
        self._final = ""
        self._cancel.clear()

        ctx: dict[str, Any] = {
            "params": self.params,
            "event": scenario.get("event") or {},
            "workspace": {},
            "run": {"scenario": scenario.get("id", ""), "workflow": wf_name},
        }
        pid = scenario.get("profile")
        if pid:
            ctx["profile"] = (self.pack.data.get("profiles") or {}).get(pid) or {}
        else:
            ctx["profile"] = {}
        self.ctx = ctx

        self._ev = Evaluator({
            "ctx": ctx,
            "event": ctx["event"],
            "profile": ctx["profile"],
            "params": self.params,
            "data": self.pack.data,
            "hub": self.hub,
        })

        res = RunResult(pack_id=self.pack.id,
                        scenario_id=str(scenario.get("id", "")),
                        workflow=wf_name)
        node = wf.get("entry")
        steps = 0
        try:
            while node and steps < MAX_STEPS:
                steps += 1
                node = self._exec(node)
            if steps >= MAX_STEPS:
                raise WorkflowError(f"超过最大步数 {MAX_STEPS}，疑似死循环")
            res.final = self._final or "unfinished"
        except Exception as e:
            res.error = f"{type(e).__name__}: {e}"
            res.final = "error"
            self.trace.add("<engine>", "engine", "", "error", res.error, 0)

        res.ctx = ctx
        res.trace = self.trace
        res.sim_ms = self.sim_ms
        return res

    def _exec(self, node_id: str | None) -> str | None:
        if not node_id:
            return None
        node = (self._wf.get("nodes") or {}).get(node_id)
        if node is None:
            raise WorkflowError(f"节点不存在: {node_id}")
        impl = NODE_IMPL.get(node.get("type"))
        if impl is None:
            raise WorkflowError(f"未知节点类型: {node.get('type')}")
        t0 = time.perf_counter()
        try:
            nxt, summary, status = impl(self, node_id, node)
        except Exception as e:  # noqa: BLE001
            ms = int((time.perf_counter() - t0) * 1000)
            self._emit(node_id, node, "error", f"{type(e).__name__}: {e}", ms)
            if node.get("on_error"):
                return node["on_error"]
            raise
        ms = int((time.perf_counter() - t0) * 1000)
        self._emit(node_id, node, status, summary, ms)
        return nxt

    def _emit(self, node_id: str, node: dict, status: str, summary: str, ms: int):
        step = self.trace.add(node_id, node.get("type", "?"), node.get("actor", ""),
                              status, summary, ms)
        if self.on_step is not None:
            try:
                self.on_step(step.to_dict())
            except Exception:
                pass
        if self.pace_s:
            time.sleep(self.pace_s)


# ---------------------------------------------------------------- 节点实现

def _node_input(rt: "Engine", nid: str, n: dict):
    return n.get("next"), "进入工作流", "ok"


def _node_agent(rt: "Engine", nid: str, n: dict):
    aid = n["agent"]
    ad = rt.pack.agents.get(aid)
    if ad is None:
        raise KeyError(f"Agent 未注册: {aid}")
    inputs = {k: rt._ev.eval(v) for k, v in (n.get("input") or {}).items()}
    out = _invoke(rt, aid, ad, inputs)
    status = "ok"
    if isinstance(out, dict) and "_status" in out:
        status = str(out.pop("_status"))
    rt.ctx[n.get("out", aid)] = out
    label = ad.get("name", aid)
    return n.get("next"), f"{label} → {_brief(out)}", status


def _invoke(rt: "Engine", aid: str, ad: dict, inputs: dict) -> dict:
    driver = ad.get("driver", "handler")
    if driver == "handler":
        fn = rt.pack.handlers.get(ad.get("handler", aid))
        if fn is None:
            raise KeyError(f"handler 未注册: {ad.get('handler', aid)}")
        return fn(rt, inputs) or {}
    return {"text": _llm(rt, ad, inputs)}


def _llm(rt: "Engine", ad: dict, inputs: dict) -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if rt.mode == "real" and key:
        prompt = "\n".join(f"{k}: {_brief(v, 400)}" for k, v in inputs.items())
        body = json.dumps({
            "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
            "messages": [
                {"role": "system", "content": ad.get("prompt", "你是一个多智能体系统中的执行者。")},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": int(ad.get("max_tokens", 400)),
        }).encode("utf-8")
        try:
            req = urllib.request.Request(
                (os.environ.get("DEEPSEEK_BASE", "https://api.deepseek.com")) + "/chat/completions",
                data=body,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            return f"[llm-error] {type(e).__name__}: {e}"
    body = "；".join(f"{k}={_brief(v)}" for k, v in inputs.items())
    return f"[mock:{ad.get('name', ad.get('id'))}] {body}"


def _node_tool(rt: "Engine", nid: str, n: dict):
    args = {k: rt._ev.eval(v) for k, v in (n.get("args") or {}).items()}
    out = rt.call_tool(n.get("agent", "platform"), n["tool"], **args)
    key = n.get("out") or n["tool"].split(".")[-1]
    rt.ctx[key] = out
    return n.get("next"), f"{n['tool']} → {_brief(out)}", "ok"


def _node_condition(rt: "Engine", nid: str, n: dict):
    v = bool(rt._ev.eval(n["when"]))
    return (n.get("then") if v else n.get("else")), f"{n['when']} ⇒ {v}", "ok"


def _node_parallel(rt: "Engine", nid: str, n: dict):
    branches = list(n.get("branches") or [])
    errors: list[str] = []

    def go(b):
        try:
            rt._exec(b)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{b}: {e}")

    ts = [threading.Thread(target=go, args=(b,), daemon=True) for b in branches]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    if errors:
        raise WorkflowError("并行分支失败: " + "; ".join(errors))
    return n.get("next"), f"并行 {len(branches)} 分支完成", "ok"


def _node_foreach(rt: "Engine", nid: str, n: dict):
    items = rt._ev.eval(n["over"]) or []
    if not isinstance(items, (list, tuple)):
        items = [items]
    maxn = int(n.get("max", 20))
    ran = 0
    for i, item in enumerate(items):
        rt.ctx["item"] = item
        rt.ctx["index"] = i
        rt._ev.names["item"] = item
        rt._ev.names["index"] = i
        rt._exec(n.get("body"))
        ran += 1
        if n.get("break_when") and bool(rt._ev.eval(n["break_when"])):
            return n.get("on_break"), f"第 {ran} 次命中中断条件", "ok"
        if ran >= maxn:
            break
    return n.get("on_exhausted"), f"遍历 {ran} 项，未命中中断条件", "ok"


def _node_wait(rt: "Engine", nid: str, n: dict):
    secs = float(rt._ev.eval(n.get("duration", 0)) or 0)
    rt.add_latency(int(secs * 1000))
    deadline = time.time() + secs
    while time.time() < deadline:
        if rt._cancel.is_set():
            return n.get("on_cancel"), "窗口内被撤销", "cancel"
        time.sleep(min(0.05, max(deadline - time.time(), 0.001)))
    return n.get("next"), f"等待 {secs:.1f}s 未被撤销", "ok"


def _node_knowledge(rt: "Engine", nid: str, n: dict):
    q = rt._ev.eval(n["query"]) or ""
    k = int(n.get("k", 3))
    hits = rt.hub.search(str(q), k=k)
    rt.ctx[n.get("out", "hits")] = hits
    rt.add_latency(int(n.get("latency_ms", 15)))
    titles = ",".join(h["title"] for h in hits[:3]) or "无命中"
    return n.get("next"), f"检索「{_brief(q, 24)}」→ {len(hits)} 条（{titles}）", "ok"


def _node_transform(rt: "Engine", nid: str, n: dict):
    sets = n.get("set") or {}
    for k, v in sets.items():
        rt.ctx[k] = rt._ev.eval(v)
    return n.get("next"), f"写入 {','.join(sets)}", "ok"


def _node_end(rt: "Engine", nid: str, n: dict):
    rt._final = str(n.get("status") or nid)
    return None, f"结束 → {rt._final}", "ok"


NODE_IMPL = {
    "input": _node_input,
    "agent": _node_agent,
    "tool": _node_tool,
    "condition": _node_condition,
    "parallel": _node_parallel,
    "foreach": _node_foreach,
    "wait": _node_wait,
    "knowledge": _node_knowledge,
    "transform": _node_transform,
    "end": _node_end,
}


# ---------------------------------------------------------------- 断言

def resolve(res: RunResult, key: str):
    if key == "final":
        return res.final
    if key.startswith("trace."):
        rest = key[6:]
        if rest.startswith("node:"):
            return res.trace.count(node=rest[5:])
        if rest.startswith("actor:"):
            return res.trace.count(actor=rest[6:])
        return res.trace.count(status=rest)
    path = key[4:] if key.startswith("ctx.") else key
    cur = res.ctx
    for seg in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, (list, tuple)) and seg.isdigit():
            cur = cur[int(seg)]
        else:
            return None
    return cur


def check_expect(res: RunResult, expect: dict) -> list[str]:
    fails = []
    for k, want in (expect or {}).items():
        got = resolve(res, k)
        if got != want:
            fails.append(f"{k}: 期望 {want!r}，实际 {got!r}")
    return fails
