"""Hypothesis: bounds hold, waterfall sums, determinism, counterfactual == fresh evaluate."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from monetize360.engine import Context, compile_pack, evaluate, explain
from tests.conftest import load_pack

HOTEL = compile_pack(load_pack("hotel.yaml"))
AIRLINE = compile_pack(load_pack("airline.yaml"))

money = st.decimals(min_value=Decimal("100"), max_value=Decimal("7000"), places=2)   # hotel floor 70% <= ceiling
ratio = st.decimals(min_value=Decimal("0"), max_value=Decimal("1"), places=2)
instant = st.integers(min_value=0, max_value=3 * 365 * 24).map(
    lambda h: datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(hours=h))

hotel_ctx = st.builds(
    lambda base, occ, ref, as_of: Context(base_price=base, signals={"occupancy": occ}, as_of=as_of,
                                          reference_price=ref),
    money, ratio, st.none() | money, instant)

airline_ctx = st.builds(
    lambda seats, days, cabin, ref, as_of: Context(
        signals={"seats_sold": seats, "days_to_departure": days, "cabin": cabin}, as_of=as_of, reference_price=ref),
    ratio, st.integers(0, 365), st.sampled_from(["economy", "business"]),
    st.none() | st.decimals(min_value=Decimal("2500"), max_value=Decimal("45000"), places=2), instant)


def check(pack, ctx):
    result = evaluate(pack, ctx)
    rows = result.waterfall
    assert result.bounds["lower"] <= result.price <= result.bounds["upper"]
    assert result.price >= Decimal("0.01")
    assert result.price == sum((r.delta for r in rows[1:]), rows[0].delta)
    assert all(r.delta.as_tuple().exponent == -2 for r in rows)
    assert evaluate(pack, ctx) == result                                   # determinism
    for cf in explain(pack, ctx, result):
        assert "price" in cf, cf                                           # no counterfactual fails on a template
        rule = next(r for r in rows if r.kind == "rule" and f"without: {r.reason}" == cf["if"])
        assert cf["price"] == evaluate(pack, ctx, disabled_rules=frozenset({rule.step})).price


@settings(max_examples=300, deadline=None)
@given(hotel_ctx)
def test_hotel_invariants(ctx):
    check(HOTEL, ctx)


@settings(max_examples=300, deadline=None)
@given(airline_ctx)
def test_airline_invariants(ctx):
    check(AIRLINE, ctx)


@settings(max_examples=200, deadline=None)
@given(hotel_ctx)
def test_max_change_respected_when_reachable(ctx):
    result = evaluate(HOTEL, ctx)
    if ctx.reference_price is not None and "max_change ignored" not in " ".join(result.flags):
        ref = ctx.reference_price
        assert ref * Decimal("0.85") - Decimal("0.01") <= result.price <= ref * Decimal("1.15") + Decimal("0.01")
