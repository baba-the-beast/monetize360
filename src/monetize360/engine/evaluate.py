"""Pipeline (ENGINE_SPEC): base -> adjust groups -> guards -> round -> self-check (base + sum(deltas) == final,
else raise)."""
from __future__ import annotations

from decimal import Decimal, DecimalException, localcontext

from .guards import clamp, interval
from .models import (
    DEC,
    MAX_ABS,
    CompiledPack,
    Context,
    InvariantViolation,
    Policy,
    Result,
    Row,
    RuleError,
    SignalInvalid,
    q,
)
from .operators import base_price
from .rounding import round_price
from .signals import resolve
from .stacking import apply_group


def evaluate(pack: CompiledPack, ctx: Context, policy: Policy | None = None,
             disabled_rules: frozenset[str] = frozenset()) -> Result:
    if ctx.as_of.tzinfo is None:
        raise SignalInvalid("as_of must include a timezone", [{"path": "as_of"}])
    _check_amount(ctx.reference_price, "reference_price")
    if policy is not None:
        _check_amount(policy.min_price, "policy.min_price")
        _check_amount(policy.max_price, "policy.max_price")
        _check_amount(policy.max_surge_x, "policy.max_surge_x", limit=Decimal(1000))
    try:
        return _evaluate(pack, ctx, policy, disabled_rules)
    except DecimalException as e:
        # inputs are capped (MAX_ABS) and rule arithmetic is caught per rule, so only an absurd pack number
        # (e.g. a base band price of 1e30) gets here: a pack problem, never a raw crash
        raise RuleError(f"price arithmetic out of range ({type(e).__name__}): check the pack's numbers") from None


def _check_amount(raw, path: str, limit: Decimal = MAX_ABS) -> None:
    """A tenant limit or reference price must be a finite number > 0 and <= limit. A bad one is the CALLER's
    input (422 naming it), never 'check the pack's numbers' (review S2)."""
    if raw is None:
        return
    try:
        v = Decimal(str(raw))
    except ArithmeticError:
        v = None
    if v is None or not v.is_finite() or v <= 0 or v > limit:
        raise SignalInvalid(f"{path} must be a number greater than 0 and at most {limit}",
                            [{"path": path, "value": str(raw)}])


def _evaluate(pack: CompiledPack, ctx: Context, policy: Policy | None, disabled_rules: frozenset[str]) -> Result:
    with localcontext(DEC):
        values, used, flags = resolve(pack, ctx)

        raw_base, why = base_price(pack.base, ctx, values)
        base = q(raw_base)
        if base <= 0:
            raise _base_error(pack, raw_base)
        values = {**values, "base": base}               # rules and formulas may use `base` (review D4)
        rows = [Row("base", "base", base, base, why)]
        price = base

        for group in pack.groups:
            group_rows, price = apply_group(group, price, values, ctx.as_of, disabled_rules)
            rows += group_rows

        iv = interval(pack.guards, base, policy, ctx.reference_price, values)
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


def _base_error(pack: CompiledPack, raw_base) -> Exception:
    """Never let a base of 0.00 (e.g. 0.00102 per second, or a competitor price of 0) reach the floor clamp and come
    back as a confident wrong price (review D2). Blame whoever supplied it."""
    why = ("is below the smallest unit 0.01 — price per 1,000 units instead" if raw_base > 0
           else "must be greater than 0")
    op = pack.base.op
    if op == "from_request":
        return SignalInvalid(f"base price {raw_base} {why}", [{"path": "base_price", "value": str(raw_base)}])
    if op in ("cost_plus", "competitor_rel"):                      # computed from a caller's signal
        return SignalInvalid(f"base price from {op} ({raw_base}) {why}", [{"path": f"signals.{pack.base.signal}"}])
    return RuleError(f"base price from {op} ({raw_base}) {why}", [{"path": "base"}])   # pack data or formula


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
