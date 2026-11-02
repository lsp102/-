"""selfloop.py — 自我循环（Self-Loop）与反馈优化

闭环四步，每一步都有产物，不是流程图：

  1. 复盘   —— 跑参数扫描，收集"通过率 / 平均响应时延"
  2. 建议   —— 在通过率最高的候选里挑时延最低的；同时给出边界证据
  3. 采纳   —— 写回 data/learning.json（参数 + 经验条目）
  4. 再验证 —— 用新参数重跑，对比前后

关键点：这不是"越短越好"的单向优化。振铃时间太短会把"家属正在接听途中"
误判成无人接听，通过率会掉。甜点由验证集决定，不由直觉决定。

用法：
    python app/selfloop.py            # 只扫描，不写文件
    python app/selfloop.py --apply    # 扫描 + 采纳 + 再验证
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.core import DATA, apply_learned, config, knowledge_hub  # noqa: E402
from experiment import run_suite                                  # noqa: E402

CANDIDATES = (30.0, 20.0, 15.0, 10.0, 8.0, 5.0)


def sweep(candidates=CANDIDATES) -> list[dict]:
    rows = []
    for ring in candidates:
        config.ring_timeout_s = float(ring)
        rs = run_suite("multi")
        passed = sum(1 for r in rs if r["pass"])
        avg_s = round(sum(r["sim_ms"] for r in rs) / len(rs) / 1000, 1)
        failed = [r["scenario"] for r in rs if not r["pass"]]
        rows.append({"ring": float(ring), "passed": passed, "total": len(rs),
                     "avg_sim_s": avg_s, "failed": failed})
    return rows


def review(rows: list[dict], baseline: float) -> dict:
    best_passed = max(r["passed"] for r in rows)
    pool = [r for r in rows if r["passed"] == best_passed]
    best = min(pool, key=lambda r: r["avg_sim_s"])
    worst = max(pool, key=lambda r: r["avg_sim_s"])
    worse = [r for r in rows if r["passed"] < best_passed]
    boundary = max(worse, key=lambda r: r["ring"]) if worse else None

    reason = (f"扫描 {len(rows)} 组振铃参数，{best['ring']:.0f}s 在保持 "
              f"{best['passed']}/{best['total']} 通过率的前提下，把 8 个场景的平均响应"
              f"从 {worst['avg_sim_s']}s 降到 {best['avg_sim_s']}s。")
    if boundary:
        reason += (f" 再降到 {boundary['ring']:.0f}s 会让 {','.join(boundary['failed'])} "
                   "失败——家属实际接听耗时被误判为无人接听，说明这是不能越过的下界。")

    return {
        "id": "S-PARAM-RING",
        "title": f"振铃等待 {baseline:.0f}s → {best['ring']:.0f}s",
        "target": "ring_timeout_s",
        "from": baseline,
        "to": best["ring"],
        "reason": reason,
        "evidence": rows,
        "boundary": boundary,
        "before_avg_s": worst["avg_sim_s"],
        "after_avg_s": best["avg_sim_s"],
    }


def apply(sug: dict) -> dict:
    p = DATA / "learning.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("params", {})[sug["target"]] = sug["to"]
    lessons = d.setdefault("lessons", [])
    lessons.append({
        "id": f"L{len(lessons) + 1}",
        "title": sug["title"],
        "text": sug["reason"],
        "ts": time.time(),
    })
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def validate() -> dict:
    """采纳后重跑，验证行为确实改变。"""
    config.ring_timeout_s = float(
        json.loads((DATA / "learning.json").read_text(encoding="utf-8"))
        .get("params", {}).get("ring_timeout_s", config.ring_timeout_s)
    )
    rs = run_suite("multi")
    return {
        "ring": config.ring_timeout_s,
        "passed": sum(1 for r in rs if r["pass"]),
        "total": len(rs),
        "avg_sim_s": round(sum(r["sim_ms"] for r in rs) / len(rs) / 1000, 1),
    }


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


def main(argv: list[str]) -> int:
    _utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="采纳建议并写回 learning.json")
    args = ap.parse_args(argv)

    print("=" * 78)
    print("Self-Loop 步骤 1/4 · 复盘：振铃等待参数扫描")
    print("=" * 78)
    baseline = float(config.ring_timeout_s)
    rows = sweep()
    print(f"{'振铃(s)':>8} {'通过':>8} {'平均响应(s)':>12}  失败场景")
    for r in rows:
        mark = "✓" if r["passed"] == r["total"] else "✗"
        print(f"{r['ring']:>8.0f} {r['passed']}/{r['total']} {mark:>2} "
              f"{r['avg_sim_s']:>12.1f}  {','.join(r['failed']) or '—'}")

    print("\n" + "=" * 78)
    print("Self-Loop 步骤 2/4 · 建议")
    print("=" * 78)
    sug = review(rows, baseline)
    print(f"  {sug['title']}")
    print(f"  {sug['reason']}")
    if sug["boundary"]:
        b = sug["boundary"]
        print(f"  边界证据：{b['ring']:.0f}s 时失败 {len(b['failed'])} 个场景 → {b['failed']}")

    if not args.apply:
        print("\n（未采纳。加 --apply 执行步骤 3/4）")
        return 0

    print("\n" + "=" * 78)
    print("Self-Loop 步骤 3/4 · 采纳：写回 learning.json + 经验库")
    print("=" * 78)
    d = apply(sug)
    print(f"  params = {d.get('params')}")
    print(f"  lessons = {len(d.get('lessons', []))} 条（下次分诊可从经验库检索到）")

    print("\n" + "=" * 78)
    print("Self-Loop 步骤 4/4 · 再验证：用采纳后的参数重跑")
    print("=" * 78)
    v = validate()
    print(f"  振铃 {v['ring']:.0f}s → 通过 {v['passed']}/{v['total']}，"
          f"平均响应 {v['avg_sim_s']}s")

    knowledge_hub.reload()
    apply_learned()
    print("\n闭环完成：参数已生效，经验已入库，下一轮从新基线开始。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
