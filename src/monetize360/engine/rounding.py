"""Step rounding with explicit mode (half_up, down, up) and optional price ending (49/99), never leaving the
guard interval. Never uses built-in round() (ENGINE_SPEC §7)."""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from .models import Rounding

_MODES = {"half_up": ROUND_HALF_UP, "down": ROUND_FLOOR, "up": ROUND_CEILING}
_HUNDRED = Decimal(100)
SKIPPED = "rounding skipped: bounds tighter than step"


def _to_step(x: Decimal, step: Decimal, mode: str) -> Decimal:
    return (x / step).quantize(Decimal(1), rounding=mode) * step


def _with_ending(r: Decimal, ending: int) -> Decimal:
    """Nearest of ⌊r/100⌋×100 + ending and that ±100; a tie goes up."""
    c = _to_step(r, _HUNDRED, ROUND_FLOOR) + ending
    best = None
    for cand in (c - _HUNDRED, c, c + _HUNDRED):
        if best is None or abs(cand - r) < abs(best - r) or (abs(cand - r) == abs(best - r) and cand > best):
            best = cand
    return best


def _valid_at_most(x: Decimal, rounding: Rounding) -> Decimal | None:
    if rounding.ending is None:
        return _to_step(x, rounding.step, ROUND_FLOOR)
    c = _to_step(x, _HUNDRED, ROUND_FLOOR) + rounding.ending
    if c > x:
        c -= _HUNDRED
    return c if c % rounding.step == 0 else None


def _valid_at_least(x: Decimal, rounding: Rounding) -> Decimal | None:
    if rounding.ending is None:
        return _to_step(x, rounding.step, ROUND_CEILING)
    c = _to_step(x, _HUNDRED, ROUND_FLOOR) + rounding.ending
    if c < x:
        c += _HUNDRED
    return c if c % rounding.step == 0 else None


def round_price(price: Decimal, rounding: Rounding, lower: Decimal, upper: Decimal) -> tuple[Decimal, str | None]:
    """Returns (rounded price, flag or None)."""
    r = _to_step(price, rounding.step, _MODES[rounding.mode])
    if rounding.ending is not None:
        r = _with_ending(r, rounding.ending)
    if r > upper:
        r = _valid_at_most(upper, rounding)
    elif r < lower:
        r = _valid_at_least(lower, rounding)
    if r is None or not lower <= r <= upper:
        return price, SKIPPED
    return r, None
