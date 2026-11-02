"""server.py — HTTP 控制台（新引擎）

    python -m harness.server 8000
    浏览器打开 http://127.0.0.1:8000

接口：
    GET  /                     控制台页面
    GET  /api/packs            pack 列表
    GET  /api/pack?id=         pack 详情（含各工作流的节点图）
    POST /api/run              {pack, scenario, workflow, pace_ms} → {run_id}
    GET  /api/run?id=&since=   增量轨迹
    POST /api/cancel           {run_id}
    GET  /api/runs             最近运行
"""
from __future__ import annotations

import json
import hmac
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from harness.engine import check_expect              # noqa: E402
from harness.graph import build as build_graph       # noqa: E402
from harness.pack import discover, load_pack         # noqa: E402
from harness.runner import run_scenario              # noqa: E402

PACKS_DIR = BASE / "packs"
MAX_RUNS = 80
# 可选访问令牌：设置 HARNESS_TOKEN 后，/api/* 需要带 X-Harness-Token 头或 ?token=
TOKEN = os.environ.get("HARNESS_TOKEN", "")


class Run:
    def __init__(self, pack, scenario: dict, workflow: str):
        self.id = uuid.uuid4().hex[:8]
        self.pack_id = pack.id
        self.scenario_id = str(scenario.get("id", ""))
        self.title = scenario.get("title", "")
        self.workflow = workflow
        self.status = "running"
        self.steps: list[dict] = []
        self.result: dict | None = None
        self.error = ""
        self.started = time.time()
        self.expect = dict(scenario.get("expect") or {})
        self.lock = threading.Lock()
        self.engine: Engine | None = None

    def add(self, step: dict):
        with self.lock:
            self.steps.append(step)

    def slice(self, since: int):
        with self.lock:
            return list(self.steps[since:]), len(self.steps)

    def summary(self) -> dict:
        return {
            "run_id": self.id,
            "pack": self.pack_id,
            "scenario": self.scenario_id,
            "title": self.title,
            "workflow": self.workflow,
            "status": self.status,
            "steps": len(self.steps),
            "final": (self.result or {}).get("final", ""),
            "sim_ms": (self.result or {}).get("sim_ms", 0),
            "pass": (self.result or {}).get("expect", {}).get("pass"),
        }


RUNS: dict[str, Run] = {}
RUNS_ORDER: list[str] = []
RUNS_LOCK = threading.Lock()


def _remember(run: Run):
    with RUNS_LOCK:
        RUNS[run.id] = run
        RUNS_ORDER.append(run.id)
        while len(RUNS_ORDER) > MAX_RUNS:
            old = RUNS_ORDER.pop(0)
            RUNS.pop(old, None)


def _execute(run: Run, pack, scenario: dict, workflow: str, pace_ms: int):
    try:
        # 走统一执行器：控制台以前绕过了 runner，导致 s4 不撤销、s8 不去重
        res, eng = run_scenario(pack, scenario, workflow=workflow,
                                pace_ms=pace_ms, on_step=run.add)
        run.engine = eng
        fails = check_expect(res, run.expect)
        run.result = {
            "final": res.final,
            "sim_ms": res.sim_ms,
            "error": res.error,
            "ctx_keys": sorted(res.ctx.keys()),
            "expect": {"pass": (not fails) and not res.error, "fails": fails},
            "trace_summary": res.trace.summary() if res.trace else {},
            # 跌倒核验的结构化结果，前端拿它画评分卡（不再从轨迹摘要里猜）
            "verify": res.ctx.get("verify"),
        }
        run.status = "done" if not res.error else "error"
    except Exception as e:  # noqa: BLE001
        run.status = "error"
        run.error = f"{type(e).__name__}: {e}"


