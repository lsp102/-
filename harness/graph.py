"""graph.py — 把 workflow.json 转成可渲染的节点图

不做花哨的自动布局：按最长路径分层（rank），同层纵向排列。
对 10 种原语来说，这个布局已经够读，而且完全确定、可复现。
"""
from __future__ import annotations

NODE_W, NODE_H = 152, 56
GAP_X, GAP_Y = 210, 92


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

    rank = {nid: 0 for nid in nodes}
    for _ in range(len(nodes) + 2):
        changed = False
        for e in edges:
            want = rank[e["from"]] + 1
            if want > rank[e["to"]]:
                rank[e["to"]] = want
                changed = True
        if not changed:
            break

    cols: dict[int, list[str]] = {}
    for nid in nodes:
        cols.setdefault(rank[nid], []).append(nid)

    out_nodes = []
    for r in sorted(cols):
        for i, nid in enumerate(sorted(cols[r])):
            n = nodes[nid]
            out_nodes.append({
                "id": nid,
                "type": n.get("type", "?"),
                "actor": n.get("actor", ""),
                "x": r * GAP_X,
                "y": i * GAP_Y,
                "w": NODE_W,
                "h": NODE_H,
                "rank": r,
                "entry": nid == entry,
                "summary": _summarize(n),
            })

    width = (max(rank.values()) + 1 if rank else 1) * GAP_X
    height = (max((len(v) for v in cols.values()), default=1)) * GAP_Y
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
    if t == "parallel":
        return f"{len(n.get('branches') or [])} 分支"
    if t == "transform":
        return ", ".join((n.get("set") or {}).keys())
    if t == "end":
        return str(n.get("status", ""))
    return n.get("next", "") or ""
