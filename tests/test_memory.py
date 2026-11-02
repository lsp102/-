"""tests — 持久记忆（memory 节点 + MemoryStore）

覆盖三件事：存储本身的读写/原子性、引擎节点的语义、以及“跨运行真的记得住”。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.engine import Engine                         # noqa: E402
from harness.memory import (MemoryPolicy, MemoryStore,    # noqa: E402
                            clear_stores, store_for)
from harness.pack import clear_cache, load_pack           # noqa: E402
from harness.runner import run_scenario                   # noqa: E402

PACKS = ROOT / "packs"


def _write(d: Path, name: str, obj):
    p = d / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False),
                 encoding="utf-8")


def make_pack(tmp: Path, nodes: dict, memory_cfg: dict | None = None) -> Path:
    meta = {"id": "t", "name": "t", "default_workflow": "w",
            "workflows": {"w": "workflows/w.json"}, "params": {}}
    if memory_cfg is not None:
        meta["memory"] = memory_cfg
    _write(tmp, "pack.json", meta)
    _write(tmp, "workflows/w.json", {"entry": "receive", "nodes": nodes})
    _write(tmp, "agents.json", {"agents": {}})
    return tmp


class TestMemoryStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "p.json"
        self.store = MemoryStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_set_get_incr_append_delete(self):
        s = self.store
        self.assertEqual(s.get("missing"), None)
        self.assertEqual(s.get("missing", 0), 0)
        s.set("a", {"x": 1})
        self.assertEqual(s.get("a"), {"x": 1})
        self.assertEqual(s.incr("n"), 1)
        self.assertEqual(s.incr("n", 2), 3)
        self.assertEqual(s.append("log", "e1"), ["e1"])
        self.assertEqual(s.append("log", "e2"), ["e1", "e2"])
        self.assertTrue(s.delete("a"))
        self.assertFalse(s.delete("a"))
        self.assertEqual(s.keys(), sorted(["n", "log"]))

    def test_append_maxlen(self):
        for i in range(12):
            self.store.append("log", i, maxlen=5)
        self.assertEqual(self.store.get("log"), [7, 8, 9, 10, 11])

    def test_file_is_valid_json_and_no_tmp_left(self):
        self.store.set("k", "值")
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn("值", raw)
        json.loads(raw)                                    # 不抛即合法
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def test_reload_from_disk(self):
        self.store.set("k", 42)
        other = MemoryStore(self.path)                     # 新实例，从盘上读
        self.assertEqual(other.get("k"), 42)

    def test_concurrent_incr_does_not_lose_updates(self):
        def work():
            for _ in range(100):
                self.store.incr("n")
        ts = [threading.Thread(target=work) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(self.store.get("n"), 800)

    def test_corrupt_file_degrades_to_empty(self):
        self.path.write_text("{ 这不是 json", encoding="utf-8")
        self.assertEqual(MemoryStore(self.path).get("k"), None)


class TestMemoryNode(unittest.TestCase):
    def setUp(self):
        clear_cache()
        clear_stores()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"

    def tearDown(self):
        clear_stores()
        self.tmp.cleanup()

    def _run(self, nodes, sc=None, state=None):
        d = make_pack(Path(self.tmp.name) / "pack", nodes)
        pack = load_pack(d, cache=False)
        return run_scenario(pack, sc or {"id": "x"}, state_dir=str(state or self.state))[0]

    def test_set_then_get_roundtrip(self):
        res = self._run({
            "receive": {"type": "input", "next": "w"},
            "w": {"type": "memory", "op": "set", "key": "k",
                  "value_expr": "'hello'", "out": "w", "next": "r"},
            "r": {"type": "memory", "op": "get", "key": "k", "out": "got", "next": "done"},
            "done": {"type": "end", "status": "ok"},
        })
        self.assertEqual(res.final, "ok")
        self.assertEqual(res.ctx["got"], "hello")

    def test_incr_persists_across_engines(self):
        nodes = {
            "receive": {"type": "input", "next": "inc"},
            "inc": {"type": "memory", "op": "incr", "key": "n", "out": "n", "next": "done"},
            "done": {"type": "end", "status": "ok"},
        }
        first = self._run(nodes)
        second = self._run(nodes)          # 全新 pack / 全新 Engine，同一个状态目录
        self.assertEqual(first.ctx["n"], 1)
        self.assertEqual(second.ctx["n"], 2)

    def test_different_state_dirs_are_isolated(self):
        nodes = {
            "receive": {"type": "input", "next": "inc"},
            "inc": {"type": "memory", "op": "incr", "key": "n", "out": "n", "next": "done"},
            "done": {"type": "end", "status": "ok"},
        }
        a = self._run(nodes, state=Path(self.tmp.name) / "s1")
        b = self._run(nodes, state=Path(self.tmp.name) / "s2")
        self.assertEqual(a.ctx["n"], 1)
        self.assertEqual(b.ctx["n"], 1)

    def test_key_expression(self):
        res = self._run({
            "receive": {"type": "input", "next": "inc"},
            "inc": {"type": "memory", "op": "incr", "key_expr": "'falls:' + event.elder",
                    "out": "n", "next": "done"},
            "done": {"type": "end", "status": "ok"},
        }, sc={"id": "x", "event": {"elder": "e9"}})
        self.assertEqual(res.ctx["n"], 1)
        self.assertEqual(json.loads((self.state / "t.json").read_text(encoding="utf-8"))["falls:e9"], 1)

    def test_missing_key_raises(self):
        res = self._run({
            "receive": {"type": "input", "next": "m"},
            "m": {"type": "memory", "op": "get", "out": "v", "next": "done"},
            "done": {"type": "end", "status": "ok"},
        })
        self.assertIn("缺少 key", res.error)

    def test_unknown_op_raises(self):
        res = self._run({
            "receive": {"type": "input", "next": "m"},
            "m": {"type": "memory", "op": "explode", "key": "k", "next": "done"},
            "done": {"type": "end", "status": "ok"},
        })
        self.assertIn("memory 操作未知", res.error)

    def test_default_when_key_absent(self):
        res = self._run({
            "receive": {"type": "input", "next": "m"},
            "m": {"type": "memory", "op": "get", "key": "nope", "default": 0,
                  "out": "v", "next": "done"},
            "done": {"type": "end", "status": "ok"},
        })
        self.assertEqual(res.ctx["v"], 0)


class TestRealPackMemory(unittest.TestCase):
    def setUp(self):
        clear_cache()
        clear_stores()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.pack = load_pack(PACKS / "fall-response")

    def tearDown(self):
        clear_stores()
        self.tmp.cleanup()

    def _sc(self, sid):
        return next(s for s in self.pack.scenarios if s["id"] == sid)

    def test_confirmed_falls_are_counted_per_elder(self):
        r1, _ = run_scenario(self.pack, self._sc("s13"), state_dir=self.state)
        r2, _ = run_scenario(self.pack, self._sc("s13"), state_dir=self.state)
        self.assertEqual(r1.ctx["mem_falls"], 1)
        self.assertEqual(r2.ctx["mem_falls"], 2)
        data = json.loads((Path(self.state) / "fall-response.json").read_text(encoding="utf-8"))
        self.assertEqual(data["falls:e002:confirmed"], 2)

    def test_suspected_is_counted_separately(self):
        run_scenario(self.pack, self._sc("s13"), state_dir=self.state)
        run_scenario(self.pack, self._sc("s12"), state_dir=self.state)
        data = json.loads((Path(self.state) / "fall-response.json").read_text(encoding="utf-8"))
        self.assertEqual(data["falls:e002:confirmed"], 1)
        self.assertEqual(data["falls:e002:suspected"], 1)

    def test_rejected_event_is_not_counted(self):
        res, _ = run_scenario(self.pack, self._sc("s11"), state_dir=self.state)
        self.assertEqual(res.final, "no_fall")
        self.assertNotIn("mem_falls", res.ctx)
        self.assertFalse((Path(self.state) / "fall-response.json").exists())


class TestSelectivePersistence(unittest.TestCase):
    """只有策略允许的键才落盘，其余只在进程内。"""

    def setUp(self):
        clear_stores()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.path = self.state / "t.json"   # make_pack 的 pack id 是 t

    def tearDown(self):
        clear_stores()
        self.tmp.cleanup()

    def test_policy_allows(self):
        p = MemoryPolicy.from_config({"mode": "declared", "allow": ["falls:*"],
                                      "deny": ["falls:debug:*"]})
        self.assertTrue(p.allows("falls:e1:confirmed"))
        self.assertFalse(p.allows("falls:debug:x"))       # deny 优先
        self.assertFalse(p.allows("session:x"))
        self.assertTrue(p.allows("session:x", override=True))   # 节点强制

        self.assertTrue(MemoryPolicy(mode="all").allows("anything"))
        self.assertFalse(MemoryPolicy(mode="off").allows("anything"))
        self.assertTrue(MemoryPolicy(mode="off").allows("x", override=True))
        self.assertEqual(MemoryPolicy.from_config({"mode": "乱七八糟"}).mode, "all")

    def test_declared_mode_persists_only_allowed(self):
        policy = MemoryPolicy.from_config({"mode": "declared", "allow": ["falls:*"]})
        self.state.mkdir(parents=True, exist_ok=True)
        st = MemoryStore(self.path, policy)
        st.incr("falls:e1", 1)
        st.set("session:tmp", "hello")

        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["falls:e1"], 1)
        self.assertNotIn("session:tmp", on_disk)
        # 进程内仍然读得到
        self.assertEqual(st.get("session:tmp"), "hello")
        self.assertIn("session:tmp", st.session_snapshot())

    def test_ephemeral_key_disappears_after_restart(self):
        policy = MemoryPolicy.from_config({"mode": "declared", "allow": ["falls:*"]})
        self.state.mkdir(parents=True, exist_ok=True)
        st = MemoryStore(self.path, policy)
        st.incr("falls:e1", 1)
        st.set("session:tmp", "hello")

        fresh = MemoryStore(self.path, policy)         # 模拟进程重启
        self.assertEqual(fresh.get("falls:e1"), 1)
        self.assertIsNone(fresh.get("session:tmp"))

    def test_off_mode_persists_nothing(self):
        self.state.mkdir(parents=True, exist_ok=True)
        st = MemoryStore(self.path, MemoryPolicy(mode="off"))
        st.set("k", 1)
        self.assertFalse(self.path.exists())

    def test_node_override_forces_persist(self):
        res = run_scenario(*self._pack({
            "receive": {"type": "input", "next": "m"},
            "m": {"type": "memory", "op": "set", "key": "forced", "value": 7,
                  "persist": True, "out": "v", "next": "done"},
            "done": {"type": "end", "status": "ok"},
        }), state_dir=str(self.state))[0]
        self.assertEqual(res.final, "ok")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["forced"], 7)

    def test_node_override_forces_ephemeral(self):
        res = run_scenario(*self._pack({
            "receive": {"type": "input", "next": "m"},
            "m": {"type": "memory", "op": "set", "key": "tmp", "value": 1,
                  "persist": False, "out": "v", "next": "done"},
            "done": {"type": "end", "status": "ok"},
        }, memory_cfg={"mode": "all"}))[0]
        self.assertEqual(res.final, "ok")
        self.assertFalse(self.path.exists())

    def _pack(self, nodes, memory_cfg=None):
        d = make_pack(Path(self.tmp.name) / "p", nodes, memory_cfg=memory_cfg)
        return load_pack(d, cache=False), {"id": "x"}


class TestRealPackPolicy(unittest.TestCase):
    def setUp(self):
        clear_stores()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        clear_cache()
        self.pack = load_pack(PACKS / "fall-response")

    def tearDown(self):
        clear_stores()
        self.tmp.cleanup()

    def test_only_falls_are_on_disk(self):
        sc = next(s for s in self.pack.scenarios if s["id"] == "s13")
        run_scenario(self.pack, sc, state_dir=self.state)
        data = json.loads((Path(self.state) / "fall-response.json").read_text(encoding="utf-8"))
        self.assertIn("falls:e002:confirmed", data)
        self.assertNotIn("session:last_verdict", data)

    def test_session_key_visible_in_run_but_not_persisted(self):
        sc = next(s for s in self.pack.scenarios if s["id"] == "s13")
        res, eng = run_scenario(self.pack, sc, state_dir=self.state)
        self.assertEqual(res.ctx["mem_last_verdict"], "confirmed")
        self.assertEqual(eng.memory.get("session:last_verdict"), "confirmed")
        self.assertFalse(eng.memory.is_persisted("session:last_verdict"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
