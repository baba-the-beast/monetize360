"""The golden runner itself: expect_error and expect_flags (review T2). Real golden cases stay hand-written."""
import pytest

from tests.conftest import load_pack
from tests.golden.test_golden import test_golden as run_case

HOTEL = load_pack("hotel.yaml")


def test_expect_error():
    run_case(HOTEL, {"name": "occupancy 7", "inputs": {"base_price": "3000", "occupancy": 7},
                     "expect_error": "signal_invalid"})


def test_expect_error_fails_when_a_price_comes_back():
    with pytest.raises(pytest.fail.Exception):
        run_case(HOTEL, {"name": "x", "inputs": {"base_price": "3000", "occupancy": "0.5"},
                         "expect_error": "signal_invalid"})


def test_expect_flags():
    run_case(HOTEL, {"name": "missing occupancy", "inputs": {"base_price": "3000", "is_weekend": False,
                                                              "festival": "none"},
                     "expect_price": "3000.00",
                     "expect_waterfall": [{"step": "base", "delta": "3000.00", "after": "3000.00"}],
                     "expect_flags": ["occupancy missing, used default 0.5"]})
