"""Rounding to the nearest VALID price — a multiple of `step` that, if `ending` is set, also ends in it (6199,
6249…) — in the direction `mode` promises, never leaving the guard interval. Never uses built-in round()
(ENGINE_SPEC §7).

Exact, in whole cents: valid prices are x ≡ 0 (mod step) and x ≡ ending (mod 100). By the Chinese remainder
theorem they exist iff ending ≡ 0 (mod gcd(step, 100)) — the compiler checks this — and then repeat every
lcm(step, 100). So `step: 30, ending: 90` gives …, 90, 390, 690, … and `step: 500, ending: 0` every multiple of 500."""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from math import gcd

from .models import Rounding

SKIPPED = "rounding skipped: bounds tighter than step"
_CENTS_PER_100 = 10_000


def grid(rounding: Rounding) -> tuple[int, int] | None:
    """(first valid price, spacing) in cents, or None when the ending can never be reached with this step."""
    step = int(rounding.step * 100)
    if rounding.ending is None:
        return 0, step
    ending = rounding.ending * 100
    g = gcd(step, _CENTS_PER_100)
    if ending % g:
        return None
    m = _CENTS_PER_100 // g                          # step * m = lcm(step, 100 units)
    k = (ending // g) * pow(step // g % m, -1, m) % m if m > 1 else 0
    return step * k % (step * m), step * m


def _at_most(x: int, g: tuple[int, int]) -> int:
    first, spacing = g
    return first + (x - first) // spacing * spacing


def _at_least(x: int, g: tuple[int, int]) -> int:
    below = _at_most(x, g)
    return below if below == x else below + g[1]


def round_price(price: Decimal, rounding: Rounding, lower: Decimal, upper: Decimal) -> tuple[Decimal, str | None]:
    """Returns (rounded price, flag or None)."""
    g = rounding.grid or grid(rounding)              # compile_pack stores it; computed here only for bare Roundings
    if g is None:                                    # unreachable: compile_pack refuses such a pack
        return price, SKIPPED
    p = price * 100                                  # exact; whole cents in the engine, but never assume it
    down = _at_most(int(p.to_integral_value(ROUND_FLOOR)), g)
    up = _at_least(int(p.to_integral_value(ROUND_CEILING)), g)
    if rounding.mode == "down":
        r = down
    elif rounding.mode == "up":
        r = up
    else:                                            # half_up: nearest, a tie goes up
        r = down if p - down < up - p else up
    lo = int((lower * 100).to_integral_value(ROUND_CEILING))
    hi = int((upper * 100).to_integral_value(ROUND_FLOOR))
    if r > hi:
        r = _at_most(hi, g)
    elif r < lo:
        r = _at_least(lo, g)
    if not lo <= r <= hi:
        return price, SKIPPED
    flag = None
    if rounding.mode == "down" and r > p:
        flag = f"rounding went up: no valid price at or below {price} is inside the guard interval"
    elif rounding.mode == "up" and r < p:
        flag = f"rounding went down: no valid price at or above {price} is inside the guard interval"
    return Decimal(r) / 100, flag
