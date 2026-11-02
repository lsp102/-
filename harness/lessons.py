"""lessons.py — 经验回写（复盘闭环的最后一步）

链路：跑场景 → 得出可复用的结论 → 写进 pack 的 data/cases.json
      → CaseKnowledge 注册进 KnowledgeHub → 下次分诊的检索能命中它。

写回一定会让 pack 缓存失效，所以下一次 `validate` / 控制台运行就能看到新经验，
不需要重启服务。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .pack import Pack, clear_cache

MAX_LESSONS = 80


def cases_path(pack: Pack) -> Path:
    return Path(pack.dir) / "data" / "cases.json"


def load_lessons(pack: Pack) -> list[dict]:
    p = cases_path(pack)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("lessons", [])
    except (OSError, ValueError):
        return []


def add_lesson(pack: Pack, title: str, text: str, source: str = "manual") -> dict:
    """追加一条经验并返回它。列表超过 MAX_LESSONS 时丢最旧的。"""
    p = cases_path(pack)
    data: dict = {"lessons": []}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            data = {"lessons": []}
    lessons = data.setdefault("lessons", [])
    item = {
        "id": f"L{len(lessons) + 1}",
        "title": title.strip(),
        "text": text.strip(),
        "ts": time.time(),
        "source": source,
    }
    lessons.append(item)
    data["lessons"] = lessons[-MAX_LESSONS:]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    clear_cache()
    return item
