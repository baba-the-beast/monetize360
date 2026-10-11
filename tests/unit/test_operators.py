"""One test per operator incl. edge cases (band boundaries, curve endpoints)."""
from decimal import Decimal

import pytest

from monetize360.engine import LookupMiss, PackInvalid, compile_pack, evaluate
from tests.conftest import ctx, make_pack

NUM = {"x": {"type": "number", "source": "request", "default": 0}}


def price_with(then, signals=None, base="1000", **sig):
    pack = compile_pack(make_pack(
        [{"name": "g", "stacking": "multiply", "rules": [{"id": "r", "when": "true", "then": then, "reason": "r"}]}],
        signals=signals or NUM))
    return evaluate(pack, ctx(base, **sig)).price


@pytest.mark.parametrize("then, expected", [
    ({"op": "pct", "value": 20}, "1200.00"),
    ({"op": "pct", "value": -12.5}, "875.00"),
    ({"op": "abs", "value": -200}, "800.00"),
    ({"op": "multiplier", "value": 1.5}, "1500.00"),
    ({"op": "set", "value": 999}, "999.00"),
    ({"op": "formula", "expr": "price * 1.1 + 5"}, "1105.00"),     # exact: no 1100.0000000000002
])
def test_simple_operators(then, expected):
    assert price_with(then) == Decimal(expected)


def test_lookup_table_single_key_and_default():
    sig = {"cabin": {"type": "enum", "values": ["eco", "biz", "first"], "source": "request"}}
    then = {"op": "lookup_table", "signal": "cabin", "table": {"eco": 1.0, "biz": 2.8}, "default": 5}
    assert price_with(then, sig, cabin="biz") == Decimal("2800.00")
    assert price_with(then, sig, cabin="first") == Decimal("5000.00")


def test_lookup_table_miss_without_default():
    sig = {"cabin": {"type": "enum", "values": ["eco", "first"], "source": "request"}}
    with pytest.raises(LookupMiss):
        price_with({"op": "lookup_table", "signal": "cabin", "table": {"eco": 1.0}}, sig, cabin="first")


def test_lookup_table_numeric_and_bool_keys_normalised():
    sig = {"n": {"type": "number", "source": "request"}, "b": {"type": "bool", "source": "request"}}
    assert price_with({"op": "lookup_table", "signal": "n", "table": {0.5: 2}}, sig, n="0.50", b=True) \
        == Decimal("2000.00")
    assert price_with({"op": "lookup_table", "signal": "b", "table": {True: 3}}, sig, n="1", b=True) \
        == Decimal("3000.00")


def test_lookup_table_multi_dimensional():
    sig = {"zone": {"type": "enum", "values": ["a", "b"], "source": "request"},
           "size": {"type": "int", "source": "request"}}
    then = {"op": "lookup_table", "signals": ["zone", "size"],
            "rows": [{"key": ["a", 1], "value": 1.1}, {"key": ["b", 2], "value": 1.3}]}
    assert price_with(then, sig, zone="b", size=2) == Decimal("1300.00")
    with pytest.raises(LookupMiss):
        price_with(then, sig, zone="a", size=2)


@pytest.mark.parametrize("x, expected", [("0.5", "1100.00"), ("0.51", "1200.00"), ("1", "1200.00")])
def test_band_upto_is_inclusive(x, expected):
    then = {"op": "band", "signal": "x", "bands": [{"upto": 1, "factor": 1.2}, {"upto": 0.5, "factor": 1.1}]}
    assert price_with(then, x=x) == Decimal(expected)       # bands are sorted by upto at compile time


def test_band_above_last_is_lookup_miss():
    with pytest.raises(LookupMiss, match="above the last band"):
        price_with({"op": "band", "signal": "x", "bands": [{"upto": 1, "factor": 1.2}]}, x="1.01")


@pytest.mark.parametrize("x, expected", [
    ("-5", "1600.00"),            # below first point -> first factor
    ("0", "1600.00"),
    ("3.5", "1450.00"),           # halfway 1.6 -> 1.3
    ("7", "1300.00"),
    ("100", "900.00"),            # beyond last point -> last factor
])
def test_piecewise_curve(x, expected):
    then = {"op": "piecewise_curve", "signal": "x", "points": [[0, 1.6], [7, 1.3], [21, 1.0], [60, 0.9]]}
    assert price_with(then, x=x) == Decimal(expected)


@pytest.mark.parametrize("x, expected", [("0", "1000.00"), ("10", "950.00"), ("49", "950.00"), ("50", "900.00")])
def test_tier(x, expected):
    then = {"op": "tier", "signal": "x", "tiers": [{"from": 10, "pct": -5}, {"from": 50, "pct": -10}]}
    assert price_with(then, x=x) == Decimal(expected)


