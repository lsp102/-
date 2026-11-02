"""selfloop.py — 自我循环：扫描 → 建议 → 采纳 → 回写经验 → 再验证

和 lessons.py 的分工：

    lessons  = 「人发现问题后，怎么把结论写回去」
    selfloop = 「系统自己发现问题、自己给出建议、自己验证」

pack 在 pack.json 里声明可调项，平台不认识业务：

```json
"tunable": {
  "ring_timeout_s": {
    "label": "外呼振铃等待",
    "candidates": [30, 20, 15, 13, 10, 8, 5],
    "goal": "min",
    "metric": "sim_ms"
  }
}
```

采纳结果落到 `packs/<id>/data/tuned.json`，由 `Pack.params()` 覆盖默认值——
所以是「改数据」，不是「改代码」，下一次运行立刻生效。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .engine import check_expect
from .lessons import add_lesson
from .pack import Pack, clear_cache
from .runner import run_scenario

DEFAULT_CANDIDATES = (30, 20, 15, 10, 8, 5)


def _num(v) -> str:
    f = float(v)
    return f"{f:g}"


def run_all(pack: Pack, overrides: dict | None = None) -> dict:
    """用同一组参数跑完全部场景，返回通过率与平均仿真耗时。"""
    passed = 0
    sims: list[int] = []
    failed: list[str] = []
    for sc in pack.scenarios:
        res, _ = run_scenario(pack, sc, overrides=overrides)
        fails = check_expect(res, sc.get("expect") or {})
        if fails or res.error:
            failed.append(str(sc.get("id")))
        else:
            passed += 1
        sims.append(res.sim_ms)
    return {
        "passed": passed,
        "total": len(pack.scenarios),
        "avg_ms": int(sum(sims) / len(sims)) if sims else 0,
        "failed": failed,
    }


def sweep(pack: Pack, target: str, candidates=None) -> list[dict]:
    spec = pack.tunable.get(target) or {}
    raw = candidates if candidates is not None else (spec.get("candidates") or DEFAULT_CANDIDATES)
    current = pack.params().get(target)

    # 当前值必须进候选表，否则看不出「现在是好是坏」
    vals = [float(x) for x in raw]
    if current is not None and float(current) not in vals:
        vals.append(float(current))
    vals.sort()

    rows = []
    for c in vals:
        st = run_all(pack, {target: c})
        rows.append({"value": c, "current": current is not None and float(c) == float(current), **st})
    return rows


def review(rows: list[dict], spec: dict) -> dict:
    """在通过率最高的候选里挑指标最优的；再找出越界会坏在哪。"""
    goal = str(spec.get("goal", "min")).lower()
    best_pass = max(r["passed"] for r in rows)
    pool = [r for r in rows if r["passed"] == best_pass]
    # 指标并列时优先保留当前值：不为同样的效果去改配置，避免来回抖
    if goal == "min":
        pool.sort(key=lambda r: (r["avg_ms"], 0 if r.get("current") else 1, float(r["value"])))
    else:
        pool.sort(key=lambda r: (-r["avg_ms"], 0 if r.get("current") else 1, -float(r["value"])))
    best = pool[0]
    worst = max(pool, key=lambda r: r["avg_ms"])
    worse = [r for r in rows if r["passed"] < best_pass]
    boundary = None
    if worse:
        boundary = min(worse, key=lambda r: abs(float(r["value"]) - float(best["value"])))

    label = str(spec.get("label") or "参数")
    cur_row = next((r for r in rows if r.get("current")), None)
    cur_val = cur_row["value"] if cur_row else None
    direction = "小" if goal == "min" else "大"

    if cur_val is not None and float(cur_val) == float(best["value"]):
        text = (f"扫描 {len(rows)} 组候选，当前取值 {_num(best['value'])} 已是最优："
                f"通过率 {best['passed']}/{best['total']}，"
                f"平均仿真耗时 {best['avg_ms'] / 1000:.1f}s。")
    elif cur_val is not None:
        delta = (cur_row["avg_ms"] - best["avg_ms"]) / 1000
        text = (f"当前取值 {_num(cur_val)} 换成 {_num(best['value'])}，通过率 "
                f"{cur_row['passed']}/{cur_row['total']} → {best['passed']}/{best['total']}，"
                f"平均仿真耗时 {cur_row['avg_ms'] / 1000:.1f}s → {best['avg_ms'] / 1000:.1f}s")
        if delta > 0:
            text += f"（节省 {delta:.1f}s）"
        text += "。"
    else:
        text = (f"{label}取 {_num(best['value'])} 时通过率最高，"
                f"为 {best['passed']}/{best['total']}，"
                f"平均仿真耗时 {best['avg_ms'] / 1000:.1f}s。")

    if boundary:
        text += (f" 再往{direction}走到 {_num(boundary['value'])} 会让 "
                 f"{','.join(boundary['failed'])} 失败，说明这是不能越过的边界。")

    return {
        "target": None,
        "label": label,
        "from": cur_val,
        "to": best["value"],
        "before_ms": (cur_row or worst)["avg_ms"],
        "after_ms": best["avg_ms"],
        "passed": best["passed"],
        "total": best["total"],
        "boundary": boundary,
        "text": text,
        "rows": rows,
    }


def adopt(pack: Pack, target: str, sug: dict) -> dict:
    """把建议写进 data/tuned.json，并追加一条经验。"""
    p = Path(pack.dir) / "data" / "tuned.json"
    data: dict = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    data[target] = sug["to"]
    data["_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    clear_cache()
    info = {"file": str(p), "params": {k: v for k, v in data.items() if not k.startswith("_")}}
    return info


def record_lesson(pack: Pack, target: str, sug: dict) -> dict:
    return add_lesson(pack,
                      f"{sug['label']} 取 {_num(sug['to'])} 是当前场景集的边界",
                      sug["text"],
                      source=f"self-loop:{target}")
