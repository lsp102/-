"""runner.py — 场景执行器（唯一入口）

以前同一件事写了三遍：
    cli._run        —— 处理 repeat + cancel_after_ms 定时器
    server._execute —— 什么都处理，于是控制台里 s4 不会撤销、s8 不会去重
    audit           —— 直接 engine.run，同上

现在收敛到这里，三处都调它。谁想跑一个场景，语义就一样。
"""
from __future__ import annotations

import os
import threading

from .engine import Engine, RunResult
from .pack import Pack


def run_scenario(pack: Pack, scenario: dict, workflow: str | None = None,
                 pace_ms: int = 0, shared: dict | None = None,
                 on_step=None, state_dir: str | None = None,
                 overrides: dict | None = None,
                 mode: str | None = None) -> tuple[RunResult, Engine]:
    wf = (workflow or pack.meta.get("default_workflow")
          or next(iter(pack.workflows), None))
    if wf is None:
        raise ValueError("pack 里没有任何工作流")

    params = {"pace_ms": pace_ms}
    if overrides:
        params.update(overrides)
    eng = Engine(pack, params=params,
                 shared=shared if shared is not None else {}, on_step=on_step,
                 state_dir=state_dir,
                 mode=mode or os.environ.get("HARNESS_MODE", "mock"))

    res: RunResult | None = None
    for _ in range(max(int(scenario.get("repeat", 1) or 1), 1)):
        # 场景可以用 cancel_after_ms 模拟“老人在第 N 毫秒按键撤销”
        delay = (scenario.get("params") or {}).get("cancel_after_ms")
        timer = None
        if delay:
            timer = threading.Timer(float(delay) / 1000.0, eng.cancel)
            timer.daemon = True
            timer.start()
        try:
            res = eng.run(scenario, workflow=wf)
        finally:
            if timer:
                timer.cancel()
    assert res is not None
    return res, eng
