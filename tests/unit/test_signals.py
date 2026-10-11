"""Signal resolution (ENGINE_SPEC §3): missing -> default + flag, out of range -> SignalInvalid, stale -> on_stale
policy, public override rejected unless allowed."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from monetize360.engine import (
    Context,
    Policy,
    SignalInvalid,
    SignalMissing,
    SignalStale,
    compile_pack,
    evaluate,
)
from tests.conftest import WEDNESDAY, ctx, load_pack, make_pack

DIWALI_EVENING_IST = datetime(2026, 11, 8, 13, 0, tzinfo=timezone.utc)   # 18:30 in Asia/Kolkata, a Sunday


def test_missing_signal_uses_default_and_flags(hotel):
    r = evaluate(hotel, ctx("3000", is_weekend=False, festival="none"))
    assert r.signals_used["occupancy"] == {"value": Decimal("0.5"), "source": "default"}
    assert "occupancy missing, used default 0.5" in r.flags


def test_out_of_range_is_rejected_not_priced(hotel):
    with pytest.raises(SignalInvalid) as e:
        evaluate(hotel, ctx("3000", occupancy=7))
    assert e.value.message == "occupancy must be between 0 and 1"
    assert e.value.details == [{"path": "signals.occupancy", "value": 7}]


@pytest.mark.parametrize("raw", [True, "high", None, [1]])
def test_wrong_type_is_rejected(hotel, raw):
    with pytest.raises(SignalInvalid):
        evaluate(hotel, ctx("3000", occupancy=raw))


def test_numeric_strings_and_numbers_are_exact(hotel):
    a = evaluate(hotel, ctx("3000", occupancy="0.85", is_weekend=False, festival="none"))
    b = evaluate(hotel, ctx("3000", occupancy=0.85, is_weekend=False, festival="none"))
    assert a.signals_used["occupancy"]["value"] == Decimal("0.85") == b.signals_used["occupancy"]["value"]


def test_unknown_signal_is_rejected(hotel):
    with pytest.raises(SignalInvalid, match="unknown signal"):
        evaluate(hotel, ctx("3000", occupancy="0.5", ocupancy="0.9"))


def test_public_signal_cannot_be_sent_in_price_requests(hotel):
    with pytest.raises(SignalInvalid, match="festival is a public signal"):
        evaluate(hotel, ctx("3000", override=False, occupancy="0.5", festival="diwali"))


def test_public_signals_are_computed_in_pack_timezone(hotel):
    r = evaluate(hotel, ctx("3000", as_of=DIWALI_EVENING_IST, override=False, occupancy="0.5"))
    assert r.signals_used["is_weekend"] == {"value": True, "source": "public"}
    assert r.signals_used["festival"] == {"value": "diwali", "source": "public"}


def test_timezone_decides_the_day():
    # 2026-10-16 (Friday) 20:00 UTC is already Saturday 01:30 in Kolkata
    pack = compile_pack(make_pack(
        [{"name": "g", "stacking": "multiply",
          "rules": [{"id": "wk", "when": "is_weekend", "then": {"op": "pct", "value": 10}, "reason": "w"}]}],
        signals={"is_weekend": {"type": "bool", "source": "public"}}))
    friday_utc = datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)
    assert evaluate(pack, ctx(as_of=friday_utc, override=False)).price == Decimal("1100.00")


def test_missing_base_price():
    pack = compile_pack(make_pack([{"name": "g", "stacking": "multiply", "rules": [
        {"id": "r", "when": "true", "then": {"op": "pct", "value": 1}, "reason": "r"}]}]))
    with pytest.raises(SignalMissing, match="base_price"):
        evaluate(pack, ctx(base=None))
    with pytest.raises(SignalInvalid, match="greater than 0"):
        evaluate(pack, ctx(base="0"))


def test_missing_required_signal():
    pack = compile_pack(make_pack(
        [{"name": "g", "stacking": "multiply",
          "rules": [{"id": "r", "when": "x > 1", "then": {"op": "pct", "value": 1}, "reason": "r"}]}],
        signals={"x": {"type": "number", "source": "request"}}))
    with pytest.raises(SignalMissing):
        evaluate(pack, ctx())


# ---------------------------------------------------------------- redis signals and staleness

def redis_pack(on_stale, default=900):
    sig = {"type": "money", "source": "redis", "max_staleness": "1h", "on_stale": on_stale}
    if default is not None:
        sig["default"] = default
    return compile_pack(make_pack(
        [{"name": "g", "stacking": "first_match",
          "rules": [{"id": "match", "when": "comp > 0", "then": {"op": "formula", "expr": "comp"},
                     "reason": "Match competitor {comp}"}]}],
        signals={"comp": sig}))


def at(minutes_old):
    return {"comp": ("1200", WEDNESDAY - timedelta(minutes=minutes_old))}


def test_fresh_redis_signal_is_used():
    r = evaluate(redis_pack("use_default"), Context(base_price=Decimal("1000"), as_of=WEDNESDAY,
                                                     redis_signals=at(10)))
    assert r.price == Decimal("1200.00")
    assert r.signals_used["comp"]["source"] == "redis"


def test_stale_use_default():
    r = evaluate(redis_pack("use_default"), Context(base_price=Decimal("1000"), as_of=WEDNESDAY,
                                                     redis_signals=at(90)))
    assert r.price == Decimal("900.00")
    assert r.signals_used["comp"]["source"] == "default"
    assert any(f.startswith("comp stale") for f in r.flags)


def test_stale_use_default_without_default_is_stale_error():
    with pytest.raises(SignalStale):
        evaluate(redis_pack("use_default", default=None),
                 Context(base_price=Decimal("1000"), as_of=WEDNESDAY, redis_signals=at(90)))


def test_stale_use_last_known():
    r = evaluate(redis_pack("use_last_known"), Context(base_price=Decimal("1000"), as_of=WEDNESDAY,
                                                        redis_signals=at(90)))
    assert r.price == Decimal("1200.00")
    assert any("used last known value" in f for f in r.flags)


def test_stale_reject():
    with pytest.raises(SignalStale):
        evaluate(redis_pack("reject"), Context(base_price=Decimal("1000"), as_of=WEDNESDAY,
                                               redis_signals=at(90)))


def test_redis_signal_cannot_be_forged_in_request():
    with pytest.raises(SignalInvalid, match="redis signal"):
        evaluate(redis_pack("use_default"), ctx(override=False, comp="1"))


def test_naive_as_of_is_rejected(hotel):
    with pytest.raises(SignalInvalid, match="timezone"):
        evaluate(hotel, ctx("3000", as_of=datetime(2026, 10, 14, 9, 0), occupancy="0.5"))


# ---------------------------------------------------------------- review fix: huge numbers are a clean 422

@pytest.mark.parametrize("base", ["1e30", "1000000000001"])
def test_huge_base_price_is_invalid_input_not_a_crash(hotel, base):
    with pytest.raises(SignalInvalid, match="base_price is too large"):
        evaluate(hotel, ctx(base, occupancy="0.5", is_weekend=False, festival="none"))


def test_huge_numeric_signal_is_invalid_input():
    pack = compile_pack(make_pack([{"name": "g", "stacking": "multiply", "rules": [
        {"id": "r", "when": "x > 1", "then": {"op": "pct", "value": 1}, "reason": "r"}]}]))
    with pytest.raises(SignalInvalid, match="x is too large"):
        evaluate(pack, ctx(x="1e30"))


def test_largest_allowed_base_price_works(hotel):
    p = compile_pack({**load_pack("hotel.yaml"),
                      "guards": {"floor": {"op": "abs", "value": 1}, "ceiling": {"op": "abs", "value": 10**12}}})
    assert evaluate(p, ctx("1000000000000", occupancy="0.5", is_weekend=False, festival="none")).price \
        == Decimal("1000000000000.00")


# ---------------------------------------------------------------- review S2, R2-2: bad context inputs are 422s

@pytest.mark.parametrize("policy, path", [
    (Policy(max_price=Decimal("NaN")), "policy.max_price"),
    (Policy(min_price=Decimal("-5")), "policy.min_price"),
    (Policy(max_surge_x=Decimal("1e40")), "policy.max_surge_x"),
])
def test_bad_tenant_policy_is_invalid_input_not_a_pack_error(hotel, policy, path):
    with pytest.raises(SignalInvalid) as e:
        evaluate(hotel, ctx("3000", occupancy="0.5"), policy=policy)
    assert e.value.details[0]["path"] == path


@pytest.mark.parametrize("ref", ["1e40", "0", "-1", "NaN"])
def test_bad_reference_price_is_invalid_input(hotel, ref):
    with pytest.raises(SignalInvalid, match="reference_price"):
        evaluate(hotel, Context(base_price=Decimal("3000"), signals={"occupancy": "0.5"}, as_of=WEDNESDAY,
                                reference_price=Decimal(ref)))


def test_feed_timestamp_without_timezone():
    with pytest.raises(SignalInvalid, match="written_at must be a datetime with a timezone"):
        evaluate(redis_pack("use_default"), Context(base_price=Decimal("1000"), as_of=WEDNESDAY,
                                                    redis_signals={"comp": ("1200", datetime(2026, 10, 14, 8))}))
