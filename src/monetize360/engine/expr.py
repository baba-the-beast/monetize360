"""Rule expressions: the CEL subset packs use — && || ! == != < <= > >= in + - * / ( ) [ ], true false null,
'strings', numbers and signal names — TYPE-CHECKED and compiled once, evaluated with exact Decimal math.

Why not cel-python (ENGINE_SPEC §5.4): it evaluates numbers as floats (`price * 1.1` = 3300.0000000000005),
rejects `int > double` comparisons, and measured ~300 µs per condition. This evaluator walks Python's own
`ast` parse tree through a whitelist — no eval/exec, no attribute access, no calls, no loops — and checks
types at compile time, so `festival > 3` or `festival == 'diwaly'` is a PackInvalid at publish, never a
failure on a live request.
"""
from __future__ import annotations

import ast
import re
from decimal import Decimal, DecimalException, InvalidOperation
from typing import Any, Callable, Mapping

# expression types
NUM, BOOL, STR, NULL = "number", "true/false", "text", "null"
LIST = "list of "          # LIST + element type, e.g. "list of text"; "list of ?" for []


class ExprError(Exception):
    """Bad expression at compile time (syntax, unknown name, type error), or an arithmetic error at runtime."""


_TOKENS = re.compile(
    r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|&&|\|\||!=|!"""
    r"""|\b(?:true|false|null|True|False|None|and|or|not|is|if|else|lambda|for|import)\b"""
)
_CEL_TO_PY = {"&&": " and ", "||": " or ", "!=": "!=", "!": " not ",
              "true": "True", "false": "False", "null": "None"}

Fn = Callable[[dict], Any]


class Expr:
    """A compiled expression. Call it with {name: value}."""

    __slots__ = ("source", "names", "type", "_fn")

    def __init__(self, source: str, fn: Fn, names: frozenset[str], typ: str):
        self.source, self._fn, self.names, self.type = source, fn, names, typ

    def __call__(self, env: dict) -> Any:
        try:
            return self._fn(env)
        except DecimalException as e:
            raise ExprError(f"arithmetic error in '{self.source}': {type(e).__name__}") from None


def compile_expr(source: str, types: Mapping[str, str], *, enums: Mapping[str, tuple[str, ...]] | None = None,
                 result: str | None = None) -> Expr:
    """Compile `source`. `types` maps each visible name to NUM/BOOL/STR; `enums` gives allowed values of
    enum signals (comparing one to any other string literal is an error); `result` is the required type."""
    def translate(m: re.Match) -> str:
        tok = m.group(0)
        if tok[0] in "'\"":
            return tok
        if tok in _CEL_TO_PY:
            return _CEL_TO_PY[tok]
        raise ExprError(f"'{tok}' is not CEL — use &&, ||, ! and true/false/null")

    py_source = _TOKENS.sub(translate, source).strip()
    try:
        tree = ast.parse(py_source, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"syntax error in '{source}': {e.msg}") from None
    names: set[str] = set()
    fn, typ = _Builder(types, enums or {}, names, py_source).build(tree.body)
    if result is not None and typ != result:
        raise ExprError(f"'{source}' gives {typ}, but {result} is needed")
    return Expr(source, fn, frozenset(names), typ)


# ---------------------------------------------------------------- runtime helpers (defence in depth)

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


_EQUALITY = {ast.Eq: _eq, ast.NotEq: lambda a, b: not _eq(a, b)}
_ORDERING = {
    ast.Lt: lambda a, b: (lambda x, y: x < y)(*_order(a, b)),
    ast.LtE: lambda a, b: (lambda x, y: x <= y)(*_order(a, b)),
    ast.Gt: lambda a, b: (lambda x, y: x > y)(*_order(a, b)),
    ast.GtE: lambda a, b: (lambda x, y: x >= y)(*_order(a, b)),
}
_MEMBERSHIP = {ast.In: _member, ast.NotIn: lambda a, b: not _member(a, b)}
_ARITH: dict[type, Callable[[Decimal, Decimal], Decimal]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}


# ---------------------------------------------------------------- compiler: whitelist + types

class _Builder:
    def __init__(self, types: Mapping[str, str], enums: Mapping[str, tuple[str, ...]], names: set[str],
                 source: str):
        self.types, self.enums, self.names, self.source = types, enums, names, source

    def build(self, node: ast.AST) -> tuple[Fn, str]:
        method = getattr(self, f"_{type(node).__name__}", None)
        if method is None:
            raise ExprError(f"'{ast.unparse(node)}' is not allowed in a rule expression")
        return method(node)

    @staticmethod
    def _need(node: ast.AST, got: str, want: str, what: str) -> None:
        if got != want:
            raise ExprError(f"{what} needs {want}, but '{ast.unparse(node)}' is {got}")

    def _Constant(self, node: ast.Constant) -> tuple[Fn, str]:
        v = node.value
        if isinstance(v, bool):
            return (lambda env: v), BOOL
        if isinstance(v, str):
            return (lambda env: v), STR
        if v is None:
            return (lambda env: None), NULL
        if isinstance(v, (int, float)):
            # Read the digits from the source text, never via float: 0.80000000000000000001 must stay itself.
            text = ast.get_source_segment(self.source, node) or ""
            try:
                d = Decimal(text)
            except InvalidOperation:
                raise ExprError(f"number {text!r} must be written in decimal, e.g. 0.8 or 1e3") from None
            return (lambda env: d), NUM
        raise ExprError(f"unsupported literal {v!r}")

    def _Name(self, node: ast.Name) -> tuple[Fn, str]:
        if node.id not in self.types:
            raise ExprError(f"unknown name '{node.id}'")
        self.names.add(node.id)
        key = node.id
        return (lambda env: env[key]), self.types[key]

    def _List(self, node: ast.List | ast.Tuple) -> tuple[Fn, str]:
        built = [self.build(e) for e in node.elts]
        elem_types = {t for _, t in built}
        if len(elem_types) > 1:
            raise ExprError(f"list '{ast.unparse(node)}' mixes {', '.join(sorted(elem_types))}")
        items = [f for f, _ in built]
        return (lambda env: tuple(f(env) for f in items)), LIST + (elem_types.pop() if elem_types else "?")

    _Tuple = _List

    def _UnaryOp(self, node: ast.UnaryOp) -> tuple[Fn, str]:
        operand, t = self.build(node.operand)
        if isinstance(node.op, ast.Not):
            self._need(node.operand, t, BOOL, "'!'")
            return (lambda env: not _bool(operand(env))), BOOL
        if isinstance(node.op, (ast.USub, ast.UAdd)):
            self._need(node.operand, t, NUM, "a sign")
            if isinstance(node.op, ast.USub):
                return (lambda env: -_num(operand(env))), NUM
            return (lambda env: _num(operand(env))), NUM
        raise ExprError(f"'{ast.unparse(node)}' is not allowed in a rule expression")

    def _BoolOp(self, node: ast.BoolOp) -> tuple[Fn, str]:
        parts = []
        for v in node.values:
            f, t = self.build(v)
            self._need(v, t, BOOL, "'&&' / '||'")
            parts.append(f)
        if isinstance(node.op, ast.And):
            return (lambda env: all(_bool(p(env)) for p in parts)), BOOL
        return (lambda env: any(_bool(p(env)) for p in parts)), BOOL

    def _BinOp(self, node: ast.BinOp) -> tuple[Fn, str]:
        if type(node.op) not in _ARITH:
            raise ExprError(f"'{ast.unparse(node)}' is not allowed in a rule expression")
        op = _ARITH[type(node.op)]
        (left, lt), (right, rt) = self.build(node.left), self.build(node.right)
        self._need(node.left, lt, NUM, "arithmetic")
        self._need(node.right, rt, NUM, "arithmetic")
        return (lambda env: op(_num(left(env)), _num(right(env)))), NUM

    def _Compare(self, node: ast.Compare) -> tuple[Fn, str]:
        operands = [node.left, *node.comparators]
        built = [self.build(o) for o in operands]
        steps = []
        for i, op in enumerate(node.ops):
            (a_node, (_, at)), (b_node, (_, bt)) = (operands[i], built[i]), (operands[i + 1], built[i + 1])
            shown = f"'{ast.unparse(a_node)}' ({at}) and '{ast.unparse(b_node)}' ({bt})"
            if type(op) in _EQUALITY:
                if at != bt:
                    raise ExprError(f"can't compare {shown}")
                self._check_enum(a_node, b_node)
                steps.append(_EQUALITY[type(op)])
            elif type(op) in _ORDERING:
                if at != bt or at not in (NUM, STR):
                    raise ExprError(f"can't order {shown}")
                steps.append(_ORDERING[type(op)])
            elif type(op) in _MEMBERSHIP:
                if bt not in (LIST + at, LIST + "?"):
                    raise ExprError(f"'in' needs a list of {at}, got {shown}")
                self._check_enum(a_node, b_node)
                steps.append(_MEMBERSHIP[type(op)])
            else:
                raise ExprError(f"'{ast.unparse(node)}' is not allowed in a rule expression")
        fns = [f for f, _ in built]

        def compare(env: dict) -> bool:
            a = fns[0](env)
            for cmp, f in zip(steps, fns[1:], strict=True):
                b = f(env)
                if not cmp(a, b):
                    return False
                a = b
            return True
        return compare, BOOL

    def _check_enum(self, a: ast.AST, b: ast.AST) -> None:
        """`festival == 'diwaly'`: a text literal that the enum signal can never have is a typo, not a rule."""
        for name_node, other in ((a, b), (b, a)):
            if isinstance(name_node, ast.Name) and name_node.id in self.enums:
                allowed = self.enums[name_node.id]
                literals = other.elts if isinstance(other, (ast.List, ast.Tuple)) else [other]
                for lit in literals:
                    if isinstance(lit, ast.Constant) and isinstance(lit.value, str) and lit.value not in allowed:
                        raise ExprError(f"{name_node.id} is never '{lit.value}' (values: {', '.join(allowed)})")