def test_base_operators():
    sig = {"seats": {"type": "ratio", "source": "request"},
           "cost": {"type": "money", "source": "request"}, "comp": {"type": "money", "source": "request"}}
    rules = [{"name": "g", "stacking": "multiply",
              "rules": [{"id": "r", "when": "true", "then": {"op": "pct", "value": 0}, "reason": "r"}]}]

    def base_of(base, **s):
        return evaluate(compile_pack(make_pack(rules, signals=sig, base=base)), ctx(None, **s)).price

    s = {"seats": "0.3", "cost": "800", "comp": "1000"}
    assert base_of({"op": "set", "price": 499}, **s) == Decimal("499.00")
    assert base_of({"op": "band", "signal": "seats", "bands": [{"upto": 0.2, "price": 3500},
                                                               {"upto": 1, "price": 4800}]}, **s) == Decimal("4800.00")
    assert base_of({"op": "cost_plus", "cost_signal": "cost", "margin_pct": 30}, **s) == Decimal("1040.00")
    assert base_of({"op": "competitor_rel", "competitor_signal": "comp", "pct": -5}, **s) == Decimal("950.00")


@pytest.mark.parametrize("when, error", [
    ("ocupancy > 0.8", "unknown name"),
    ("x and true", "not CEL"),
    ("__import__('os')", "not allowed"),
    ("x.real > 1", "not allowed"),
    ("x >", "syntax error"),
])
def test_bad_expressions_are_rejected_at_compile_time(when, error):
    with pytest.raises(PackInvalid) as e:
        compile_pack(make_pack([{"name": "g", "stacking": "multiply",
                                 "rules": [{"id": "r", "when": when, "then": {"op": "pct", "value": 1},
                                            "reason": "r"}]}]))
    assert error in e.value.details[0]["message"]


@pytest.mark.parametrize("when, x, fires", [
    ("x > 0.8", "0.8", False),
    ("x >= 0.8", "0.80", True),           # exact decimal compare; floats would get this wrong
    ("x > 1 && !(x > 5)", "3", True),
    ("x < 1 || x == 9", "9", True),
    ("x in [1, 2, 3]", "2", True),
])
def test_expression_semantics(when, x, fires):
    pack = compile_pack(make_pack([{"name": "g", "stacking": "multiply",
                                    "rules": [{"id": "r", "when": when, "then": {"op": "pct", "value": 10},
                                               "reason": "r"}]}]))
    assert (evaluate(pack, ctx(x=x)).price == Decimal("1100.00")) is fires


# ---------------------------------------------------------------- review fixes: typed keys, typed rules

def test_numeric_table_key_written_as_text_still_matches():
    # "0.50" in YAML is a string; x = 0.5 used to miss it silently and fall back to the default
    assert price_with({"op": "lookup_table", "signal": "x", "table": {"0.50": 2}, "default": 1}, x="0.5") \
        == Decimal("2000.00")


@pytest.mark.parametrize("signals, table, why", [
    ({"x": {"type": "number", "source": "request"}}, {"half": 2}, "not a number"),
    ({"c": {"type": "enum", "values": ["eco", "biz"], "source": "request"}}, {"ecco": 2}, "can never match"),
    ({"b": {"type": "bool", "source": "request"}}, {"yes": 2}, "can never match"),
])
def test_table_key_that_can_never_match_is_rejected(signals, table, why):
    signal = next(iter(signals))
    with pytest.raises(PackInvalid) as e:
        compile_pack(make_pack([{"name": "g", "stacking": "multiply", "rules": [
            {"id": "r", "when": "true", "then": {"op": "lookup_table", "signal": signal, "table": table},
             "reason": "r"}]}], signals=signals))
    assert why in e.value.details[0]["message"]


@pytest.mark.parametrize("when", ["festival > 3", "festival == 3", "festival == 'diwaly'", "occupancy + 1"])
def test_wrongly_typed_rule_never_publishes(when):
    from tests.conftest import load_pack
    pack = load_pack("hotel.yaml")
    pack["groups"][0]["rules"][2]["when"] = when
    with pytest.raises(PackInvalid) as e:
        compile_pack(pack)
    assert e.value.details[0]["path"] == "groups/0/rules/2/when"


# -------------------------------- review #4: pack failures = rule_error, naming the rule

def test_formula_division_by_zero_is_a_rule_error_naming_the_rule():
    from monetize360.engine import RuleError
    pack = compile_pack(make_pack([{"name": "g", "stacking": "multiply", "rules": [
        {"id": "risky", "when": "true", "then": {"op": "formula", "expr": "price / (x - 2)"}, "reason": "r"}]}]))
    assert evaluate(pack, ctx(x="4")).price == Decimal("500.00")
    with pytest.raises(RuleError, match="rule risky: arithmetic error") as e:
        evaluate(pack, ctx(x="2"))
    assert e.value.code == "rule_error" and e.value.details == [{"path": "rules.risky.then"}]


def test_self_check_failure_is_still_an_invariant_violation():
    from monetize360.engine import InvariantViolation, Result, Row
    from monetize360.engine.evaluate import self_check
    bad = Result(price=Decimal("101.00"), currency="INR", flags=(), signals_used={},
                 waterfall=(Row("base", "base", Decimal("100.00"), Decimal("100.00"), "b"),),
                 bounds={"lower": Decimal("1.00"), "upper": Decimal("1000.00")})
    with pytest.raises(InvariantViolation, match="self-check failed"):
        self_check(bad)


def test_formula_must_produce_a_number():
    with pytest.raises(PackInvalid, match="pack is invalid"):
        compile_pack(make_pack([{"name": "g", "stacking": "multiply", "rules": [
            {"id": "r", "when": "true", "then": {"op": "formula", "expr": "x > 1"}, "reason": "r"}]}]))
