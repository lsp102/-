"""pack.py — 场景包（scenario pack）加载

一个 pack 是自包含的目录：
    pack.json          元信息 + 默认参数 + workflow 映射
    agents.json        Agent 团队定义（角色 / 权限 / 工具 / 驱动方式）
    workflows/*.json   工作流定义（节点 + 转移）
    data/*.json        领域数据（档案 / 规则 …），任意键值
    kb/*.md            文档知识库
    scenarios.json     验证场景（含期望结果）
    tools.py           TOOLS = {name: fn}      工具实现
    handlers.py        HANDLERS = {name: fn}   Agent 处理器（代码驱动）

平台不认识业务；业务全部装在 pack 里。
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType


@dataclass
class Pack:
    id: str
    name: str
    dir: Path
    meta: dict
    workflows: dict[str, dict] = field(default_factory=dict)
    agents: dict = field(default_factory=dict)
    tools: dict = field(default_factory=dict)
    handlers: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)
    scenarios: list[dict] = field(default_factory=list)
    kb_dir: Path | None = None

    def params(self) -> dict:
        return dict(self.meta.get("params") or {})

    def scenario(self, sid: str) -> dict | None:
        for s in self.scenarios:
            if str(s.get("id")) == str(sid):
                return s
        return None

    def info(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "workflows": list(self.workflows),
            "agents": list(self.agents),
            "tools": list(self.tools),
            "scenarios": len(self.scenarios),
            "data": list(self.data),
        }


def _load_module(path: Path) -> ModuleType | None:
    if not path.exists():
        return None
    key = hashlib.md5(str(path).encode()).hexdigest()[:8]
    name = f"_pack_{path.parent.name}_{path.stem}_{key}"
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_pack(path: str | Path) -> Pack:
    d = Path(path).resolve()
    if not (d / "pack.json").exists():
        raise FileNotFoundError(f"不是有效的 pack 目录（缺少 pack.json）: {d}")

    meta = _read_json(d / "pack.json")

    workflows: dict[str, dict] = {}
    for name, rel in (meta.get("workflows") or {}).items():
        workflows[name] = _read_json(d / rel)

    agents = _read_json(d / "agents.json").get("agents", {}) if (d / "agents.json").exists() else {}

    data: dict = {}
    ddir = d / meta.get("data_dir", "data")
    if ddir.exists():
        for p in sorted(ddir.glob("*.json")):
            data[p.stem] = _read_json(p)

    scenarios: list[dict] = []
    if (d / "scenarios.json").exists():
        payload = _read_json(d / "scenarios.json")
        scenarios = payload if isinstance(payload, list) else payload.get("scenarios", [])
    else:
        sdir = d / "scenarios"
        if sdir.exists():
            for p in sorted(sdir.glob("*.json")):
                scenarios.append(_read_json(p))

    tools_mod = _load_module(d / "tools.py")
    handlers_mod = _load_module(d / "handlers.py")

    kb_dir = d / "kb" if (d / "kb").exists() else None

    return Pack(
        id=meta.get("id", d.name),
        name=meta.get("name", d.name),
        dir=d,
        meta=meta,
        workflows=workflows,
        agents=agents,
        tools=(tools_mod.TOOLS if tools_mod and hasattr(tools_mod, "TOOLS") else {}),
        handlers=(handlers_mod.HANDLERS if handlers_mod and hasattr(handlers_mod, "HANDLERS") else {}),
        data=data,
        scenarios=scenarios,
        kb_dir=kb_dir,
    )


def discover(root: str | Path) -> list[Path]:
    r = Path(root)
    if not r.exists():
        return []
    return sorted(p.parent for p in r.glob("*/pack.json"))
