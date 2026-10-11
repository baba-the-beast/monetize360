"""engine/expr.py — the rule-expression evaluator. Rule text is UNTRUSTED input (written by business users),
so these tests prove that nothing except the CEL subset in ENGINE_SPEC §5.4 gets through, and that what does
get through can't reach Python internals, call anything, or run code."""
from decimal import Decimal

import pytest

from monetize360.engine.expr import ExprError, compile_expr

SIGNALS = {"x", "occupancy", "festival", "is_weekend", "price"}


def run(src: str, **env):
    return compile_expr(src, SIGNALS)(env)


# ---------------------------------------------------------------- attacks are rejected at compile time

@pytest.mark.parametrize("attack", [
    # imports / builtins
    "__import__('os')",
    "__import__('os').system('rm -rf /')",
    "__builtins__",
    "open('/etc/passwd')",
    "eval('1')",
    "exec('import os')",
    "compile('1', 'x', 'eval')",
    "globals()",
    "getattr(x, '__class__')",
    # attribute access, incl. dunder escapes
    "x.__class__",
    "x.__class__.__mro__[1].__subclasses__()",
    "().__class__.__bases__[0]",
    "''.__class__",
    "festival.upper()",
    "x.real",
    # function / method calls of any kind
    "len(festival)",
    "max(x, 1)",
    "int('5')",
    "print(x)",
    # other Python constructs that are not CEL
    "lambda: 1",
    "[c for c in festival]",
    "{'a': 1}",
    "{1, 2}",
    "festival[0]",
    "x if x else 1",
    "(y := 1)",
    "x ** 99999999",
    "x % 2",
    "x // 2",
    "f'{x}'",
    "x and occupancy",      # Python boolean words are not CEL
    "not is_weekend",
    "x is None",
    "await x",
    "yield x",
])
def test_attack_is_rejected_at_compile_time(attack):
    with pytest.raises(ExprError):
        compile_expr(attack, SIGNALS)


@pytest.mark.parametrize("src", ["import os", "x = 1", "del x", "x; y", "def f(): pass", "\nimport os"])
def test_statements_are_not_expressions(src):
    with pytest.raises(ExprError):
        compile_expr(src, SIGNALS)


def test_only_declared_names_are_visible():
    with pytest.raises(ExprError, match="unknown name 'os'"):
        compile_expr("os", SIGNALS)
    with pytest.raises(ExprError, match="unknown name 'ocupancy'"):
        compile_expr("ocupancy > 0.8", SIGNALS)


def test_operators_inside_strings_are_left_alone():
    # '&&', '!' and 'and' inside a string literal are data, not syntax
    assert run("festival == 'rock && roll!'", festival="rock && roll!") is True
    assert run("festival != 'salt and pepper'", festival="x") is True


def test_evaluation_never_reaches_python_objects():
    # even if a signal value is a Python object, the evaluator has no way to touch its attributes/methods
    class Evil:
        def __eq__(self, other):
            raise AssertionError("must not be called")
    expr = compile_expr("x == 1", SIGNALS)
    assert expr({"x": Evil()}) is False          # different type category -> plain False, no __eq__ call


# ---------------------------------------------------------------- the CEL subset works, exactly

@pytest.mark.parametrize("src, env, expected", [
    ("occupancy > 0.8", {"occupancy": Decimal("0.85")}, True),
    ("occupancy > 0.8", {"occupancy": Decimal("0.80")}, False),
    ("occupancy >= 0.8", {"occupancy": Decimal("0.80")}, True),     # floats would say False
    ("x == 0.3", {"x": Decimal("0.1") + Decimal("0.2")}, True),       # floats would say False
    ("is_weekend && occupancy > 0.5", {"is_weekend": True, "occupancy": Decimal("0.6")}, True),
    ("!is_weekend || x < 0", {"is_weekend": True, "x": Decimal(1)}, False),
    ("festival != 'none'", {"festival": "diwali"}, True),
    ("festival in ['diwali', 'holi']", {"festival": "holi"}, True),
    ("x > 7.5", {"x": 8}, True),                                      # int vs decimal: fine (cel-python errors)
    ("true", {}, True),
    ("null == null", {}, True),
    ("1 < x < 5", {"x": 3}, True),
])
def test_cel_subset(src, env, expected):
    assert run(src, **env) is expected


def test_formula_math_is_exact_decimal():
    assert run("price * 1.1", price=Decimal("3000")) == Decimal("3300.0")    # cel-python: 3300.0000000000005
    assert run("(price - 100) / 3", price=Decimal("400")) == Decimal(100)


@pytest.mark.parametrize("src, env", [
    ("x > 'a'", {"x": Decimal(1)}),          # ordering different types
    ("x + 'a'", {"x": Decimal(1)}),          # arithmetic on a string
    ("is_weekend && x", {"is_weekend": True, "x": Decimal(1)}),   # && needs true/false
    ("price / 0", {"price": Decimal(1)}),    # division by zero
])
def test_runtime_type_errors_raise(src, env):
    with pytest.raises(ExprError):
        run(src, **env)
