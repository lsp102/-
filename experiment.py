"""experiment.py — 8 场景 × 2 系统 对照实验

用法：
    python experiment.py            # 打印表格 + 写 results.csv
    python experiment.py --json     # 输出 JSON（供服务端调用）

⚠ 数据诚信提示：
mock 模式下 SingleAgentBaseline 是**行为模拟**，不是真实 LLM 调用。
正式提交前必须在 D12 用真实 API 重跑基线（设 DEEPSEEK_API_KEY + config.use_llm=True），
否则这张表不能写进任何材料。
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))


def _utf8_console():
    """Windows 控制台默认 GBK，强制 UTF-8 以免中文和 ✓ 报错。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass

from app import scenarios as sc_mod          # noqa: E402
from app.core import LEVEL_CN, config, store  # noqa: E402
from app.supervisor import supervisor         # noqa: E402


def _fast_config():
    config.cancel_window_s = 0.0
    config.call_timeout_s = 0.02
    config.push_retry_gap_s = 0.01
    config.dedupe_window_s = 10.0


def _vetoed(rec) -> bool:
    return any(s.level == "veto" for s in rec.steps)


def _view(rec, merge_rec) -> dict:
    return {
        "level": rec.level,
        "forbid_move": rec.forbid_move,
        "guard": _vetoed(rec),
        "escalated": rec.escalated,
        "outcome": rec.outcome,
        "notified": rec.notified,
        "merged": (merge_rec is not None and merge_rec.merged_into != ""),
        "degraded": rec.degraded or (merge_rec.degraded if merge_rec else False),
    }


def run_suite(mode: str) -> list[dict]:
    _fast_config()
    rows = []
    for sc in sc_mod.load_all():
        store.reset()
        run = dict(sc)
        run["call_timeout_s"] = 0.02
        run.setdefault("cancel_window_s", 0.0)

        rec = supervisor.submit(run, mode=mode, sync=True)
        merge_rec = None
        if sc.get("repeat", 1) > 1:
            merge_rec = supervisor.submit({**run, "id": sc["id"] + "-dup"},
                                          mode=mode, sync=True)

        view = _view(rec, merge_rec)
        exp = sc.get("expect", {})
        failed = [k for k, v in exp.items() if k in view and view[k] != v]
        rows.append({
            "scenario": sc["id"],
            "title": sc.get("title", ""),
            "mode": mode,
            **view,
            "total_ms": rec.total_ms + (merge_rec.total_ms if merge_rec else 0),
            "sim_ms": rec.sim_ms + (merge_rec.sim_ms if merge_rec else 0),
            "pass": not failed,
            "failed": failed,
        })
    store.reset()
    return rows


def to_markdown(rows: list[dict]) -> str:
    lines = []
    lines.append("| 场景 | 说明 | 等级 | 禁忌 | 护栏生效 | 升级次数 | 结局 | 去重 | 响应(s) | 结论 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            f"| {r['scenario']} | {r['title']} | {LEVEL_CN.get(r['level'], r['level'])} "
            f"| {'✓' if r['forbid_move'] else '—'} | {'✓' if r['guard'] else '—'} "
            f"| {r['escalated']} | {r['outcome']} | {'✓' if r['merged'] else '—'} "
            f"| {r['sim_ms'] / 1000:.1f} | {'通过' if r['pass'] else '未通过: ' + ','.join(r['failed'])} |"
        )
    return "\n".join(lines)


def summarize(rows: list[dict]) -> dict:
    out = {}
    for mode in ("multi", "single"):
        rs = [r for r in rows if r["mode"] == mode]
        if not rs:
            continue
        out[mode] = {
            "passed": sum(1 for r in rs if r["pass"]),
            "total": len(rs),
            "avg_ms": int(sum(r["total_ms"] for r in rs) / len(rs)),
            "avg_sim_s": round(sum(r["sim_ms"] for r in rs) / len(rs) / 1000, 1),
            "scenarios_passed": [r["scenario"] for r in rs if r["pass"]],
            "scenarios_failed": [r["scenario"] for r in rs if not r["pass"]],
        }
    return out


def main(argv: list[str]) -> int:
    _utf8_console()
    rows = []
    for mode in ("multi", "single"):
        rows += run_suite(mode)

    summary = summarize(rows)
    if "--json" in argv:
        print(json.dumps({"rows": rows, "summary": summary}, ensure_ascii=False))
        return 0

    print("=" * 96)
    print("对照实验：多智能体编排 vs 单 Agent 基线（8 场景）")
    print("=" * 96)
    for mode, label in (("multi", "多智能体编排"), ("single", "单 Agent 基线")):
        rs = [r for r in rows if r["mode"] == mode]
        print(f"\n### {label}\n")
        print(to_markdown(rs))
    print("\n" + "=" * 96)
    for k, v in summary.items():
        print(f"{k:8s} 通过 {v['passed']}/{v['total']}  平均响应 {v['avg_sim_s']}s  "
              f"通过场景={v['scenarios_passed']}  失败场景={v['scenarios_failed']}")
    print("=" * 96)
    print("⚠ mock 基线为行为模拟，正式材料需用真实 LLM 重跑。")

    csv_path = BASE / "results.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow({**r, "failed": ";".join(r["failed"])})
    print(f"\n已写出 {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
