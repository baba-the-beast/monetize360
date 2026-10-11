"""The max_change anchor rule and the idempotency request hash (pure parts; the database parts are in
tests/integration/test_guard_and_idempotency.py)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from monetize360.store.idempotency import request_hash
from monetize360.store.pricestate import Anchor, anchor_for

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
STATE = {"anchor_price": Decimal("3000.00"), "anchor_time": T0,
         "last_price": Decimal("3450.00"), "last_time": T0 + timedelta(minutes=50)}


def test_first_price_has_nothing_to_compare_with_and_becomes_the_anchor():
    assert anchor_for(None, T0, HOUR) == Anchor(reference=None, price=None, time=T0)


def test_inside_the_window_the_anchor_stays_however_many_prices_were_issued():
    for minutes in (0, 1, 30, 59, 60):                        # exactly one hour is still inside
        a = anchor_for(STATE, T0 + timedelta(minutes=minutes), HOUR)
        assert a == Anchor(Decimal("3000.00"), Decimal("3000.00"), T0)


def test_after_the_window_the_last_price_becomes_the_new_anchor():
    later = T0 + timedelta(hours=1, seconds=1)
    assert anchor_for(STATE, later, HOUR) == Anchor(Decimal("3450.00"), Decimal("3450.00"), later)


def test_a_backdated_request_uses_the_current_anchor():
    assert anchor_for(STATE, T0 - timedelta(days=1), HOUR).reference == Decimal("3000.00")


def test_a_pack_without_max_change_has_no_reference():
    assert anchor_for(STATE, T0, None) == Anchor(None, None, T0)


def test_request_hash_is_about_the_content_not_the_key_order():
    a = request_hash("hotel", {"entity": "r1", "base_price": "3000", "signals": {"occupancy": "0.85", "n": 1}})
    b = request_hash("hotel", {"signals": {"n": 1, "occupancy": "0.85"}, "base_price": "3000", "entity": "r1"})
    assert a == b and len(a) == 32
    assert request_hash("airline", {"entity": "r1", "base_price": "3000"}) != request_hash(
        "hotel", {"entity": "r1", "base_price": "3000"})
    assert request_hash("hotel", {"entity": "r1", "base_price": "3001"}) != request_hash(
        "hotel", {"entity": "r1", "base_price": "3000"})
