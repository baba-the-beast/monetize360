"""Counterfactuals (ENGINE_SPEC §9): real re-runs, bounded cost, engine bugs never hidden."""
import sys
from decimal import Decimal

import pytest

from monetize360.engine import InvariantViolation, compile_pack, evaluate, explain
from tests.conftest import ctx, make_pack

explain_module = sys.modules["monetize360.engine.explain"]   # the package re-exports the function under this name


def test_hotel_counterfactuals_match_api_md(hotel):
    c = ctx("3000", occupancy="0.85", is_weekend=True, festival="diwali", reference="4500")
    assert [(cf["if"], str(cf["price"])) for cf in explain(hotel, c, evaluate(hotel, c))] == [
        ("without: Festival: diwali", "3950.00"),
        ("without: Occupancy above 80%", "4150.00"),
        ("without: Weekend", "4500.00"),
    ]


def test_cost_is_bounded_however_many_rules_fire(monkeypatch):
    rules = [{"id": f"r{i}", "when": "true", "then": {"op": "abs", "value": i}, "reason": f"+{i}"}
             for i in range(1, 201)]
    pack = compile_pack(make_pack([{"name": "g", "stacking": "multiply", "rules": rules}]))
    c = ctx()
    result = evaluate(pack, c)
    calls = []
    real = explain_module.evaluate
    monkeypatch.setattr(explain_module, "evaluate", lambda *a, **k: calls.append(1) or real(*a, **k))
    cfs = explain(pack, c, result)
    assert len(calls) == 10                                         # was 200 re-runs
    assert [cf["if"] for cf in cfs] == [f"without: +{i}" for i in (200, 199, 198, 197, 196)]   # biggest first


def test_engine_bug_in_a_rerun_is_not_hidden(monkeypatch, hotel):
    c = ctx("3000", occupancy="0.85", is_weekend=True, festival="diwali")
    result = evaluate(hotel, c)

    def broken(*args, **kwargs):
        raise InvariantViolation("self-check failed")
    monkeypatch.setattr(explain_module, "evaluate", broken)
    with pytest.raises(InvariantViolation):
        explain(hotel, c, result)


def test_zero_delta_rule_in_first_match_still_explained():
    # "free" (+0) is chosen by first_match; without it, "surge" (+100) applies -> the price DOES change
    rules = [{"id": "free", "when": "true", "then": {"op": "abs", "value": 0}, "reason": "Promo: no surge"},
             {"id": "surge", "when": "true", "then": {"op": "abs", "value": 100}, "reason": "Surge"}]
    pack = compile_pack(make_pack([{"name": "g", "stacking": "first_match", "rules": rules}]))
    c = ctx()
    assert explain(pack, c, evaluate(pack, c)) == [{"if": "without: Promo: no surge", "price": Decimal("1100.00")}]


def test_no_rules_fired_no_counterfactuals(hotel):
    c = ctx("3000", occupancy="0.5", is_weekend=False, festival="none")
    assert explain(hotel, c, evaluate(hotel, c)) == []


def test_counterfactual_prices_are_fresh_evaluations(hotel):
    # ceiling case: 5775 clamps to 5000, so "subtracting the delta" would give wrong answers
    c = ctx("3500", occupancy="0.95", is_weekend=True, festival="holi", reference="5000")
    result = evaluate(hotel, c)
    rule_of = {row.reason: row.step for row in result.waterfall if row.kind == "rule"}
    cfs = explain(hotel, c, result)
    assert len(cfs) == 3
    for cf in cfs:
        rule = rule_of[cf["if"].removeprefix("without: ")]
        assert cf["price"] == evaluate(hotel, c, disabled_rules=frozenset({rule})).price


def test_a_counterfactual_that_fails_is_reported_not_dropped():
    # review S3: without A (+100), B divides by (1000 - 1000) -> rule_error; it must show, not vanish
    rules = [{"id": "a", "when": "true", "then": {"op": "abs", "value": 100}, "reason": "Plus 100"},
             {"id": "b", "when": "true", "then": {"op": "formula", "expr": "price / (price - 1000) * 1000"},
              "reason": "Odd formula"}]
    pack = compile_pack(make_pack([{"name": "g", "stacking": "multiply", "rules": rules}]))
    c = ctx()
    cfs = explain(pack, c, evaluate(pack, c))
    failed = [cf for cf in cfs if "error" in cf]
    assert [cf["if"] for cf in failed] == ["without: Plus 100"]
    assert failed[0]["error"]["code"] == "rule_error" and cfs[-1] is failed[0]      # failures listed last


def test_a_failed_counterfactual_is_never_cut_by_the_top_5():
    # 7 rules change the price; without "a" (+1), "b" divides by (1075 - 1075)
    rules = [{"id": "a", "when": "true", "then": {"op": "abs", "value": 1}, "reason": "Plus 1"}]
    rules += [{"id": f"x{i}", "when": "true", "then": {"op": "abs", "value": 10 + i}, "reason": f"x{i}"}
              for i in range(6)]
    rules += [{"id": "b", "when": "true", "then": {"op": "formula", "expr": "price / (price - 1075) * 1075"},
               "reason": "Odd"}]
    pack = compile_pack(make_pack([{"name": "g", "stacking": "multiply", "rules": rules}]))
    c = ctx()
    cfs = explain(pack, c, evaluate(pack, c))
    assert len([cf for cf in cfs if "price" in cf]) == 5
    assert [cf["if"] for cf in cfs if "error" in cf] == ["without: Plus 1"]
