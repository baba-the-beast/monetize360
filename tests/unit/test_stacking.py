"""Each stacking policy, incl. the 4950 (multiply) vs 4650 (add) example."""
from decimal import Decimal

import pytest

from monetize360.engine import ExclusiveConflict, PackInvalid, compile_pack, evaluate
from tests.conftest import ctx, load_pack, make_pack


def hotel_with(stacking):
    pack = load_pack("hotel.yaml")
    pack["groups"][0]["stacking"] = stacking
    return compile_pack(pack)


def priced(pack, base="3000", **sig):
    return evaluate(pack, ctx(base, occupancy="0.85", is_weekend=True, festival="diwali", **sig))


def test_multiply_compounds():
    r = priced(hotel_with("multiply"))
    assert [(x.step, str(x.delta)) for x in r.waterfall] == [
        ("base", "3000.00"), ("high-occ", "600.00"), ("weekend", "360.00"), ("festival", "990.00")]
    assert r.price == Decimal("4950.00")


def test_add_sums_against_group_start():
    r = priced(hotel_with("add"))
    assert [str(x.delta) for x in r.waterfall[1:4]] == ["600.00", "300.00", "750.00"]
    assert r.price == Decimal("4650.00")


def test_max_keeps_the_largest_change():
    r = priced(hotel_with("max"))
    assert [x.step for x in r.waterfall if x.kind == "rule"] == ["festival"]
    assert r.price == Decimal("3750.00")


def test_first_match_keeps_the_first_fired_rule():
    r = priced(hotel_with("first_match"))
    assert [x.step for x in r.waterfall if x.kind == "rule"] == ["high-occ"]
    r = evaluate(hotel_with("first_match"), ctx("3000", occupancy="0.5", is_weekend=True, festival="none"))
    assert [x.step for x in r.waterfall if x.kind == "rule"] == ["weekend"]


def test_exclusive_conflict_and_single():
    with pytest.raises(ExclusiveConflict):
        priced(hotel_with("exclusive"))
    r = evaluate(hotel_with("exclusive"), ctx("3000", occupancy="0.5", is_weekend=False, festival="holi"))
    assert r.price == Decimal("3750.00")


def test_priority_then_file_order():
    rules = [
        {"id": "b-low", "when": "true", "then": {"op": "abs", "value": 1}, "reason": "b"},
        {"id": "a-low", "when": "true", "then": {"op": "abs", "value": 2}, "reason": "a"},
        {"id": "z-high", "when": "true", "then": {"op": "abs", "value": 3}, "reason": "z", "priority": 5},
    ]
    pack = compile_pack(make_pack([{"name": "g", "stacking": "multiply", "rules": rules}]))
    assert [x.step for x in evaluate(pack, ctx()).waterfall[1:]] == ["z-high", "b-low", "a-low"]


def test_fired_rule_with_zero_effect_still_emits_a_row(airline):
    r = evaluate(airline, ctx(None, seats_sold="0.2", days_to_departure=21, cabin="economy"))
    rows = {x.step: x.delta for x in r.waterfall}
    assert rows["days-to-departure"] == Decimal("0.00") and rows["cabin"] == Decimal("0.00")


def test_groups_run_top_to_bottom():
    groups = [
        {"name": "first", "stacking": "multiply", "rules": [
            {"id": "plus", "when": "true", "then": {"op": "abs", "value": 100}, "reason": "+100"}]},
        {"name": "second", "stacking": "multiply", "rules": [
            {"id": "double", "when": "true", "then": {"op": "multiplier", "value": 2}, "reason": "x2"}]},
    ]
    assert evaluate(compile_pack(make_pack(groups)), ctx()).price == Decimal("2200.00")


def test_effective_dates(airline):
    rule = {"id": "promo", "when": "true", "then": {"op": "pct", "value": -10}, "reason": "promo",
            "effective_from": "2026-10-14T00:00:00Z", "effective_to": "2026-10-15T00:00:00Z"}
    pack = compile_pack(make_pack([{"name": "g", "stacking": "multiply", "rules": [rule]}]))
    assert evaluate(pack, ctx()).price == Decimal("900.00")            # WEDNESDAY 2026-10-14 09:00Z
    from datetime import datetime, timezone
    later = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)          # effective_to is exclusive
    assert evaluate(pack, ctx(as_of=later)).price == Decimal("1000.00")


def test_set_not_allowed_in_add_group():
    with pytest.raises(PackInvalid, match="pack is invalid"):
        compile_pack(make_pack([{"name": "g", "stacking": "add", "rules": [
            {"id": "s", "when": "true", "then": {"op": "set", "value": 5}, "reason": "s"}]}]))


def test_duplicate_rule_ids_rejected():
    r = {"id": "same", "when": "true", "then": {"op": "pct", "value": 1}, "reason": "r"}
    with pytest.raises(PackInvalid):
        compile_pack(make_pack([{"name": "g", "stacking": "multiply", "rules": [r, r]}]))


def test_reason_placeholders_are_filled(hotel):
    r = priced(hotel)
    assert r.waterfall[-1].reason == "Festival: diwali"


# -------------------------------- review #11: enforced at compile until the linter exists

@pytest.mark.parametrize("step, ending, ok", [(50, 99, False), (1, 99, True), (50, 50, True), (10, 90, True),
                                              (30, 90, False), (50, 0, True)])
def test_ending_must_be_reachable_with_step(step, ending, ok):
    pack = make_pack([{"name": "g", "stacking": "multiply", "rules": [
        {"id": "r", "when": "true", "then": {"op": "pct", "value": 1}, "reason": "r"}]}],
        rounding={"step": step, "mode": "half_up", "ending": ending})
    if ok:
        compile_pack(pack)
    else:
        with pytest.raises(PackInvalid) as e:
            compile_pack(pack)
        assert e.value.details[0]["path"] == "rounding/ending"


def test_more_than_500_rules_rejected():
    rules = [{"id": f"r{i}", "when": "true", "then": {"op": "pct", "value": 0}, "reason": "r"} for i in range(200)]
    groups = [{"name": f"g{j}", "stacking": "multiply",
               "rules": [dict(r, id=f"{r['id']}-{j}") for r in rules]} for j in range(3)]     # 600 rules
    with pytest.raises(PackInvalid) as e:
        compile_pack(make_pack(groups))
    assert "600 rules in the pack; the limit is 500" in e.value.details[0]["message"]
