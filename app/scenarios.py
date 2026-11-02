"""scenarios.py — 场景库加载"""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
REPLAY_DIR = BASE / "replay"


def load_all() -> list[dict]:
    items = []
    for p in REPLAY_DIR.glob("s*.json"):
        try:
            items.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    items.sort(key=lambda x: int(str(x.get("id", "s0")).lstrip("s") or 0))
    return items


def by_id(sid: str) -> dict | None:
    for s in load_all():
        if s["id"] == sid:
            return s
    return None
