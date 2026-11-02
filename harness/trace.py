"""trace.py — 执行轨迹

平台的核心可观测性契约：每个节点的执行都留一条记录，
字段固定，UI / 断言 / 复盘都读它，不读业务日志。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, asdict

STATUS = ("ok", "error", "skipped", "veto", "cancel")


@dataclass
class Step:
    seq: int
    node: str
    type: str
    actor: str
    status: str
    summary: str
    ms: int
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class Trace:
    def __init__(self):
        self._lock = threading.Lock()
        self.steps: list[Step] = []

    def add(self, node: str, type_: str, actor: str, status: str,
            summary: str, ms: int = 0) -> Step:
        with self._lock:
            st = Step(seq=len(self.steps) + 1, node=node, type=type_,
                      actor=actor, status=status, summary=summary, ms=ms)
            self.steps.append(st)
            return st

    # ---- 查询 ----
    def count(self, status: str | None = None, node: str | None = None,
              actor: str | None = None) -> int:
        return sum(1 for s in self.steps
                   if (status is None or s.status == status)
                   and (node is None or s.node == node)
                   and (actor is None or s.actor == actor))

    def last(self, node: str | None = None) -> Step | None:
        for s in reversed(self.steps):
            if node is None or s.node == node:
                return s
        return None

    def summary(self) -> dict:
        return {
            "steps": len(self.steps),
            "by_status": {k: self.count(status=k) for k in STATUS},
            "actors": sorted({s.actor for s in self.steps if s.actor}),
            "total_ms": sum(s.ms for s in self.steps),
        }

    def to_list(self) -> list[dict]:
        with self._lock:
            return [s.to_dict() for s in self.steps]
