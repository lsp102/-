"""graph.py — 把 workflow.json 转成可渲染的节点图

不做花哨的自动布局：按最长路径分层（rank），同层纵向排列。
对 10 种原语来说，这个布局已经够读，而且完全确定、可复现。
"""
from __future__ import annotations

NODE_W, NODE_H = 152, 50
GAP_X, GAP_Y = 210, 68
PER_BAND = 6        # 每 6 列折一行，否则 29 个节点会拉成一条 6 米长的纸带
BAND_GAP = 24


def _edges_of(nid: str, node: dict) -> list[tuple[str, str]]:
    t = node.get("type")
    out: list[tuple[str, str]] = []

    if t == "condition":
        if node.get("then"):
            out.append((node["then"], "是"))
        if node.get("else"):
            out.append((node["else"], "否"))
    elif t == "parallel":
        for b in node.get("branches") or []:
            out.append((b, "并行"))
        if node.get("next"):
            out.append((node["next"], "汇合"))
    elif t == "foreach":
        if node.get("body"):
            out.append((node["body"], "每项"))
        if node.get("on_break"):
            out.append((node["on_break"], "命中"))
        if node.get("on_exhausted"):
            out.append((node["on_exhausted"], "耗尽"))
    else:
        if node.get("next"):
            out.append((node["next"], ""))
    if node.get("on_cancel"):
        out.append((node["on_cancel"], "撤销"))
    if node.get("on_error"):
        out.append((node["on_error"], "异常"))
    return out


def _back_edges(nodes: dict, edges: list[dict], entry: str | None) -> set[tuple[str, str]]:
    """找出回边（指回祖先的边）。分层时忽略它们，否则循环会让 rank 一路涨到上限。"""
    adj: dict[str, list[str]] = {}
    for e in edges:
        adj.setdefault(e["from"], []).append(e["to"])

    state: dict[str, int] = {}      # 1 = 正在栈上，2 = 已完成
    back: set[tuple[str, str]] = set()

    def dfs(u: str):
        state[u] = 1
        for v in adj.get(u, []):
            if state.get(v) == 1:
                back.add((u, v))
            elif v not in state:
                dfs(v)
        state[u] = 2

    order = ([entry] if entry in nodes else []) + [n for n in nodes if n != entry]
    for nid in order:
        if nid not in state:
            dfs(nid)
    return back


def build(workflow: dict) -> dict:
    nodes: dict = workflow.get("nodes") or {}
    entry = workflow.get("entry")

    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for nid, n in nodes.items():
        for to, label in _edges_of(nid, n):
            if to in nodes and (nid, to) not in seen:
                seen.add((nid, to))
                edges.append({"from": nid, "to": to, "label": label})

    back = _back_edges(nodes, edges, entry)
    for e in edges:
        e["back"] = (e["from"], e["to"]) in back

    rank = {nid: 0 for nid in nodes}
    forward = [e for e in edges if not e["back"]]
    # 汇合点必须排在所有分支之后：给 branches → next 加只用于分层的虚拟边。
    # 否则 join 会和分支挤在同一列，图看起来是“并排”而不是“汇合”。
    rank_edges = list(forward)
    for nid, n in nodes.items():
        if n.get("type") != "parallel":
            continue
        join = n.get("next")
        if join in nodes:
            for b in n.get("branches") or []:
                if b in nodes:
                    rank_edges.append({"from": b, "to": join})
    for _ in range(len(nodes) + 2):
        changed = False
        for e in rank_edges:
            want = rank[e["from"]] + 1
            if want > rank[e["to"]]:
                rank[e["to"]] = want
                changed = True
        if not changed:
            break

    cols: dict[int, list[str]] = {}
    for nid in nodes:
        cols.setdefault(rank[nid], []).append(nid)

    ranked = sorted(cols)
    # 折行布局：列数超过 PER_BAND 就换一条带，避免超长工作流横向溢出
    band_rows: dict[int, int] = {}
    for r in ranked:
        b = r // PER_BAND
        band_rows[b] = max(band_rows.get(b, 0), len(cols[r]))
    band_top: dict[int, int] = {}
    cursor = 0
    for b in sorted(band_rows):
        band_top[b] = cursor
        cursor += band_rows[b] * GAP_Y + BAND_GAP

    out_nodes = []
    for r in ranked:
        b = r // PER_BAND
        for i, nid in enumerate(sorted(cols[r])):
            n = nodes[nid]
            out_nodes.append({
                "id": nid,
                "type": n.get("type", "?"),
                "actor": n.get("actor", ""),
                "out": n.get("out", ""),
                "x": (r % PER_BAND) * GAP_X,
                "y": band_top[b] + i * GAP_Y,
                "w": NODE_W,
                "h": NODE_H,
                "rank": r,
                "band": b,
                "entry": nid == entry,
                "summary": _summarize(n),
            })

    used_cols = min(PER_BAND, (ranked[-1] + 1) if ranked else 1)
    width = used_cols * GAP_X
    height = max(cursor - BAND_GAP, GAP_Y)
    return {"nodes": out_nodes, "edges": edges, "entry": entry,
            "width": width, "height": height}


def _summarize(n: dict) -> str:
    t = n.get("type")
    if t == "condition":
        return str(n.get("when", ""))[:60]
    if t == "agent":
        return f"{n.get('agent')} → {n.get('out', '')}"
    if t == "tool":
        return str(n.get("tool", ""))
    if t == "foreach":
        return f"over {n.get('over', '')}"
    if t == "wait":
        return f"duration {n.get('duration', '')}"
    if t == "knowledge":
        return f"query {n.get('query', '')}"
    if t == "memory":
        return f"{n.get('op', 'get')} {n.get('key') or n.get('key_expr', '')}"
    if t == "parallel":
        return f"{len(n.get('branches') or [])} 分支"
    if t == "transform":
        return ", ".join((n.get("set") or {}).keys())
    if t == "end":
        return str(n.get("status", ""))
    return n.get("next", "") or ""
