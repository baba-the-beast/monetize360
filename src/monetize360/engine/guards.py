"""Three guard layers (pack -> tenant policy -> platform policy), strictest wins; one interval, one clamp,
and the clamp row names the binding limit (ENGINE_SPEC §6)."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .models import CENT, Guards, InvariantViolation, Limit, Policy, Row, q


@dataclass
class Interval:
    lower: Decimal
    upper: Decimal
    # (step name, value, reason), hardest first: platform -> tenant -> pack -> max_change
    lower_limits: list[tuple[str, Decimal, str]] = field(default_factory=list)
    upper_limits: list[tuple[str, Decimal, str]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def _limit(limit: Limit, base: Decimal) -> Decimal:
    return q(limit.value if limit.op == "abs" else base * limit.value / 100)


def _describe(name: str, limit: Limit, value: Decimal) -> str:
    if limit.op == "pct_of_base":
        return f"{name} {limit.value}% of base = {value}"
    return f"{name} {value}"


def interval(guards: Guards, base: Decimal, policy: Policy | None,
             reference: Decimal | None) -> Interval:
    floor, ceiling = _limit(guards.floor, base), _limit(guards.ceiling, base)
    lower = [("platform_min", CENT, f"Platform minimum price {CENT}")]
    upper: list[tuple[str, Decimal, str]] = []
    if policy is not None:
        if policy.min_price is not None:
            v = q(Decimal(str(policy.min_price)))
            lower.append(("tenant_min", v, f"Tenant minimum price {v}"))
        if policy.max_price is not None:
            v = q(Decimal(str(policy.max_price)))
            upper.append(("tenant_max", v, f"Tenant maximum price {v}"))
        if policy.max_surge_x is not None:
            x = Decimal(str(policy.max_surge_x))
            upper.append(("tenant_surge", q(base * x), f"Tenant cap {x}x base = {q(base * x)}"))
    lower.append(("floor", floor, _describe("Floor", guards.floor, floor)))
    upper.append(("ceiling", ceiling, _describe("Ceiling", guards.ceiling, ceiling)))

    hard_lo = max(v for _, v, _ in lower)
    hard_hi = min(v for _, v, _ in upper)
    if hard_lo > hard_hi:
        raise InvariantViolation(
            f"limits cross: lower {hard_lo} > upper {hard_hi}",
            [{"path": "guards", "value": {"lower": str(hard_lo), "upper": str(hard_hi)}}])

    result = Interval(hard_lo, hard_hi, lower, upper)
    if guards.max_change_pct is not None and reference is not None:
        ref, pct = Decimal(str(reference)), guards.max_change_pct
        soft_lo, soft_hi = q(ref * (1 - pct / 100)), q(ref * (1 + pct / 100))
        lo, hi = max(soft_lo, hard_lo), min(soft_hi, hard_hi)
        if lo <= hi:
            why = f"Max change {pct}% vs {q(ref)}"
            result.lower, result.upper = lo, hi
            lower.append(("max_change", soft_lo, why))
            upper.append(("max_change", soft_hi, why))
        else:
            result.flags.append("max_change ignored: its range is outside the hard limits")
    return result


def clamp(price: Decimal, iv: Interval) -> Row | None:
    """One clamp row if the price is outside the interval, else None."""
    if price < iv.lower:
        edge, limits = iv.lower, iv.lower_limits
    elif price > iv.upper:
        edge, limits = iv.upper, iv.upper_limits
    else:
        return None
    name, _, why = next(lim for lim in limits if lim[1] == edge)
    return Row(name, "clamp", edge - price, edge, why)
