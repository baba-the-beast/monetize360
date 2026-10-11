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


def test_rounding_never_crosses_the_ceiling():
    assert r("4990", "50", bounds=(Decimal("1000"), Decimal("4980"))) == (Decimal("4950"), None)


def test_rounding_never_crosses_the_floor():
    assert r("1010", "50", "down", bounds=(Decimal("1005"), Decimal("5000"))) == (Decimal("1050"), None)


def test_ending_moves_inside_bounds():
    assert r("5000", "1", ending=99, bounds=(Decimal("1000"), Decimal("5000"))) == (Decimal("4999"), None)


def test_no_valid_value_skips_rounding():
    assert r("1010.00", "50", bounds=(Decimal("1001"), Decimal("1020"))) == (Decimal("1010.00"), SKIPPED)
