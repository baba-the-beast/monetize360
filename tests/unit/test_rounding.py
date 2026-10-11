"""Half-up tie (4125 -> 4150), endings 49/99, never banker's rounding."""
from decimal import Decimal

import pytest

from monetize360.engine.models import Rounding
from monetize360.engine.rounding import SKIPPED, round_price

WIDE = (Decimal("0.01"), Decimal("1000000"))


def r(price, step, mode="half_up", ending=None, bounds=WIDE):
    return round_price(Decimal(price), Rounding(Decimal(step), mode, ending), *bounds)


@pytest.mark.parametrize("price, step, mode, expected", [
    ("4125", "50", "half_up", "4150"),     # banker's rounding would give 4100
    ("4175", "50", "half_up", "4200"),
    ("4124.99", "50", "half_up", "4100"),
    ("4149", "50", "down", "4100"),
    ("4101", "50", "up", "4150"),
    ("123.455", "0.01", "half_up", "123.46"),
])
def test_step_and_mode(price, step, mode, expected):
    assert r(price, step, mode) == (Decimal(expected), None)


@pytest.mark.parametrize("price, ending, expected", [
    ("6150", 99, "6199"),     # 6099 is 51 away, 6199 is 49 away
    ("6149", 99, "6199"),     # 6099 and 6199 are both 50 away -> a tie goes up (ENGINE_SPEC §7)
    ("25544", 99, "25499"),
    ("6124", 49, "6149"),     # 6149 (25) vs 6049 (75)
])
def test_endings(price, ending, expected):
    assert r(price, "1", ending=ending) == (Decimal(expected), None)


@pytest.mark.parametrize("price, mode, expected", [
    ("6150.40", "down", "6099"),     # review #8: used to give 6199 - "down" must never raise the price
    ("6199.00", "down", "6199"),
    ("6100.00", "up", "6199"),
    ("6199.50", "up", "6299"),       # step-rounded up to 6200 first, then the next ...99 above it
    ("6150.40", "half_up", "6199"),  # nearest (49.60 away vs 51.40)
])
def test_ending_follows_mode(price, mode, expected):
    assert r(price, "1", mode, ending=99) == (Decimal(expected), None)


def test_rounding_never_crosses_the_ceiling():
    assert r("4990", "50", bounds=(Decimal("1000"), Decimal("4980"))) == (Decimal("4950"), None)


def test_rounding_never_crosses_the_floor():
    # "down" would give 1000, below the floor 1005 -> the nearest valid price inside is 1050, and it says so (S4)
    assert r("1010", "50", "down", bounds=(Decimal("1005"), Decimal("5000"))) == (
        Decimal("1050"), "rounding went up: no valid price at or below 1010 is inside the guard interval")


def test_ending_moves_inside_bounds():
    assert r("5000", "1", ending=99, bounds=(Decimal("1000"), Decimal("5000"))) == (Decimal("4999"), None)


def test_no_valid_value_skips_rounding():
    assert r("1010.00", "50", bounds=(Decimal("1001"), Decimal("1020"))) == (Decimal("1010.00"), SKIPPED)


# ---------------------------------------------------------------- review S1: every reachable step/ending works

@pytest.mark.parametrize("price, step, ending, expected", [
    ("4230", "30", 90, "4290"),        # valid: ..., 3990, 4290, 4590 (multiples of 30 ending in 90); nearest
    ("4260", "500", 0, "4500"),        # multiples of 500: 4000 (260 away), 4500 (240 away)
    ("4150", "200", 0, "4200"),        # 4000 / 4200
    ("4500", "1000", 0, "5000"),       # tie between 4000 and 5000 goes up
    ("6098.60", "1", 99, "6099"),      # nearest valid price to the actual price, no double rounding
    ("19.66", "0.5", None, "19.5"),
])
def test_step_and_ending_combinations(price, step, ending, expected):
    got, flag = r(price, step, ending=ending)
    assert got == Decimal(expected) and flag is None
    assert got % Decimal(step) == 0 and (ending is None or int(got) % 100 == ending)


def test_up_mode_forced_down_by_the_ceiling_is_flagged():
    got, flag = r("4990", "50", "up", bounds=(Decimal("1000"), Decimal("4980")))
    assert got == Decimal("4950") and flag.startswith("rounding went down")


def test_compile_pack_stores_the_grid():
    from monetize360.engine import compile_pack
    from tests.conftest import load_pack
    assert compile_pack(load_pack("airline.yaml")).rounding.grid == (9900, 10000)     # step 1, ending 99
    assert compile_pack(load_pack("hotel.yaml")).rounding.grid == (0, 5000)           # step 50
