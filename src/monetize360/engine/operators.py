"""Operators: pct, abs, multiplier, set, lookup_table, band, piecewise_curve, tier, formula (adjust) and
from_request, set, band, lookup_table, cost_plus, competitor_rel (base). Decimal only (ENGINE_SPEC §4-5)."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .expr import ExprError
from .models import (
    MAX_ABS,
    Adjustment,
    Context,
    InvariantViolation,
    LookupMiss,
    RuleError,
    SignalInvalid,
    SignalMissing,
)
from .signals import show


def key_of(v: Any) -> str:
    """Lookup-table key for a value: bools as true/false, numbers without trailing zeros, strings as-is."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, Decimal, float)):
        d = Decimal(str(v))
        return str(int(d)) if d == d.to_integral_value() else format(d.normalize(), "f")
    return str(v)


def lookup(adj: Adjustment, values: dict) -> Decimal:
    if adj.signal is not None:
        key: Any = key_of(values[adj.signal])
        label = f"{adj.signal} = {key}"
    else:
        key = tuple(key_of(values[s]) for s in adj.signals)
        label = ", ".join(f"{s} = {k}" for s, k in zip(adj.signals, key, strict=True))
    found = adj.table.get(key)
    if found is not None:
        return found
    if adj.default is not None:
        return adj.default
    raise LookupMiss(f"no table entry for {label} and no default", [{"path": "signals", "value": label}])


def band(adj: Adjustment, values: dict) -> Decimal:
    x = values[adj.signal]
    for upto, value in adj.bands:
        if x <= upto:
            return value
    raise LookupMiss(f"{adj.signal} = {show(x)} is above the last band",
                     [{"path": f"signals.{adj.signal}", "value": x}])


def curve(adj: Adjustment, values: dict) -> Decimal:
    x = Decimal(values[adj.signal])
    points = adj.points
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (xa, fa), (xb, fb) in zip(points, points[1:], strict=False):
        if xa <= x <= xb:
            return fa + (x - xa) * (fb - fa) / (xb - xa)
    raise InvariantViolation(f"curve for {adj.signal} has no segment for {x}")   # unreachable: points sorted


def tier(adj: Adjustment, values: dict) -> Decimal:
    x = values[adj.signal]
    factor = Decimal(1)
    for start, f in adj.tiers:
        if start <= x:
            factor = f
    return factor


def formula(adj: Adjustment, values: dict, price: Decimal) -> Decimal:
    """ExprError propagates: stacking turns it into a RuleError that names the rule."""
    result = adj.expr({**values, "price": price})
    if isinstance(result, bool) or not isinstance(result, (Decimal, int)):
        raise ExprError(f"formula '{adj.expr.source}' returned {result!r}, not a number")
    return Decimal(result)


def effect(adj: Adjustment, price: Decimal, values: dict) -> Decimal:
    """The operator's number: a factor, an amount or a new price, as given by adj.kind."""
    op = adj.op
    if op in ("pct", "multiplier", "abs", "set"):
        return adj.value
    if op == "lookup_table":
        return lookup(adj, values)
    if op == "band":
        return band(adj, values)
    if op == "piecewise_curve":
        return curve(adj, values)
    if op == "tier":
        return tier(adj, values)
    if op == "formula":
        return formula(adj, values, price)
    raise InvariantViolation(f"unknown operator {op}")


def base_price(adj: Adjustment, ctx: Context, values: dict) -> tuple[Decimal, str]:
    """Unquantized base price B and its waterfall reason (ENGINE_SPEC §4)."""
    op = adj.op
    if op == "from_request":
        raw = ctx.base_price
        if raw is None:
            raise SignalMissing("base_price is required", [{"path": "base_price"}])
        try:
            b = Decimal(str(raw))
        except ArithmeticError:
            raise SignalInvalid("base_price must be a number", [{"path": "base_price", "value": raw}]) from None
        if not b.is_finite() or b <= 0:
            raise SignalInvalid("base_price must be greater than 0", [{"path": "base_price", "value": raw}])
        if b > MAX_ABS:
            raise SignalInvalid(f"base_price is too large (limit {MAX_ABS})", [{"path": "base_price", "value": raw}])
        return b, "Base price"
    if op == "set":
        return adj.value, "Fixed base price"
    if op == "band":
        return band(adj, values), f"Base price for {adj.signal} = {show(values[adj.signal])}"
    if op == "lookup_table":
        names = (adj.signal,) if adj.signal else adj.signals
        return lookup(adj, values), "Base price for " + ", ".join(f"{s} = {show(values[s])}" for s in names)
    if op == "cost_plus":
        cost = Decimal(values[adj.signal])
        return cost * (1 + adj.value / 100), f"Cost {show(cost)} + {show(adj.value)}% margin"
    if op == "competitor_rel":
        comp = Decimal(values[adj.signal])
        return comp * (1 + adj.value / 100), f"Competitor price {show(comp)} {adj.value:+}%"
    if op == "formula":
        try:
            b = adj.expr(values)
        except ExprError as e:
            raise RuleError(f"base: {e}", [{"path": "base.expr"}]) from None
        if isinstance(b, bool) or not isinstance(b, (Decimal, int)) or b <= 0:
            raise RuleError(f"base formula '{adj.expr.source}' gave {b!r}; a base price must be greater than 0",
                            [{"path": "base.expr"}])
        if b > MAX_ABS:
            raise RuleError(f"base formula '{adj.expr.source}' gave {b}, above the limit {MAX_ABS}",
                            [{"path": "base.expr"}])
        return Decimal(b), f"Base price: {adj.expr.source}"
    raise InvariantViolation(f"unknown base operator {op}")
