"""cli.py — 平台命令行

    python -m harness.cli list
    python -m harness.cli info packs/fall-response
    python -m harness.cli run packs/fall-response s3
    python -m harness.cli validate packs/fall-response
    python -m harness.cli validate packs/fall-response --workflow single
    python -m harness.cli audit packs/fall-response
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from .audit import audit_pack
from .engine import check_expect
from .pack import discover, load_pack
from .runner import run_scenario

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


def _run(pack, sc: dict, workflow: str, state_dir: str | None = None):
    res, _eng = run_scenario(pack, sc, workflow, state_dir=state_dir)
    return res


def _sandbox_state():
    """验证 / 导出用的隔离状态目录：不让 validate 把演示用的计数刷上去。

    只在本次调用内有效，退出即删；`run` 与 HTTP 控制台仍然用真实的
    `.harness_state/`（那才是“持久记忆”该被看见的地方）。
    """
    return tempfile.TemporaryDirectory(prefix="harness_verify_")


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
    sandbox = _sandbox_state()
    wf = a.workflow or p.meta.get("default_workflow") or next(iter(p.workflows))
    print(f"pack={p.id}  workflow={wf}  场景数={len(p.scenarios)}")
    print("-" * 92)
    print(f"{'场景':<6} {'说明':<32} {'结局':<10} {'响应(s)':>8}  结果")
    passed = 0
    for sc in p.scenarios:
        res = _run(p, sc, wf, state_dir=sandbox.name)
        fails = check_expect(res, sc.get("expect") or {})
        ok = not fails and not res.error
        passed += 1 if ok else 0
        title = (sc.get("title") or "")[:30]
        print(f"{str(sc.get('id')):<6} {title:<32} {res.final:<10} "
              f"{res.sim_ms / 1000:>8.1f}  {'通过' if ok else '失败: ' + '; '.join(fails or [res.error])}")
    print("-" * 92)
    print(f"通过 {passed}/{len(p.scenarios)}")
    return 0 if passed == len(p.scenarios) else 1


def cmd_audit(a):
    p = _load(a.pack)
    rep = audit_pack(p, run_scenarios=not a.static_only)
    print(f"pack={rep['pack']}  规则 {len(rep['rules_covered'])}/{rep['rules_total']} 被场景覆盖")
    print(f"记忆策略: {rep.get('memory', '—')}")
    print(f"场景实际走到的结局: {', '.join(rep['finals']) or '—'}")
    print("-" * 92)
    if not rep["issues"]:
        print("没有发现问题")
        return 0
    mark = {"error": "✗", "warn": "!"}
    for it in rep["issues"]:
        print(f"{mark.get(it['level'], '·')} [{it['code']:<20}] {it['where']:<24} {it['msg']}")
    errs = sum(1 for it in rep["issues"] if it["level"] == "error")
    print("-" * 92)
    print(f"错误 {errs} 项，警告 {len(rep['issues']) - errs} 项")
    return 0 if errs == 0 else 1


def cmd_dump(a):
    """把一次运行导出成可贴进材料/复盘的东西。"""
    p = _load(a.pack)
    sandbox = _sandbox_state()
    sc = p.scenario(a.scenario)
    if sc is None:
        raise SystemExit(f"场景不存在: {a.scenario}")
    wf = a.workflow or p.meta.get("default_workflow") or next(iter(p.workflows))
    res, _eng = run_scenario(p, sc, wf, state_dir=sandbox.name)
    fails = check_expect(res, sc.get("expect") or {})
    payload = {
        "pack": res.pack_id,
        "scenario": res.scenario_id,
        "workflow": res.workflow,
        "final": res.final,
        "sim_ms": res.sim_ms,
        "error": res.error,
        "expect": {"pass": (not fails) and not res.error, "fails": fails},
        "trace_summary": res.trace.summary() if res.trace else {},
        "steps": res.steps(),
        "ctx": res.ctx,
    }
    text = (json.dumps(payload, ensure_ascii=False, indent=2) if a.json
            else _dump_markdown(payload))
    if a.out:
        path = Path(a.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        print(f"已写出 {path}")
    else:
        print(text)
    return 0 if not fails and not res.error else 1


def _dump_markdown(p: dict) -> str:
    lines = [
        f"# {p['pack']} · {p['scenario']} · {p['workflow']}",
        "",
        f"- 结局:`{p['final']}`",
        f"- 仿真耗时:`{p['sim_ms']} ms`",
        f"- 步数:`{len(p['steps'])}`",
        f"- 断言:{'全部通过' if p['expect']['pass'] else '; '.join(p['expect']['fails'])}",
    ]
    if p["error"]:
        lines.append(f"- 错误:`{p['error']}`")
    lines += ["", "| # | 节点 | 类型 | 角色 | 状态 | 耗时 | 摘要 |",
              "|---|---|---|---|---|---|---|"]
    for s in p["steps"]:
        summary = str(s.get("summary", "")).replace("|", "\\|")
        lines.append(f"| {s['seq']} | `{s['node']}` | {s['type']} | {s['actor'] or '—'} "
                     f"| {s['status']} | {s['ms']}ms | {summary} |")
    return "\n".join(lines)


def cmd_learn(a):
    from .lessons import add_lesson, load_lessons

    p = _load(a.pack)
    item = add_lesson(p, a.title, a.text, source=a.source)
    fresh = _load(a.pack)        # 缓存已失效,重新加载
    print(f"已写入 {item['id']}: {item['title']}")
    print(f"经验库现有 {len(load_lessons(fresh))} 条")

    from .engine import Engine
    hub = fresh.hub or Engine(fresh, shared={}).hub
    hits = hub.search(f"{item['title']} {item['text']}", k=3)
    print("检索验证:")
    for h in hits:
        print(f"  [{h['kind']}] {h['title']} ({h['score']})")
    return 0


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

    p = sub.add_parser("audit")
    p.add_argument("pack")
    p.add_argument("--static-only", action="store_true", help="只做静态检查，不跑场景")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("dump", help="跑一个场景并把轨迹导出成 Markdown / JSON")
    p.add_argument("pack")
    p.add_argument("scenario")
    p.add_argument("--workflow")
    p.add_argument("--json", action="store_true", help="输出 JSON 而不是 Markdown")
    p.add_argument("--out", help="写到文件而不是打到终端")
    p.set_defaults(fn=cmd_dump)

    p = sub.add_parser("learn", help="把一条复盘结论写回 pack 的经验库")
    p.add_argument("pack")
    p.add_argument("--title", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--source", default="manual")
    p.set_defaults(fn=cmd_learn)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
