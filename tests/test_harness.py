"""tests — 针对本次修的问题的回归用例（标准库 unittest，无第三方依赖）

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.audit import audit_pack                      # noqa: E402
from harness.engine import Engine, MAX_STEPS              # noqa: E402
from harness.pack import clear_cache, load_pack           # noqa: E402
from harness.runner import run_scenario                   # noqa: E402

PACKS = ROOT / "packs"


def _write(dir_: Path, name: str, payload) -> None:
    p = dir_ / name
    p.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    p.write_text(text, encoding="utf-8")


def make_pack(tmp: Path, *, workflows: dict, agents: dict | None = None,
              tools: str = "", handlers: str = "", scenario: dict | None = None) -> Path:
    """在临时目录里造一个最小 pack。"""
    d = tmp
    _write(d, "pack.json", {
        "id": "t", "name": "测试 pack", "default_workflow": next(iter(workflows)),
        "workflows": {k: f"workflows/{k}.json" for k in workflows},
        "params": {},
    })
    for name, wf in workflows.items():
        _write(d, f"workflows/{name}.json", wf)
    _write(d, "agents.json", {"agents": agents or {}})
    if tools:
        _write(d, "tools.py", tools)
    if handlers:
        _write(d, "handlers.py", handlers)
    if scenario:
        _write(d, "scenarios.json", [scenario])
    return d


ECHO = '''
def echo(rt, ins):
    return {"text": "ok"}
HANDLERS = {"echo": echo}
'''

TOOL = '''
def noop_tool(rt, **kw):
    return {"ok": True}
TOOLS = {"t": noop_tool}
'''


class TestPermissions(unittest.TestCase):
    def test_tool_node_without_agent_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_pack(Path(tmp), workflows={"w": {
                "entry": "receive",
                "nodes": {
                    "receive": {"type": "input", "next": "call"},
                    "call": {"type": "tool", "tool": "t", "next": "done"},
                    "done": {"type": "end", "status": "ok"},
                },
            }}, tools=TOOL)
            pack = load_pack(d, cache=False)
            res, _ = run_scenario(pack, {"id": "x"})
            self.assertIn("未声明 agent", res.error)

    def test_tool_not_in_whitelist_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_pack(Path(tmp), workflows={"w": {
                "entry": "receive",
                "nodes": {
                    "receive": {"type": "input", "next": "call"},
                    "call": {"type": "tool", "agent": "a", "tool": "t", "next": "done"},
                    "done": {"type": "end", "status": "ok"},
                },
            }}, agents={"a": {"name": "a", "permissions": [], "tools": []}}, tools=TOOL)
            pack = load_pack(d, cache=False)
            res, _ = run_scenario(pack, {"id": "x"})
            self.assertIn("PermissionDenied", res.error)

    def test_unknown_agent_cannot_bypass_tool_whitelist(self):
        """旧实现里 ad is None 会跳过检查，任何未注册的 agent 名都能调工具。"""
        with tempfile.TemporaryDirectory() as tmp:
            d = make_pack(Path(tmp), workflows={"w": {
                "entry": "receive",
                "nodes": {
                    "receive": {"type": "input", "next": "call"},
                    "call": {"type": "tool", "agent": "ghost", "tool": "t", "next": "done"},
                    "done": {"type": "end", "status": "ok"},
                },
            }}, tools=TOOL)
            pack = load_pack(d, cache=False)
            res, _ = run_scenario(pack, {"id": "x"})
            self.assertIn("PermissionDenied", res.error)

    def test_permissions_field_is_enforced(self):
        """triage 声明的 read_profile 被拿掉后，handler 必须拒绝执行。"""
        clear_cache()
        pack = load_pack(PACKS / "fall-response", cache=False)
        pack.agents["triage"]["permissions"] = []
        res, _ = run_scenario(pack, {"id": "s1", "profile": "e003",
                                     "event": {"confidence": 0.93, "base_level": "mid"},
                                     "params": {"answer": [True], "answer_at": [8]}})
        self.assertIn("未被授予权限", res.error)


class TestTransitions(unittest.TestCase):
    def test_condition_without_else_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_pack(Path(tmp), workflows={"w": {
                "entry": "receive",
                "nodes": {
                    "receive": {"type": "input", "next": "c"},
                    "c": {"type": "condition", "when": "1 == 2", "then": "done"},
                    "done": {"type": "end", "status": "ok"},
                },
            }})
            pack = load_pack(d, cache=False)
            res, _ = run_scenario(pack, {"id": "x"})
            self.assertIn("没有声明 else", res.error)

    def test_endless_loop_hits_max_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_pack(Path(tmp), workflows={"w": {
                "entry": "receive",
                "nodes": {
                    "receive": {"type": "input", "next": "c"},
                    "c": {"type": "condition", "when": "1 == 1", "then": "c"},
                },
            }})
            pack = load_pack(d, cache=False)
            res, _ = run_scenario(pack, {"id": "x"})
            self.assertIn(f"超过最大步数 {MAX_STEPS}", res.error)


class TestConcurrency(unittest.TestCase):
    def test_parallel_branch_chain_runs_to_barrier(self):
        """分支是 2 节点链时，旧引擎只跑第一个节点就静默截断。"""
        with tempfile.TemporaryDirectory() as tmp:
            d = make_pack(Path(tmp), workflows={"w": {
                "entry": "receive",
                "nodes": {
                    "receive": {"type": "input", "next": "fan"},
                    "fan": {"type": "parallel", "branches": ["b1"], "next": "join"},
                    "b1": {"type": "agent", "agent": "a", "out": "o1", "next": "b1b"},
                    "b1b": {"type": "transform", "set": {"x": "1"}, "next": "join"},
                    "join": {"type": "transform", "set": {"j": "1"}, "next": "done"},
                    "done": {"type": "end", "status": "ok"},
                },
            }}, agents={"a": {"name": "a", "driver": "handler", "handler": "echo",
                              "permissions": [], "tools": []}}, handlers=ECHO)
            pack = load_pack(d, cache=False)
            res, _ = run_scenario(pack, {"id": "x"})
            self.assertEqual(res.final, "ok")
            self.assertEqual(res.trace.count(node="b1"), 1)
            self.assertEqual(res.trace.count(node="b1b"), 1)
            self.assertEqual(res.trace.count(node="join"), 1)

    def test_parallel_branch_does_not_run_join_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_pack(Path(tmp), workflows={"w": {
                "entry": "receive",
                "nodes": {
                    "receive": {"type": "input", "next": "fan"},
                    "fan": {"type": "parallel", "branches": ["b1", "b2"], "next": "join"},
                    "b1": {"type": "agent", "agent": "a", "out": "o1", "next": "join"},
                    "b2": {"type": "agent", "agent": "a", "out": "o2", "next": "join"},
                    "join": {"type": "transform", "set": {"j": "1"}, "next": "done"},
                    "done": {"type": "end", "status": "ok"},
                },
            }}, agents={"a": {"name": "a", "driver": "handler", "handler": "echo",
                              "permissions": [], "tools": []}}, handlers=ECHO)
            pack = load_pack(d, cache=False)
            res, _ = run_scenario(pack, {"id": "x"})
            self.assertEqual(res.final, "ok")
            self.assertEqual(res.trace.count(node="join"), 1)

    def test_foreach_does_not_leak_item_into_ctx(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_pack(Path(tmp), workflows={"w": {
                "entry": "receive",
                "nodes": {
                    "receive": {"type": "input", "next": "loop"},
                    "loop": {"type": "foreach", "over": "[1, 2, 3]", "body": "b",
                             "on_exhausted": "done"},
                    "b": {"type": "agent", "agent": "a", "input": {"v": "item"}, "out": "r"},
                    "done": {"type": "end", "status": "ok"},
                },
            }}, agents={"a": {"name": "a", "driver": "handler", "handler": "echo",
                              "permissions": [], "tools": []}}, handlers=ECHO)
            pack = load_pack(d, cache=False)
            res, _ = run_scenario(pack, {"id": "x"})
            self.assertEqual(res.final, "ok")
            self.assertNotIn("item", res.ctx)
            self.assertNotIn("index", res.ctx)


class TestScenarioPaths(unittest.TestCase):
    def setUp(self):
        clear_cache()
        self.pack = load_pack(PACKS / "fall-response")

    def _by_id(self, sid):
        for s in self.pack.scenarios:
            if s["id"] == sid:
                return s
        raise KeyError(sid)

    def test_night_rule_now_matches(self):
        res, _ = run_scenario(self.pack, self._by_id("s9"))
        self.assertEqual(res.final, "answered")
        self.assertEqual(res.ctx["triage"]["level"], "high")

    def test_push_failure_path_is_reachable(self):
        res, _ = run_scenario(self.pack, self._by_id("s10"))
        self.assertEqual(res.final, "push_failed")
        self.assertFalse(res.ctx["pushed"]["ok"])

    def test_dedupe_merges_on_repeat(self):
        res, _ = run_scenario(self.pack, self._by_id("s8"))
        self.assertEqual(res.final, "merged")

    def test_knowledge_hits_are_consumed_by_draft(self):
        res, _ = run_scenario(self.pack, self._by_id("s3"))
        self.assertIn("处置依据", res.ctx["draft"]["text"])
        self.assertGreater(len(res.ctx["hits"]), 0)


class TestFallVerification(unittest.TestCase):
    """跌倒核验：多信号判定 + 分级升级策略。"""

    def setUp(self):
        clear_cache()
        self.pack = load_pack(PACKS / "fall-response")

    def _sc(self, sid):
        return next(s for s in self.pack.scenarios if s["id"] == sid)

    def test_phone_drop_is_rejected_without_notifying(self):
        res, _ = run_scenario(self.pack, self._sc("s11"))
        self.assertEqual(res.final, "no_fall")
        self.assertEqual(res.ctx["verify"]["verdict"], "negligible")
        self.assertEqual(res.trace.count(node="push"), 0)

    def test_strong_multisignal_fall_is_confirmed(self):
        res, _ = run_scenario(self.pack, self._sc("s13"))
        self.assertEqual(res.ctx["verify"]["verdict"], "confirmed")
        self.assertGreaterEqual(res.ctx["verify"]["score"], 0.85)

    def test_suspected_needs_voice_check(self):
        res, _ = run_scenario(self.pack, self._sc("s12"))
        self.assertEqual(res.ctx["verify"]["verdict"], "suspected")
        self.assertTrue(res.ctx["verify"]["needs_voice"])
        self.assertEqual(res.trace.count(node="voice_check"), 1)

    def test_anticoagulant_biases_verdict_upward(self):
        """吃华法林 + 中等分 → 被安全偏置提到 confirmed。"""
        sc = {"id": "probe", "profile": "e001",
              "event": {"confidence": 0.80, "impact_g": 3.0, "freefall_ms": 150,
                        "still_ms": 2500, "orientation_delta_deg": 40, "source": "phone_sensor"}}
        res, _ = run_scenario(self.pack, sc)
        self.assertEqual(res.ctx["verify"]["verdict"], "confirmed")

    def test_escalation_budget_caps_total_wait(self):
        """两位家属都不接，响应时间必须被总预算压住，而不是 2×30s。"""
        res, _ = run_scenario(self.pack, self._sc("s2"))
        self.assertEqual(res.final, "community")
        self.assertLessEqual(res.sim_ms, 21_000, f"实际 {res.sim_ms}ms")
        self.assertTrue(res.ctx["call"]["budget_hit"])

    def test_answered_path_is_not_slower(self):
        res, _ = run_scenario(self.pack, self._sc("s1"))
        self.assertEqual(res.final, "answered")
        self.assertLessEqual(res.sim_ms, 9_000, f"实际 {res.sim_ms}ms")


class TestPackCache(unittest.TestCase):
    def test_cache_hits_and_invalidates(self):
        import harness.pack as hp

        clear_cache()
        with tempfile.TemporaryDirectory() as tmp:
            d = make_pack(Path(tmp), workflows={"w": {
                "entry": "receive",
                "nodes": {"receive": {"type": "input", "next": "done"},
                          "done": {"type": "end", "status": "ok"}},
            }})
            a = load_pack(d)
            b = load_pack(d)
            self.assertIs(a, b)
            meta = json.loads((d / "pack.json").read_text(encoding="utf-8"))
            meta["name"] = "改名了"
            (d / "pack.json").write_text(json.dumps(meta), encoding="utf-8")
            os.utime(d / "pack.json", None)
            hp._SIG.clear()          # 指纹本身有 0.75s TTL，跳过它才能立刻看到改动
            c = load_pack(d)
            self.assertIsNot(a, c)
            self.assertEqual(c.name, "改名了")


class TestAudit(unittest.TestCase):
    def test_real_packs_have_no_static_errors(self):
        clear_cache()
        for name in ("fall-response", "research-brief"):
            rep = audit_pack(load_pack(PACKS / name), run_scenarios=False)
            errs = [i for i in rep["issues"] if i["level"] == "error"]
            self.assertEqual(errs, [], f"{name}: {errs}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
