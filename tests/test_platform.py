"""tests — expr / knowledge / graph / lessons 的单元测试

engine 侧的回归在 test_harness.py；这里专门补此前零测试的三个模块。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.engine import Engine                       # noqa: E402
from harness.expr import MAX_DEPTH, Evaluator, ExprError  # noqa: E402
from harness.graph import build                          # noqa: E402
from harness.knowledge import (                          # noqa: E402
    CaseKnowledge, DocKnowledge, KnowledgeHub, RuleKnowledge, _score,
)
from harness.lessons import add_lesson, load_lessons     # noqa: E402
from harness.pack import clear_cache, load_pack          # noqa: E402

PACKS = ROOT / "packs"


def ev(**names):
    return Evaluator(dict(names))


class TestExpr(unittest.TestCase):
    def test_literals_pass_through(self):
        e = ev()
        for v in (1, 1.5, True, None):
            self.assertIs(e.eval(v), v)

    def test_empty_string_is_none(self):
        self.assertIsNone(ev().eval("   "))

    def test_dict_attribute_and_subscript(self):
        e = ev(ctx={"a": {"b": [10, 20]}})
        self.assertEqual(e.eval("ctx.a.b"), [10, 20])
        self.assertEqual(e.eval("ctx.a.b[1]"), 20)
        self.assertEqual(e.eval("len(ctx.a.b)"), 2)

    def test_missing_key_raises(self):
        with self.assertRaises(ExprError):
            ev(ctx={"a": {}}).eval("ctx.a.nope")

    def test_underscore_attribute_blocked(self):
        with self.assertRaises(ExprError):
            ev(ctx={"__x": 1}).eval("ctx.__x")

    def test_only_whitelisted_calls(self):
        self.assertEqual(ev(x=[1, 2]).eval("sum(x)"), 3)
        with self.assertRaises(ExprError):
            ev().eval("open('x')")

    def test_method_call_blocked(self):
        with self.assertRaises(ExprError):
            ev().eval("'a'.upper()")

    def test_lambda_and_comprehension_blocked(self):
        with self.assertRaises(ExprError):
            ev().eval("(lambda: 1)()")
        with self.assertRaises(ExprError):
            ev().eval("[x for x in [1]]")

    def test_chained_compare_and_ternary(self):
        self.assertTrue(ev().eval("1 < 2 < 3"))
        self.assertEqual(ev().eval("'a' if 1 else 'b'"), "a")

    def test_boolean_short_circuit(self):
        # boom 未定义；and 短路后不应该被求值
        self.assertFalse(ev().eval("False and boom"))

    def test_depth_limit(self):
        deep = "[" * (MAX_DEPTH + 4) + "]" * (MAX_DEPTH + 4)
        with self.assertRaises(ExprError):
            ev().eval(deep)

    def test_binary_ops(self):
        self.assertEqual(ev().eval("2 * 3 + 4 % 3 - 1"), 6)

    def test_ast_is_cached(self):
        e = ev(ctx={"a": 1})
        e.eval("ctx.a == 1")
        e.eval("ctx.a == 1")
        self.assertEqual(len(e._cache), 1)

    def test_syntax_error_is_wrapped(self):
        with self.assertRaises(ExprError):
            ev().eval("1 +")


class TestKnowledge(unittest.TestCase):
    def test_score_identical_and_disjoint(self):
        self.assertAlmostEqual(_score("跌倒", "跌倒"), 1.0, places=3)
        self.assertEqual(_score("跌倒", "高血压"), 0.0)

    def test_doc_chunks_by_heading(self):
        clear_cache()
        pack = load_pack(PACKS / "fall-response")
        doc = DocKnowledge(pack.kb_dir)
        self.assertEqual(len(doc.chunks), 9)          # 3 篇 × 3 个小节
        self.assertTrue(all(c["id"].count("#") == 1 for c in doc.chunks))

    def test_rule_knowledge_hits_rule_id(self):
        clear_cache()
        pack = load_pack(PACKS / "fall-response")
        rk = RuleKnowledge(pack.data["rules"]["rules"])
        hits = rk.search("抗凝 出血 搬动", k=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["id"], "R-ANTICOAG")

    def test_hub_sorts_by_score_and_merges_sources(self):
        hub = KnowledgeHub()
        hub.register(RuleKnowledge([{"id": "R1", "desc": "跌倒", "then": {}}]))
        hub.register(CaseKnowledge(items=[{"id": "L1", "title": "跌倒 复盘",
                                           "text": "跌倒 处理"}]))
        hits = hub.search("跌倒", k=5)
        self.assertGreaterEqual(len(hits), 2)
        scores = [h["score"] for h in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual({h["kind"] for h in hits}, {"rule", "case"})

    def test_pack_registers_three_sources(self):
        clear_cache()
        pack = load_pack(PACKS / "fall-response")
        hub = Engine(pack, shared={}).hub
        kinds = [s.stats()["kind"] for s in hub.sources]
        self.assertEqual(kinds, ["rule", "doc", "case"])

    def test_case_knowledge_is_not_empty(self):
        clear_cache()
        pack = load_pack(PACKS / "fall-response")
        hub = Engine(pack, shared={}).hub
        cases = hub.get("case")
        self.assertGreaterEqual(cases.stats()["entries"], 2)


class TestGraph(unittest.TestCase):
    def test_linear_chain_ranks(self):
        wf = {"entry": "a", "nodes": {
            "a": {"type": "input", "next": "b"},
            "b": {"type": "transform", "set": {}, "next": "c"},
            "c": {"type": "end", "status": "ok"},
        }}
        g = build(wf)
        rank = {n["id"]: n["rank"] for n in g["nodes"]}
        self.assertEqual(rank, {"a": 0, "b": 1, "c": 2})
        self.assertEqual(len(g["edges"]), 2)
        self.assertEqual([n["id"] for n in g["nodes"] if n["entry"]], ["a"])

    def test_parallel_branches_and_join(self):
        wf = {"entry": "in", "nodes": {
            "in": {"type": "input", "next": "fan"},
            "fan": {"type": "parallel", "branches": ["b1", "b2"], "next": "join"},
            "b1": {"type": "agent", "agent": "x", "out": "o1", "next": None},
            "b2": {"type": "agent", "agent": "x", "out": "o2", "next": None},
            "join": {"type": "transform", "set": {}, "next": "end"},
            "end": {"type": "end", "status": "ok"},
        }}
        g = build(wf)
        labels = sorted(e["label"] for e in g["edges"])
        self.assertIn("并行", labels)
        self.assertIn("汇合", labels)
        rank = {n["id"]: n["rank"] for n in g["nodes"]}
        self.assertEqual(rank["fan"], 1)
        self.assertEqual(rank["b1"], 2)
        self.assertGreater(rank["join"], rank["b1"])

    def test_back_edge_marked_and_rank_not_inflated(self):
        wf = {"entry": "a", "nodes": {
            "a": {"type": "input", "next": "b"},
            "b": {"type": "condition", "when": "1", "then": "a", "else": "end"},
            "end": {"type": "end", "status": "ok"},
        }}
        g = build(wf)
        back = [(e["from"], e["to"]) for e in g["edges"] if e["back"]]
        self.assertIn(("b", "a"), back)
        rank = {n["id"]: n["rank"] for n in g["nodes"]}
        self.assertEqual(rank["a"], 0)          # 回边不参与分层

    def test_out_field_is_exported(self):
        g = build({"entry": "a", "nodes": {
            "a": {"type": "agent", "agent": "x", "out": "triage", "next": None}}})
        self.assertEqual(g["nodes"][0]["out"], "triage")

    def test_long_chain_folds_into_bands(self):
        """29 个节点的链不能拉成一条纸带：超过 6 列必须折行。"""
        nodes = {"n0": {"type": "input", "next": "n1"}}
        for i in range(1, 13):
            nodes[f"n{i}"] = {"type": "transform", "set": {}, "next": f"n{i + 1}"}
        nodes["n13"] = {"type": "end", "status": "ok"}
        g = build({"entry": "n0", "nodes": nodes})
        self.assertIn("band", g["nodes"][0])
        self.assertEqual({n["band"] for n in g["nodes"]}, {0, 1, 2})
        self.assertEqual(max(n["x"] for n in g["nodes"]), 5 * 210)
        self.assertGreater(g["height"], 2 * 92)

    def test_dimensions_positive(self):
        g = build({"entry": "a", "nodes": {
            "a": {"type": "input", "next": "b"}, "b": {"type": "end", "status": "ok"}}})
        self.assertGreater(g["width"], 0)
        self.assertGreater(g["height"], 0)

    def test_unknown_target_edges_are_dropped(self):
        g = build({"entry": "a", "nodes": {
            "a": {"type": "input", "next": "ghost"}}})
        self.assertEqual(g["edges"], [])


class TestLessons(unittest.TestCase):
    def setUp(self):
        clear_cache()
        self.tmp = tempfile.mkdtemp()
        self.dir = Path(self.tmp) / "fall-response"
        shutil.copytree(PACKS / "fall-response", self.dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_lesson_appends_and_ids_increment(self):
        pack = load_pack(self.dir, cache=False)
        before = len(load_lessons(pack))
        item = add_lesson(pack, "新结论", "跌倒后先看静止时长", source="test")
        self.assertEqual(item["id"], f"L{before + 1}")
        fresh = load_pack(self.dir, cache=False)
        self.assertEqual(len(load_lessons(fresh)), before + 1)

    def test_lesson_is_retrievable_after_write(self):
        pack = load_pack(self.dir, cache=False)
        add_lesson(pack, "夜间跌倒优先叫社区", "夜间跌倒发现延迟最长，先叫社区再叫家属。")
        fresh = load_pack(self.dir, cache=False)
        hub = Engine(fresh, shared={}).hub
        hits = hub.search("夜间跌倒", k=5)
        self.assertTrue(any(h["kind"] == "case" and "夜间" in h["title"] for h in hits))


class TestVersion(unittest.TestCase):
    def test_version_matches_pyproject(self):
        import re

        import harness

        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        self.assertIsNotNone(m, "pyproject.toml 里找不到 version")
        self.assertEqual(harness.__version__, m.group(1))

    def test_requires_python_matches_readme_claim(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('requires-python = ">=3.10"', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
