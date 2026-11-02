"""knowledge.py — 知识库注册中心（平台组件）

三种知识源实现同一接口，可独立注册、替换、扩展：
  RuleKnowledge  结构化规则库
  DocKnowledge   文档知识库（按 ## 切片，字符二元组检索）
  CaseKnowledge  经验库（复盘结论回写，供下一轮检索）

业务层只依赖 KnowledgeHub.search()，不关心底层是规则、文档还是将来的向量库。
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from pathlib import Path


def _grams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s or "")
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _score2(qg: set[str], tg: set[str]) -> float:
    """已经算好二元组时的打分（知识源在加载期就把正文的 grams 缓存下来）。"""
    if not qg or not tg:
        return 0.0
    return len(qg & tg) / (len(qg) ** 0.5 * len(tg) ** 0.5 + 1e-6)


def _score(query: str, text: str) -> float:
    return _score2(_grams(query), _grams(text))


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
    kind = "rule"

    def __init__(self, rules: list[dict]):
        super().__init__("规则库")
        self.rules = rules
        # 正文的二元组在加载期算一次（以前每次检索都重算）
        self._idx: list[tuple[dict, str, set[str]]] = []
        for r in rules:
            then = r.get("then", {})
            text = (r.get("desc", "") + " " + " ".join(then.get("plan_add", []))
                    + " " + str(then.get("rationale", "")))
            self._idx.append((r, text, _grams(text)))

    def search(self, query: str, k: int = 3) -> list[dict]:
        qg = _grams(query)
        out = []
        for r, text, tg in self._idx:
            s = _score2(qg, tg)
            if s > 0.01:
                out.append(self._hit(r["id"], r.get("desc", r["id"]), text, s))
        out.sort(key=lambda x: -x["score"])
        return out[:k]

    def stats(self) -> dict:
        return {"kind": self.kind, "name": self.name, "entries": len(self.rules)}


class DocKnowledge(KnowledgeSource):
    kind = "doc"

    def __init__(self, directory: Path | None = None, name: str = "文档知识库"):
        super().__init__(name)
        self.dir = Path(directory) if directory else None
        self.chunks: list[dict] = []
        self._idx: list[tuple[dict, set[str]]] = []
        self.reload()

    def reload(self):
        self.chunks = []
        self._idx = []
        if not self.dir or not self.dir.exists():
            return
        for p in sorted(self.dir.glob("*.md")):
            text = p.read_text(encoding="utf-8")
            parts = re.split(r"\n##\s+", text)
            for i, sec in enumerate(parts[1:], 1):
                lines = sec.strip().split("\n")
                title = lines[0].strip()
                body = "\n".join(lines[1:]).strip()
                if body:
                    chunk = {"id": f"{p.stem}#{i}", "title": title,
                             "text": body, "doc": p.stem}
                    self.chunks.append(chunk)
                    self._idx.append((chunk, _grams(title + " " + body)))

    def search(self, query: str, k: int = 3) -> list[dict]:
        qg = _grams(query)
        out = []
        for c, tg in self._idx:
            s = _score2(qg, tg)
            if s > 0.01:
                out.append(self._hit(c["id"], c["title"], c["text"], s, {"doc": c["doc"]}))
        out.sort(key=lambda x: -x["score"])
        return out[:k]

    def stats(self) -> dict:
        return {"kind": self.kind, "name": self.name, "entries": len(self.chunks)}


class CaseKnowledge(KnowledgeSource):
    kind = "case"

    def __init__(self, path: Path | None = None, items: list[dict] | None = None):
        super().__init__("经验库")
        self.path = Path(path) if path else None
        self.items: list[dict] = list(items or [])
        self._idx: list[tuple[dict, set[str]]] = []
        if self.path is not None:
            self.reload()
        else:
            self._rebuild()

    def _rebuild(self):
        self._idx = [(it, _grams(str(it.get("title", "")) + " " + str(it.get("text", ""))))
                     for it in self.items]

    def reload(self):
        if not self.path or not self.path.exists():
            self._rebuild()
            return
        try:
            self.items = json.loads(self.path.read_text(encoding="utf-8")).get("lessons", [])
        except Exception:
            self.items = []
        self._rebuild()

    def search(self, query: str, k: int = 3) -> list[dict]:
        qg = _grams(query)
        out = []
        for it, tg in self._idx:
            s = _score2(qg, tg)
            if s > 0.01:
                out.append(self._hit(it.get("id", ""), it.get("title", ""),
                                     it.get("text", ""), s))
        out.sort(key=lambda x: -x["score"])
        return out[:k]

    def stats(self) -> dict:
        return {"kind": self.kind, "name": self.name, "entries": len(self.items)}


class KnowledgeHub:
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
            fn = getattr(s, "reload", None)
            if callable(fn):
                fn()

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
