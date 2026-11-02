"""expr.py — 受限表达式求值

工作流里的条件、参数、断言都用同一个表达式语言。
安全策略：AST 白名单，禁用 `eval`、属性名以下划线开头、非白名单函数调用。
这比"直接把 ctx 丢进 eval"慢一点点，但它是可以写进安全评审的。
"""
from __future__ import annotations

import ast
import threading

MAX_DEPTH = 32

# 编译缓存：同一段表达式源码在任何上下文里编译结果都一样，
# 所以放模块级共享。以前 Evaluator 每个 run 都新建，缓存随之全废，
# 5600 次求值触发了 39400 次 ast.walk（剖析数据）。
_PARSE_CACHE: dict[str, ast.Expression] = {}
_PARSE_LOCK = threading.Lock()
_PARSE_CACHE_MAX = 2000


class ExprError(Exception):
    """表达式非法或求值失败。"""


_ALLOWED_NODES = {
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.USub, ast.UAdd, ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
    ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot, ast.BinOp,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Name, ast.Load,
    ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Attribute,
    ast.Subscript, ast.IfExp, ast.Call, ast.keyword, ast.Store,
}


def _get(obj, key, default=None):
    try:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return obj[key] if key in obj else default
    except Exception:
        return default


_SAFE_FUNCS = {
    "len": len, "str": str, "int": int, "float": float, "bool": bool,
    "min": min, "max": max, "sum": sum, "any": any, "all": all, "abs": abs,
    "get": _get,
}


def _attr(obj, name: str):
    if name.startswith("_"):
        raise ExprError(f"禁止访问下划线属性: {name}")
    if isinstance(obj, dict):
        if name not in obj:
            raise ExprError(f"字段不存在: {name}")
        return obj[name]
    if isinstance(obj, (list, tuple)) and name.isdigit():
        return obj[int(name)]
    if hasattr(obj, name):
        return getattr(obj, name)
    raise ExprError(f"无法读取属性: {name}")


def _compile(src: str) -> ast.Expression:
    """解析 + 白名单校验，结果进模块级缓存。"""
    node = _PARSE_CACHE.get(src)
    if node is not None:
        return node
    try:
        node = ast.parse(src, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"表达式语法错误: {src}") from e
    for n in ast.walk(node):
        if type(n) not in _ALLOWED_NODES:
            raise ExprError(f"不允许的语法 {type(n).__name__}: {src}")
    with _PARSE_LOCK:
        if len(_PARSE_CACHE) >= _PARSE_CACHE_MAX:
            _PARSE_CACHE.clear()
        _PARSE_CACHE[src] = node
    return node


class Evaluator:
    """names 提供可引用的顶层变量（如 ctx / event / profile / params）。"""

    def __init__(self, names: dict | None = None):
        self.names: dict = names or {}
        self._cache: dict[str, ast.Expression] = {}

    def bind(self, **kw):
        self.names.update(kw)

    def eval(self, expr, **_unused):
        # 字面量直接返回，省一次 parse
        if isinstance(expr, (int, float, bool, type(None))):
            return expr
        if not isinstance(expr, str):
            return expr
        src = expr.strip()
        if src == "":
            return None
        node = self._cache.get(src)
        if node is None:
            node = _compile(src)
            self._cache[src] = node
        return self._ev(node.body, 0)

    # ------------------------------------------------------------------
    def _ev(self, n, depth: int):
        if depth > MAX_DEPTH:
            raise ExprError("表达式嵌套过深")
        d = depth + 1

        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in self.names:
                return self.names[n.id]
            raise ExprError(f"未定义变量: {n.id}")
        if isinstance(n, ast.Attribute):
            return _attr(self._ev(n.value, d), n.attr)
        if isinstance(n, ast.Subscript):
            return self._ev(n.value, d)[self._ev(n.slice, d)]
        if isinstance(n, ast.List):
            return [self._ev(e, d) for e in n.elts]
        if isinstance(n, ast.Tuple):
            return tuple(self._ev(e, d) for e in n.elts)
        if isinstance(n, ast.Dict):
            return {self._ev(k, d): self._ev(v, d) for k, v in zip(n.keys, n.values)}
        if isinstance(n, ast.UnaryOp):
            v = self._ev(n.operand, d)
            if isinstance(n.op, ast.Not):
                return not v
            if isinstance(n.op, ast.USub):
                return -v
            return +v
        if isinstance(n, ast.BoolOp):
            if isinstance(n.op, ast.And):
                v = True
                for e in n.values:
                    v = self._ev(e, d)
                    if not v:
                        return v
                return v
            v = False
            for e in n.values:
                v = self._ev(e, d)
                if v:
                    return v
            return v
        if isinstance(n, ast.Compare):
            left = self._ev(n.left, d)
            for op, right_node in zip(n.ops, n.comparators):
                right = self._ev(right_node, d)
                if not _cmp(op, left, right):
                    return False
                left = right
            return True
        if isinstance(n, ast.BinOp):
            return _bin(n.op, self._ev(n.left, d), self._ev(n.right, d))
        if isinstance(n, ast.IfExp):
            return self._ev(n.body, d) if self._ev(n.test, d) else self._ev(n.orelse, d)
        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name) or n.func.id not in _SAFE_FUNCS:
                raise ExprError("只允许调用白名单函数")
            fn = _SAFE_FUNCS[n.func.id]
            args = [self._ev(a, d) for a in n.args]
            kwargs = {k.arg: self._ev(k.value, d) for k in n.keywords}
            return fn(*args, **kwargs)
        raise ExprError(f"不支持的节点: {type(n).__name__}")


def _cmp(op, a, b) -> bool:
    if isinstance(op, ast.Eq):
        return a == b
    if isinstance(op, ast.NotEq):
        return a != b
    if isinstance(op, ast.Lt):
        return a < b
    if isinstance(op, ast.LtE):
        return a <= b
    if isinstance(op, ast.Gt):
        return a > b
    if isinstance(op, ast.GtE):
        return a >= b
    if isinstance(op, ast.In):
        return a in b
    if isinstance(op, ast.NotIn):
        return a not in b
    if isinstance(op, ast.Is):
        return a is b
    if isinstance(op, ast.IsNot):
        return a is not b
    raise ExprError("不支持的比较运算")


def _bin(op, a, b):
    if isinstance(op, ast.Add):
        return a + b
    if isinstance(op, ast.Sub):
        return a - b
    if isinstance(op, ast.Mult):
        return a * b
    if isinstance(op, ast.Div):
        return a / b
    if isinstance(op, ast.Mod):
        return a % b
    raise ExprError("不支持的算术运算")
