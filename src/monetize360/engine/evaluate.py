"""Pipeline (ENGINE_SPEC): base -> adjust groups -> guards -> round -> self-check (base + sum(deltas) == final,
else raise)."""
from __future__ import annotations

from decimal import localcontext

from .guards import clamp, interval
from .models import DEC, CompiledPack, Context, InvariantViolation, Policy, Result, Row, SignalInvalid, q
from .operators import base_price
from .rounding import round_price
from .signals import resolve
from .stacking import apply_group


def evaluate(pack: CompiledPack, ctx: Context, policy: Policy | None = None,
             disabled_rules: frozenset[str] = frozenset()) -> Result:
    if ctx.as_of.tzinfo is None:
        raise SignalInvalid("as_of must include a timezone", [{"path": "as_of"}])
    with localcontext(DEC):
        values, used, flags = resolve(pack, ctx)

        raw_base, why = base_price(pack.base, ctx, values)
        base = q(raw_base)
        rows = [Row("base", "base", base, base, why)]
        price = base

        for group in pack.groups:
            group_rows, price = apply_group(group, price, values, ctx.as_of, disabled_rules)
            rows += group_rows

        iv = interval(pack.guards, base, policy, ctx.reference_price)
        flags += iv.flags
        clamped = clamp(price, iv)
        if clamped is not None:
            rows.append(clamped)
            price = clamped.after

        rounded, flag = round_price(price, pack.rounding, iv.lower, iv.upper)
        if flag:
            flags.append(flag)
        if rounded != price:
            d = q(rounded - price)
            price = price + d
            r = pack.rounding
            ending = f", ending {r.ending:02d}" if r.ending is not None else ""
            rows.append(Row("rounding", "rounding", d, price, f"Rounded to step {r.step} ({r.mode}{ending})"))

        result = Result(price=price, currency=pack.currency, waterfall=tuple(rows), signals_used=used,
                        bounds={"lower": iv.lower, "upper": iv.upper}, flags=tuple(flags))
        self_check(result)
        return result


def self_check(result: Result) -> None:
    """ENGINE_SPEC §8. A failure is a bug in the engine — no price is returned."""
    rows = result.waterfall

    def fail(why: str) -> None:
        raise InvariantViolation(f"self-check failed: {why}")

    if not rows or rows[0].kind != "base" or rows[0].after != rows[0].delta:
        fail("waterfall must start with the base row")
    for prev, row in zip(rows, rows[1:], strict=False):
        if row.after != prev.after + row.delta:
            fail(f"row {row.step}: {prev.after} + {row.delta} != {row.after}")
    for row in rows:
        if row.delta.as_tuple().exponent != -2:
            fail(f"row {row.step}: delta {row.delta} doesn't have exactly 2 decimals")
    if result.price != rows[-1].after or result.price != sum((r.delta for r in rows[1:]), rows[0].delta):
        fail("price != base + sum of deltas")
    if not result.bounds["lower"] <= result.price <= result.bounds["upper"]:
        fail(f"price {result.price} outside [{result.bounds['lower']}, {result.bounds['upper']}]")
