"""Impact preview (control/impact.py): re-price real past requests with a draft before it ships."""
from datetime import datetime, timezone
from decimal import Decimal

from monetize360.control.impact import impact
from monetize360.engine import Context, compile_pack, evaluate
from tests.conftest import load_pack

WED = datetime(2026, 10, 14, 9, tzinfo=timezone.utc)            # weekday, no festival
SAT = datetime(2026, 10, 17, 9, tzinfo=timezone.utc)            # weekend
DIWALI = datetime(2026, 11, 8, 9, tzinfo=timezone.utc)          # Sunday + Diwali


def records(pack_name, requests):
    """What the audit log would hold: the request plus the price actually charged by the live pack."""
    live = compile_pack(load_pack(pack_name))
    out = []
    for i, (base, occ, as_of) in enumerate(requests):
        ctx = Context(base_price=Decimal(base), signals={"occupancy": occ}, as_of=as_of)
        out.append({"price_id": f"p{i}", "as_of": as_of.isoformat(), "base_price": base,
                    "signals": {"occupancy": occ}, "price": str(evaluate(live, ctx).price)})
    return out


HOTEL_TRAFFIC = [("3000", "0.5", WED), ("3000", "0.9", WED), ("3000", "0.5", SAT), ("3000", "0.9", DIWALI)]


def test_no_change_means_nothing_moves_and_replay_is_exact():
    r = impact(load_pack("hotel.yaml"), records("hotel.yaml", HOTEL_TRAFFIC), live_pack=load_pack("hotel.yaml"))
    assert r["compared"] == 4 and r["unchanged_pct"] == "100.0" and r["replay_mismatches"] == 0
    assert r["biggest_movers"] == []


def test_weekend_uplift_from_10_to_20_percent():
    draft = load_pack("hotel.yaml")
    draft["groups"][0]["rules"][1]["then"]["value"] = 20
    r = impact(draft, records("hotel.yaml", HOTEL_TRAFFIC), live_pack=load_pack("hotel.yaml"))
    # weekdays: unchanged x2 ; Sat: 3000 x 1.1 = 3300 -> 3000 x 1.2 = 3600 (+9.1%)
    # Diwali Sun: 3000 x 1.2 x 1.1 x 1.25 = 4950 -> 3000 x 1.2 x 1.2 x 1.25 = 5400, clamped to 5000 (+1.0%)
    assert (r["up_pct"], r["unchanged_pct"], r["down_pct"]) == ("50.0", "50.0", "0.0")
    assert r["new_ceiling_hits"] == 1
    assert [m["change_pct"] for m in r["biggest_movers"]] == ["9.1", "1.0"]
    assert (r["change_p10"], r["change_p50"], r["change_p90"]) == ("0.0", "0.0", "9.1")


def test_requests_that_would_start_failing_are_counted():
    draft = load_pack("hotel.yaml")
    draft["guards"]["ceiling"]["value"] = 2000                  # floor 70% of 3000 = 2100 > 2000
    r = impact(draft, records("hotel.yaml", HOTEL_TRAFFIC))
    assert r["would_fail"] == 4 and r["failures"] == {"bounds_conflict": 4} and r["compared"] == 0
