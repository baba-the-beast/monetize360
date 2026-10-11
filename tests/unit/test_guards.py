"""Each guard layer; strictest wins; clamp rows appear in the waterfall."""
from decimal import Decimal

import pytest

from monetize360.engine import InvariantViolation, Policy, compile_pack, evaluate
from tests.conftest import ctx, make_pack


def pack(pct=None, floor=("abs", 500), ceiling=("abs", 2000)):
    guards = {"floor": {"op": floor[0], "value": floor[1]}, "ceiling": {"op": ceiling[0], "value": ceiling[1]}}
    if pct:
        guards["max_change"] = {"pct": pct, "per": "1h"}
    return compile_pack(make_pack(
        [{"name": "g", "stacking": "multiply",
          "rules": [{"id": "r", "when": "true", "then": {"op": "formula", "expr": "price * x"}, "reason": "x"}]}],
        guards=guards))


def last(result):
    return result.waterfall[-1]


def test_floor_clamp():
    r = evaluate(pack(), ctx(x="0.1"))
    assert (last(r).step, last(r).kind, last(r).delta, r.price) == ("floor", "clamp", Decimal("400.00"),
                                                                     Decimal("500.00"))


def test_ceiling_clamp():
    r = evaluate(pack(), ctx(x="3"))
    assert (last(r).step, r.price) == ("ceiling", Decimal("2000.00"))


def test_pct_of_base_floor():
    r = evaluate(pack(floor=("pct_of_base", 70)), ctx(x="0.5"))
    assert (last(r).step, r.price) == ("floor", Decimal("700.00"))


def test_no_clamp_no_row():
    r = evaluate(pack(), ctx(x="1.2"))
    assert all(row.kind != "clamp" for row in r.waterfall)


def test_max_change_clamp_up_and_down():
    up = evaluate(pack(pct=10), ctx(x="1.5", reference="1000"))
    assert (last(up).step, up.price) == ("max_change", Decimal("1100.00"))
    down = evaluate(pack(pct=10), ctx(x="0.7", reference="1000"))
    assert (last(down).step, down.price) == ("max_change", Decimal("900.00"))
    assert down.bounds == {"lower": Decimal("900.00"), "upper": Decimal("1100.00")}


def test_hard_limits_beat_max_change():
    # soft [1800, 2200] vs hard [500, 2000] -> [1800, 2000]; 3000 clamps to the CEILING, not max_change
    r = evaluate(pack(pct=10), ctx(x="3", reference="2000"))
    assert (last(r).step, r.price) == ("ceiling", Decimal("2000.00"))


def test_soft_range_outside_hard_is_ignored_and_flagged():
    r = evaluate(pack(pct=10), ctx(x="1", reference="5000"))       # soft [4500, 5500] misses [500, 2000]
    assert r.price == Decimal("1000.00")
    assert "max_change ignored: its range is outside the hard limits" in r.flags


def test_no_reference_means_no_max_change():
    r = evaluate(pack(pct=10), ctx(x="1.5"))
    assert r.price == Decimal("1500.00")


@pytest.mark.parametrize("policy, x, step, price", [
    (Policy(max_price=Decimal("1200")), "1.5", "tenant_max", "1200.00"),
    (Policy(min_price=Decimal("800")), "0.6", "tenant_min", "800.00"),
    (Policy(max_surge_x=Decimal("1.3")), "1.8", "tenant_surge", "1300.00"),
])
def test_tenant_policy_layer(policy, x, step, price):
    r = evaluate(pack(), ctx(x=x), policy=policy)
    assert (last(r).step, r.price) == (step, Decimal(price))


def test_equal_limits_name_the_hardest():
    r = evaluate(pack(), ctx(x="3"), policy=Policy(max_price=Decimal("2000")))    # == pack ceiling
    assert last(r).step == "tenant_max"


def test_crossing_limits_is_an_invariant_violation():
    with pytest.raises(InvariantViolation, match="limits cross"):
        evaluate(pack(), ctx(x="1"), policy=Policy(min_price=Decimal("2500")))