class Handler(BaseHTTPRequestHandler):
    server_version = "FallGuardHarness/0.2"

    def log_message(self, *a):
        pass

    # ---------------------------------------------------------------- 基础

    def _send(self, code: int, body: str, ctype: str = "application/json"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------------------------------------------------------------- GET

    def _authorized(self, q: dict) -> bool:
        if not TOKEN:
            return True
        got = self.headers.get("X-Harness-Token") or (q.get("token", [""])[0])
        return hmac.compare_digest(str(got), TOKEN)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path in ("/", "/index.html"):
            html = (BASE / "web" / "index.html").read_text(encoding="utf-8")
            return self._send(200, html, "text/html")

        if u.path.startswith("/api/") and not self._authorized(q):
            return self._json({"error": "unauthorized"}, 401)

        if u.path == "/api/packs":
            out = []
            for p in _all_packs():
                out.append({
                    "id": p.id, "name": p.name,
                    "workflows": list(p.workflows),
                    "default_workflow": p.meta.get("default_workflow") or next(iter(p.workflows), ""),
                    "agents": len(p.agents), "tools": len(p.tools),
                    "scenarios": [{"id": s.get("id"), "title": s.get("title", "")}
                                  for s in p.scenarios],
                })
            return self._json(out)

        if u.path == "/api/pack":
            pid = q.get("id", [""])[0]
            pack = _find_pack(pid)
            if pack is None:
                return self._json({"error": "pack not found"}, 404)
            graphs = {}
            for name, wf in pack.workflows.items():
                graphs[name] = build_graph(wf)
            return self._json({
                "id": pack.id, "name": pack.name,
                "params": pack.params(),
                "default_workflow": pack.meta.get("default_workflow") or next(iter(pack.workflows), ""),
                "workflows": graphs,
                "agents": pack.agents,
                "tools": sorted(pack.tools),
                "scenarios": pack.scenarios,
            })

        if u.path == "/api/run":
            rid = q.get("id", [""])[0]
            run = RUNS.get(rid)
            if run is None:
                return self._json({"error": "run not found"}, 404)
            since = int(q.get("since", ["0"])[0] or 0)
            steps, total = run.slice(since)
            return self._json({
                "run_id": run.id,
                "status": run.status,
                "steps": steps,
                "total_steps": total,
                "result": run.result,
                "error": run.error,
            })

        if u.path == "/api/runs":
            with RUNS_LOCK:
                ids = list(RUNS_ORDER)[-20:]
            return self._json([RUNS[i].summary() for i in reversed(ids) if i in RUNS])

        return self._json({"error": "not found"}, 404)

    # ---------------------------------------------------------------- POST

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/api/") and not self._authorized(q):
            return self._json({"error": "unauthorized"}, 401)
        b = self._body()

        if u.path == "/api/run":
            pack = _find_pack(b.get("pack", ""))
            if pack is None:
                return self._json({"error": "pack not found"}, 400)
            sid = str(b.get("scenario", ""))
            sc = pack.scenario(sid)
            if sc is None:
                return self._json({"error": "scenario not found"}, 400)
            wf = b.get("workflow") or pack.meta.get("default_workflow") \
                or next(iter(pack.workflows))
            if wf not in pack.workflows:
                return self._json({"error": "workflow not found"}, 400)
            pace = int(b.get("pace_ms") or 0)
            run = Run(pack, sc, wf)
            _remember(run)
            threading.Thread(target=_execute, args=(run, pack, sc, wf, pace),
                             daemon=True).start()
            return self._json({"run_id": run.id})

        if u.path == "/api/cancel":
            run = RUNS.get(b.get("run_id", ""))
            if run and run.engine:
                run.engine.cancel()
                return self._json({"ok": True})
            return self._json({"ok": False}, 404)

        return self._json({"error": "not found"}, 404)


_PACK_CACHE: dict[str, object] = {}


def _all_packs() -> list:
    """pack 加载本身按 mtime 缓存（harness.pack），这里只做 id → Pack 索引。"""
    out = []
    for d in discover(PACKS_DIR):
        try:
            out.append(load_pack(d))
        except Exception:
            continue
    for p in out:
        _PACK_CACHE[p.id] = p
    return out


def _find_pack(pid: str):
    if pid in _PACK_CACHE:
        return _PACK_CACHE[pid]
    _all_packs()
    return _PACK_CACHE.get(pid)


def main(argv: list[str]) -> int:
    port = int(argv[0]) if argv else int(os.environ.get("PORT", "8000"))
    # 本地默认只监听回环；容器/PaaS 通过 HOST=0.0.0.0 放开
    host = os.environ.get("HOST", "127.0.0.1")
    srv = ThreadingHTTPServer((host, port), Handler)
    shown = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    print("FallGuard Harness · 控制台已启动")
    print(f"  http://{shown}:{port}   (bind {host})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
