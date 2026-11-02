"""memory.py — 跨运行的持久记忆（默认不落盘，按策略挑着存）

为什么需要它：引擎里原有的状态都是“一次运行内”的——
`ctx` 每个 run 重建，`shared` 只活在同一个 Engine 实例里。
于是「这位老人跌倒过几次」「上次的判定结论」这类信息一重启就没了。

但也不能什么都往盘上写：验证次数、临时中间值、调试痕迹写进去只会污染状态。
所以这里分两层：

  · 进程内层（ephemeral）：所有键都写，随进程消失；
  · 持久层（persisted）：只有**策略允许**的键才落到 `.harness_state/<pack>.json`。

策略由 pack 自己声明，写在 `pack.json` 的 `memory` 段（或 `data/memory.json`）：

```json
"memory": { "mode": "declared", "allow": ["falls:*"], "deny": ["falls:debug:*"] }
```

  mode = "all"       全部持久（旧的默认行为）
  mode = "declared"  只有匹配 allow 且不匹配 deny 的键才持久 —— 推荐
  mode = "off"       一个都不持久，纯进程内

节点上还可以用 `"persist": true/false` 单独覆盖策略。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

ENV_STATE_DIR = "HARNESS_STATE_DIR"
MODES = ("all", "declared", "off")


def default_state_dir() -> Path:
    """默认状态目录：仓库根下的 .harness_state（已进 .gitignore）。"""
    env = os.environ.get(ENV_STATE_DIR)
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / ".harness_state"


@dataclass
class MemoryPolicy:
    """哪些键可以落盘。由 pack 声明，节点可逐次覆盖。"""

    mode: str = "all"
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg: dict | None) -> "MemoryPolicy":
        cfg = cfg or {}
        mode = str(cfg.get("mode") or "all").lower()
        if mode not in MODES:
            mode = "all"
        return cls(mode=mode,
                   allow=[str(x) for x in (cfg.get("allow") or [])],
                   deny=[str(x) for x in (cfg.get("deny") or [])])

    def allows(self, key: str, override: bool | None = None) -> bool:
        if override is not None:
            return bool(override)
        if self.mode == "off":
            return False
        if any(fnmatch(key, p) for p in self.deny):
            return False
        if self.mode == "all":
            return True
        return any(fnmatch(key, p) for p in self.allow)

    def describe(self) -> str:
        if self.mode == "all":
            return "全部持久" + (f"（排除 {self.deny}）" if self.deny else "")
        if self.mode == "off":
            return "全部仅进程内"
        return f"仅持久 {self.allow}" + (f"，排除 {self.deny}" if self.deny else "")


class MemoryStore:
    """一个 pack 一份。进程内共享，落盘靠原子替换。"""

    def __init__(self, path: str | Path, policy: MemoryPolicy | None = None):
        self.path = Path(path)
        self.policy = policy or MemoryPolicy()
        self._lock = threading.RLock()
        self._data: dict | None = None          # 持久层（盘上的内容）
        self._ephemeral: dict = {}              # 进程内层（永不落盘）

    # ---------------------------------------------------------------- 内部

    def configure(self, policy: MemoryPolicy) -> None:
        with self._lock:
            self.policy = policy

    def _load(self) -> dict:
        if self._data is not None:
            return self._data
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            self._data = {}
        return self._data

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.path)      # 原子替换：读者要么看到旧文件，要么看到新文件

    # ---------------------------------------------------------------- 读写

    def get(self, key: str, default=None):
        with self._lock:
            if key in self._ephemeral:
                return self._ephemeral[key]
            return self._load().get(key, default)

    def set(self, key: str, value, persist: bool | None = None):
        with self._lock:
            self._ephemeral[key] = value
            if self.policy.allows(key, persist):
                data = self._load()
                data[key] = value
                self._persist()
            return value

    def incr(self, key: str, by: int | float = 1, persist: bool | None = None):
        with self._lock:
            cur = self.get(key, 0)
            if not isinstance(cur, (int, float)):
                cur = 0
            return self.set(key, cur + by, persist)

    def append(self, key: str, value, maxlen: int = 100, persist: bool | None = None):
        with self._lock:
            cur = self.get(key)
            if not isinstance(cur, list):
                cur = []
            return self.set(key, (cur + [value])[-maxlen:], persist)

    def delete(self, key: str) -> bool:
        with self._lock:
            hit = self._ephemeral.pop(key, None) is not None
            data = self._load()
            if key in data:
                del data[key]
                self._persist()
                hit = True
            return hit

    def _persist(self) -> None:
        self._data.setdefault("_meta", {})["updated_at"] = time.time()
        self._flush()

    # ---------------------------------------------------------------- 观测

    def is_persisted(self, key: str) -> bool:
        with self._lock:
            return key in self._load()

    def snapshot(self) -> dict:
        """持久层内容（盘上有什么）。"""
        with self._lock:
            return json.loads(json.dumps(self._load()))

    def session_snapshot(self) -> dict:
        """进程内层的全部键（含已持久化的）。"""
        with self._lock:
            return dict(self._ephemeral)

    def keys(self) -> list[str]:
        """当前可见的全部键：持久层 ∪ 进程内层。"""
        with self._lock:
            return sorted((set(self._load()) | set(self._ephemeral)) - {"_meta"})

    def persisted_keys(self) -> list[str]:
        with self._lock:
            return sorted(k for k in self._load() if k != "_meta")

    def clear(self) -> None:
        with self._lock:
            self._data = {}
            self._ephemeral.clear()
            self._flush()

    def reset_cache(self) -> None:
        """丢掉进程内缓存（含 ephemeral），下次重新读盘。"""
        with self._lock:
            self._data = None
            self._ephemeral.clear()


_STORES: dict[str, MemoryStore] = {}
_STORES_LOCK = threading.Lock()


def store_for(pack_id: str, state_dir: str | Path | None = None,
              policy: MemoryPolicy | None = None) -> MemoryStore:
    """同一进程内同一 pack 共用一个实例（避免两处缓存互相覆盖）。"""
    base = Path(state_dir) if state_dir else default_state_dir()
    path = base / f"{pack_id}.json"
    key = str(path.resolve())
    with _STORES_LOCK:
        st = _STORES.get(key)
        if st is None:
            st = MemoryStore(path, policy)
            _STORES[key] = st
        elif policy is not None:
            st.configure(policy)
        return st


def clear_stores() -> None:
    """清空实例缓存（测试用，不动磁盘）。"""
    with _STORES_LOCK:
        _STORES.clear()
