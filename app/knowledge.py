"""knowledge.py — 知识库定义与集成（平台组件）

三种知识源实现同一接口，可独立注册、替换、扩展：
  RuleKnowledge  结构化规则库 —— 决策用，规则引擎读取
  DocKnowledge   文档知识库   —— 解释用，轻量字面检索（接口可换 embedding 后端）
  CaseKnowledge  案例/经验库  —— Self-Loop 采纳的建议回写进来，形成闭环

平台化要点：上层（分诊 Agent）只依赖 KnowledgeHub.search()，不关心底层是规则、
文档还是向量库。换知识源不需要改任何业务代码。
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"


def _grams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s or "")
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _score(query: str, text: str) -> float:
    """字符二元组余弦近似。零依赖、中文可用，够判断"哪条更相关"。"""
    qg, tg = _grams(query), _grams(text)
    if not qg or not tg:
        return 0.0
    return len(qg & tg) / (len(qg) ** 0.5 * len(tg) ** 0.5 + 1e-6)


class KnowledgeSource(ABC):
    kind = "base"

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def search(self, query: str, k: int = 3) -> list[dict]:
        ...

    @abstractmethod
    def stats(self) -> dict:
        ...

    def _hit(self, id_: str, title: str, text: str, score: float,
             extra: dict | None = None) -> dict:
        d = {
            "id": id_,
            "title": title,
            "snippet": re.sub(r"\s+", " ", text or "")[:140],
            "score": round(score, 3),
            "source": self.name,
            "kind": self.kind,
        }
        if extra:
            d.update(extra)
        return d


class RuleKnowledge(KnowledgeSource):
    """结构化规则库：每条规则本身也是一个可检索的知识条目。"""

    kind = "rule"

    def __init__(self, rules: list[dict]):
        super().__init__("规则库")
        self.rules = rules

    def search(self, query: str, k: int = 3) -> list[dict]:
        out = []
        for r in self.rules:
            t = r.get("desc", "")
            then = r.get("then", {})
            t += " " + " ".join(then.get("plan_add", [])) + " " + then.get("rationale", "")
            s = _score(query, t)
            if s > 0.01:
                out.append(self._hit(r["id"], r.get("desc", r["id"]), t, s))
        out.sort(key=lambda x: -x["score"])
        return out[:k]

    def stats(self) -> dict:
        return {"kind": self.kind, "name": self.name, "entries": len(self.rules)}


class DocKnowledge(KnowledgeSource):
    """文档知识库：按 ## 标题切片，字面相似度检索。

    刻意不引入向量库：18 天里多一个依赖就多一个演示时炸掉的点。
    接口不变，之后换 embedding 只改这个类。
    """

    kind = "doc"

    def __init__(self, directory: Path | None = None):
        super().__init__("处置知识库")
        self.dir = directory or (DATA / "kb")
        self.chunks: list[dict] = []
        self.reload()

    def reload(self):
        self.chunks = []
        if not self.dir.exists():
            return
        for p in sorted(self.dir.glob("*.md")):
            text = p.read_text(encoding="utf-8")
            parts = re.split(r"\n##\s+", text)
            for i, sec in enumerate(parts[1:], 1):
                lines = sec.strip().split("\n")
                title = lines[0].strip()
                body = "\n".join(lines[1:]).strip()
                if body:
                    self.chunks.append({"id": f"{p.stem}#{i}", "title": title,
                                        "text": body, "doc": p.stem})

    def search(self, query: str, k: int = 3) -> list[dict]:
        out = []
        for c in self.chunks:
            s = _score(query, c["title"] + " " + c["text"])
            if s > 0.01:
                out.append(self._hit(c["id"], c["title"], c["text"], s, {"doc": c["doc"]}))
        out.sort(key=lambda x: -x["score"])
        return out[:k]

    def stats(self) -> dict:
        return {"kind": self.kind, "name": self.name, "entries": len(self.chunks)}


class CaseKnowledge(KnowledgeSource):
    """经验库：Self-Loop 采纳的结论写入 learning.json，下次分诊可检索到。

    这一条让"复盘 → 建议 → 采纳 → 回写 → 下次生效"成为真实闭环，
    而不是一张流程图。
    """

    kind = "case"

    def __init__(self, path: Path | None = None):
        super().__init__("经验库")
        self.path = path or (DATA / "learning.json")
        self.items: list[dict] = []
        self.reload()

    def reload(self):
        self.items = []
        if not self.path.exists():
            return
        try:
            self.items = json.loads(self.path.read_text(encoding="utf-8")).get("lessons", [])
        except Exception:
            self.items = []

    def search(self, query: str, k: int = 3) -> list[dict]:
        out = []
        for it in self.items:
            text = it.get("title", "") + " " + it.get("text", "")
            s = _score(query, text)
            if s > 0.01:
                out.append(self._hit(it.get("id", ""), it.get("title", ""),
                                     it.get("text", ""), s))
        out.sort(key=lambda x: -x["score"])
        return out[:k]

    def stats(self) -> dict:
        return {"kind": self.kind, "name": self.name, "entries": len(self.items)}


class KnowledgeHub:
    """知识库注册中心。业务层只见 Hub，不见具体实现。"""

    def __init__(self):
        self.sources: list[KnowledgeSource] = []

    def register(self, src: KnowledgeSource) -> KnowledgeSource:
        self.sources.append(src)
        return src

    def get(self, kind: str) -> KnowledgeSource | None:
        for s in self.sources:
            if s.kind == kind:
                return s
        return None

    def reload(self):
        for s in self.sources:
            reload_fn = getattr(s, "reload", None)
            if callable(reload_fn):
                reload_fn()

    def search(self, query: str, k: int = 4) -> list[dict]:
        hits: list[dict] = []
        for s in self.sources:
            try:
                hits.extend(s.search(query, k=k))
            except Exception:
                continue
        hits.sort(key=lambda x: -x["score"])
        return hits[:k]

    def stats(self) -> list[dict]:
        return [s.stats() for s in self.sources]
