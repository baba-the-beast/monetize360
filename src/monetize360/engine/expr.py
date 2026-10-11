"""Rule expressions: the CEL subset packs use — && || ! == != < <= > >= in + - * / ( ) [ ], true false null,
'strings', numbers and signal names — compiled once and evaluated with exact Decimal math.

Why not cel-python (ENGINE_SPEC §5.4): it evaluates numbers as floats (`price * 1.1` = 3300.0000000000005),
rejects `int > double` comparisons, and measured ~300 µs per condition. This evaluator walks Python's own
`ast` parse tree through a whitelist — no eval/exec, no attribute access, no calls, no loops.
"""
from __future__ import annotations

import ast
import re
from decimal import Decimal, DecimalException
from typing import Any, Callable


class ExprError(Exception):
    """Bad expression at compile time, or a type error at evaluation time."""


_TOKENS = re.compile(
    r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|&&|\|\||!=|!"""
    r"""|\b(?:true|false|null|and|or|not|is|if|else|lambda|for|import)\b"""
)
_CEL_TO_PY = {"&&": " and ", "||": " or ", "!=": "!=", "!": " not ",
              "true": "True", "false": "False", "null": "None"}

Fn = Callable[[dict], Any]


class Expr:
    """A compiled expression. Call it with {name: value}."""

    __slots__ = ("source", "names", "_fn")

    def __init__(self, source: str, fn: Fn, names: frozenset[str]):
        self.source, self._fn, self.names = source, fn, names

    def __call__(self, env: dict) -> Any:
        try:
            return self._fn(env)
        except DecimalException as e:
            raise ExprError(f"arithmetic error in '{self.source}': {type(e).__name__}") from None


def compile_expr(source: str, allowed_names: set[str] | frozenset[str]) -> Expr:
    def translate(m: re.Match) -> str:
        tok = m.group(0)
        if tok[0] in "'\"":
            return tok
        if tok in _CEL_TO_PY:
            return _CEL_TO_PY[tok]
        raise ExprError(f"'{tok}' is not CEL — use &&, ||, ! and true/false")

    try:
        tree = ast.parse(_TOKENS.sub(translate, source).strip(), mode="eval")
    except SyntaxError as e:
        raise ExprError(f"syntax error in '{source}': {e.msg}") from None
    names: set[str] = set()
    fn = _build(tree.body, frozenset(allowed_names), names)
    return Expr(source, fn, frozenset(names))


# ---------------------------------------------------------------- helpers

def _is_num(v: Any) -> bool:
    return isinstance(v, (Decimal, int)) and not isinstance(v, bool)


def _num(v: Any) -> Decimal:
    if not _is_num(v):
        raise ExprError(f"expected a number, got {v!r}")
    return v if isinstance(v, Decimal) else Decimal(v)


def _bool(v: Any) -> bool:
    if not isinstance(v, bool):
        raise ExprError(f"expected true/false, got {v!r}")
    return v


def _category(v: Any) -> str:
    if isinstance(v, bool):
        return "bool"
    if _is_num(v):
        return "num"
    return type(v).__name__


def _eq(a: Any, b: Any) -> bool:
    if _category(a) != _category(b):
        return False
    return _num(a) == _num(b) if _is_num(a) else a == b


def _order(a: Any, b: Any) -> tuple:
    if _is_num(a) and _is_num(b):
        return _num(a), _num(b)
    if isinstance(a, str) and isinstance(b, str):
        return a, b
    raise ExprError(f"can't order {a!r} and {b!r}")


def _member(a: Any, b: Any) -> bool:
    if not isinstance(b, tuple):
        raise ExprError(f"'in' needs a list, got {b!r}")
    return any(_eq(a, x) for x in b)


_COMPARE: dict[type, Callable[[Any, Any], bool]] = {
    ast.Eq: _eq,
    ast.NotEq: lambda a, b: not _eq(a, b),
    ast.Lt: lambda a, b: (lambda x, y: x < y)(*_order(a, b)),
    ast.LtE: lambda a, b: (lambda x, y: x <= y)(*_order(a, b)),
    ast.Gt: lambda a, b: (lambda x, y: x > y)(*_order(a, b)),
    ast.GtE: lambda a, b: (lambda x, y: x >= y)(*_order(a, b)),
    ast.In: _member,
    ast.NotIn: lambda a, b: not _member(a, b),
}
_ARITH: dict[type, Callable[[Decimal, Decimal], Decimal]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}


def _build(node: ast.AST, allowed: frozenset[str], names: set[str]) -> Fn:
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, (bool, str)) or v is None:
            return lambda env: v
        if isinstance(v, (int, float)):
            d = Decimal(str(v))
            return lambda env: d
        raise ExprError(f"unsupported literal {v!r}")

    if isinstance(node, ast.Name):
        if node.id not in allowed:
            raise ExprError(f"unknown name '{node.id}'")
        names.add(node.id)
        key = node.id
        return lambda env: env[key]

    if isinstance(node, (ast.List, ast.Tuple)):
        items = [_build(e, allowed, names) for e in node.elts]
        return lambda env: tuple(f(env) for f in items)

    if isinstance(node, ast.UnaryOp):
        operand = _build(node.operand, allowed, names)
        if isinstance(node.op, ast.Not):
            return lambda env: not _bool(operand(env))
        if isinstance(node.op, ast.USub):
            return lambda env: -_num(operand(env))
        if isinstance(node.op, ast.UAdd):
            return lambda env: _num(operand(env))

    if isinstance(node, ast.BoolOp):
        parts = [_build(v, allowed, names) for v in node.values]
        if isinstance(node.op, ast.And):
            return lambda env: all(_bool(p(env)) for p in parts)
        return lambda env: any(_bool(p(env)) for p in parts)

    if isinstance(node, ast.BinOp) and type(node.op) in _ARITH:
        op = _ARITH[type(node.op)]
        left, right = _build(node.left, allowed, names), _build(node.right, allowed, names)
        return lambda env: op(_num(left(env)), _num(right(env)))

    if isinstance(node, ast.Compare) and all(type(o) in _COMPARE for o in node.ops):
        first = _build(node.left, allowed, names)
        rest = [(_COMPARE[type(o)], _build(c, allowed, names)) for o, c in zip(node.ops, node.comparators, strict=True)]

        def compare(env: dict) -> bool:
            a = first(env)
            for cmp, f in rest:
                b = f(env)
                if not cmp(a, b):
                    return False
                a = b
            return True
        return compare

    raise ExprError(f"'{ast.unparse(node)}' is not allowed in a rule expression")
