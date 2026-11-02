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

加载按目录 mtime 签名缓存：控制台每次 /api/packs 都会摸一遍全部 pack，
以前是每次都重读磁盘 + 重新 import 两个模块（handlers/tools），
改完 pack 又得重启服务才生效。现在两者都解决了。
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

_CACHE: dict[str, tuple[tuple, "Pack"]] = {}
_CACHE_LOCK = threading.RLock()
# 目录指纹本身要 stat 一圈，比解析这几个小 JSON 还贵；给它加 0.75s TTL，
# 这样控制台轮询/连续跑场景不会每次都摸盘，改完 pack 最迟 0.75s 生效。
_SIG: dict[str, tuple[float, tuple]] = {}
_SIG_TTL = 0.75


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
    # 知识库注册中心缓存：同一次运行里 Engine 是被反复创建的（每个场景一个），
    # 但 kb 目录没变就没必要每次重新切片 + 重新匹配整表规则。
    hub: object | None = field(default=None, repr=False, compare=False)

    def params(self) -> dict:
        # data/tuned.json 是 Self-Loop 采纳后的覆盖值，优先于 pack.json 的默认值
        base = dict(self.meta.get("params") or {})
        tuned = self.data.get("tuned")
        if isinstance(tuned, dict):
            base.update({k: v for k, v in tuned.items() if not k.startswith("_")})
        return base

    @property
    def tunable(self) -> dict:
        """pack 声明的可调项，selfloop 按它扫描。"""
        return dict(self.meta.get("tunable") or {})

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
    # utf-8-sig：容忍 Windows 记事本/PowerShell 写出的 BOM，否则 json.loads 直接抛
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _signature(d: Path) -> tuple:
    """目录内容指纹：路径 + mtime + 大小。改任意一个文件都会让缓存失效。"""
    key = str(d)
    now = time.time()
    with _CACHE_LOCK:
        hit = _SIG.get(key)
    if hit is not None and now - hit[0] < _SIG_TTL:
        return hit[1]
    items = []
    for p in sorted(d.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            try:
                st = p.stat()
            except OSError:
                continue
            items.append((str(p.relative_to(d)), st.st_mtime_ns, st.st_size))
    sig = tuple(items)
    with _CACHE_LOCK:
        _SIG[key] = (now, sig)
    return sig


def clear_cache():
    with _CACHE_LOCK:
        _CACHE.clear()
        _SIG.clear()


def load_pack(path: str | Path, cache: bool = True) -> Pack:
    d = Path(path).resolve()
    if not (d / "pack.json").exists():
        raise FileNotFoundError(f"不是有效的 pack 目录（缺少 pack.json）: {d}")

    key = str(d)
    sig = _signature(d) if cache else ()
    if cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]

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

    pack = Pack(
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
    if cache:
        with _CACHE_LOCK:
            _CACHE[key] = (sig, pack)
    return pack


def discover(root: str | Path) -> list[Path]:
    r = Path(root)
    if not r.exists():
        return []
    return sorted(p.parent for p in r.glob("*/pack.json"))
