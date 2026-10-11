"""Fuzz verifier (control/verifier.py): every template is Verified Safe; every kind of broken pack is caught and
shrunk to its smallest failing input; the bounds check doesn't just trust the engine's own self-check."""
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from monetize360.control.verifier import verify
from monetize360.engine import Policy, Result, Row
from tests.conftest import load_pack

TEMPLATES = sorted(p.name for p in (Path(__file__).resolve().parents[2] / "packs/templates").glob("*.yaml"))
CASES = 1_500                 # the API default is 10,000; enough here to find every planted bug


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_template_is_verified_safe(name):
    result = verify(load_pack(name), cases=CASES)
    assert result["ok"] and result["badge"] == "verified_safe", result["violations"][:1]
    assert result["cases"] == CASES and result["violations"] == []


def test_same_pack_same_result():
    a, b = verify(load_pack("ride.yaml"), cases=300), verify(load_pack("ride.yaml"), cases=300)
    assert {k: v for k, v in a.items() if k != "seconds"} == {k: v for k, v in b.items() if k != "seconds"}


# ---------------------------------------------------------------- planted bugs: found, counted, shrunk

def test_band_gap_is_found():
    pack = load_pack("event_tickets.yaml")
    pack["groups"][0]["rules"][0]["then"]["bands"] = pack["groups"][0]["rules"][0]["then"]["bands"][:2]
    result = verify(pack, cases=CASES)
    assert not result["ok"] and result["badge"] is None and "lookup_miss" in result["violation_counts"]
    assert Decimal(result["smallest"]["input"]["signals"]["capacity_sold"]) > Decimal("0.8")


def test_overlapping_exclusive_rules_are_found_and_shrunk_to_the_edge():
    pack = load_pack("retail.yaml")
    pack["groups"][0]["rules"][1]["when"] = "inventory < 600"         # now overlaps "inventory > 500"
    result = verify(pack, cases=CASES)
    assert result["smallest"]["kind"] == "exclusive_conflict"
    assert result["smallest"]["input"]["signals"]["inventory"] == 501


def test_formula_division_by_zero_is_found():
    pack = load_pack("ride.yaml")
    pack["groups"][0]["rules"][0]["then"]["expr"] = "price / (demand_ratio - 1)"   # demand_ratio defaults to 1
    result = verify(pack, cases=CASES)
    assert "rule_error" in result["violation_counts"]


def test_base_price_above_what_the_pack_can_price():
    result = verify(load_pack("hotel.yaml"), cases=CASES, base_price_range=(Decimal(1), Decimal(8000)))
    assert result["smallest"]["kind"] == "bounds_conflict"
    assert result["smallest"]["input"]["base_price"] == "7142.87"     # first paisa above 5000 / 0.70


def test_tenant_policy_is_checked_too():
    result = verify(load_pack("hotel.yaml"), cases=300, policy=Policy(max_surge_x=Decimal("0.5")))
    assert "bounds_conflict" in result["violation_counts"]


def test_violations_list_is_capped_but_counts_are_complete():
    pack = load_pack("event_tickets.yaml")
    pack["groups"][0]["rules"][0]["then"]["bands"] = pack["groups"][0]["rules"][0]["then"]["bands"][:1]
    result = verify(pack, cases=CASES)
    assert len(result["violations"]) == 10 and result["violation_counts"]["lookup_miss"] > 10


# ---------------------------------------------------------------- independent of the engine's own checks

def test_bounds_are_checked_independently_of_the_engine(monkeypatch):
    verifier = sys.modules["monetize360.control.verifier"]
    real = verifier.evaluate

    def no_ceiling(pack, ctx, policy=None, disabled_rules=frozenset()):
        r = real(pack, ctx, policy, disabled_rules)              # pretend the engine forgot the ceiling ...
        price = r.price + Decimal("6000.00")                      # ... and its self-check didn't notice
        rows = r.waterfall + (Row("bug", "rule", Decimal("6000.00"), price, "bug"),)
        return Result(price=price, currency=r.currency, waterfall=rows, signals_used=r.signals_used,
                      bounds={"lower": Decimal(0), "upper": Decimal("1e9")}, flags=r.flags)

    monkeypatch.setattr(verifier, "evaluate", no_ceiling)
    result = verify(load_pack("hotel.yaml"), cases=200)
    assert not result["ok"] and "bounds" in result["violation_counts"]


def test_progress_is_reported():
    seen = []
    verify(load_pack("hotel.yaml"), cases=1_000, progress=lambda done, total: seen.append((done, total)))
    assert seen[-1] == (1_000, 1_000) and len(seen) > 2
