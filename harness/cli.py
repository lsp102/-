"""cli.py — 平台命令行

    python -m platform.cli list
    python -m platform.cli info packs/fall-response
    python -m platform.cli run packs/fall-response s3
    python -m platform.cli validate packs/fall-response
    python -m platform.cli validate packs/fall-response --workflow single
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

from .engine import Engine, check_expect
from .pack import discover, load_pack

ROOT = Path(__file__).resolve().parent.parent
PACKS = ROOT / "packs"


def _utf8():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass


def _load(name: str):
    p = Path(name)
    if (p / "pack.json").exists():
        return load_pack(p)
    cand = PACKS / name
    if (cand / "pack.json").exists():
        return load_pack(cand)
    hits = [d for d in discover(PACKS) if d.name == name]
    if hits:
        return load_pack(hits[0])
    raise SystemExit(f"找不到 pack: {name}")


def _run(pack, sc: dict, workflow: str):
    eng = Engine(pack, shared={})
    res = None
    for _ in range(int(sc.get("repeat", 1))):
        delay = (sc.get("params") or {}).get("cancel_after_ms")
        timer = None
        if delay:
            timer = threading.Timer(float(delay) / 1000.0, eng.cancel)
            timer.daemon = True
            timer.start()
        res = eng.run(sc, workflow=workflow)
        if timer:
            timer.cancel()
    return res


def cmd_list(_a):
    packs = discover(PACKS)
    if not packs:
        print("没有找到任何 pack")
        return 0
    for d in packs:
        p = load_pack(d)
        print(f"{p.id:<20} {p.name}")
        print(f"{'':20} workflows={list(p.workflows)} agents={len(p.agents)} "
              f"tools={len(p.tools)} scenarios={len(p.scenarios)}")
    return 0


def cmd_info(a):
    p = _load(a.pack)
    print(json.dumps(p.info(), ensure_ascii=False, indent=2))
    return 0


def cmd_run(a):
    p = _load(a.pack)
    sc = p.scenario(a.scenario)
    if sc is None:
        raise SystemExit(f"场景不存在: {a.scenario}；可选 {[s['id'] for s in p.scenarios]}")
    res = _run(p, sc, a.workflow or p.meta.get("default_workflow") or next(iter(p.workflows)))
    print(f"pack={res.pack_id} scenario={res.scenario_id} workflow={res.workflow}")
    print(f"final={res.final}  sim={res.sim_ms}ms  steps={len(res.steps())}")
    if res.error:
        print(f"ERROR {res.error}")
    print("-" * 88)
    for s in res.steps():
        act = {"ok": "·", "veto": "!", "cancel": "×", "error": "E"}.get(s["status"], "·")
        print(f"{act} [{s['actor'] or '-':<10}] {s['node']:<16} {s['summary']}")
    fails = check_expect(res, sc.get("expect") or {})
    print("-" * 88)
    print("断言：" + ("全部通过" if not fails else f"{len(fails)} 项失败"))
    for f in fails:
        print(f"  ✗ {f}")
    return 0 if not fails else 1


def cmd_validate(a):
    p = _load(a.pack)
    wf = a.workflow or p.meta.get("default_workflow") or next(iter(p.workflows))
    print(f"pack={p.id}  workflow={wf}  场景数={len(p.scenarios)}")
    print("-" * 92)
    print(f"{'场景':<6} {'说明':<32} {'结局':<10} {'响应(s)':>8}  结果")
    passed = 0
    for sc in p.scenarios:
        res = _run(p, sc, wf)
        fails = check_expect(res, sc.get("expect") or {})
        ok = not fails and not res.error
        passed += 1 if ok else 0
        title = (sc.get("title") or "")[:30]
        print(f"{str(sc.get('id')):<6} {title:<32} {res.final:<10} "
              f"{res.sim_ms / 1000:>8.1f}  {'通过' if ok else '失败: ' + '; '.join(fails or [res.error])}")
    print("-" * 92)
    print(f"通过 {passed}/{len(p.scenarios)}")
    return 0 if passed == len(p.scenarios) else 1


def main(argv=None):
    _utf8()
    ap = argparse.ArgumentParser(prog="platform.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(fn=cmd_list)

    p = sub.add_parser("info"); p.add_argument("pack"); p.set_defaults(fn=cmd_info)

    p = sub.add_parser("run")
    p.add_argument("pack"); p.add_argument("scenario")
    p.add_argument("--workflow")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("validate")
    p.add_argument("pack")
    p.add_argument("--workflow")
    p.set_defaults(fn=cmd_validate)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
