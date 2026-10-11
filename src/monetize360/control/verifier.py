"""Gate 3 — fuzz verification with Hypothesis: generate N random but VALID contexts from the pack's own signal
specs (ranges, enum values, table keys, calendar days, stale feeds) and price each one. A case is a violation
when a valid input gets no price (lookup_miss, exclusive_conflict, rule_error, bounds_conflict, engine bug), or
when the price breaks a guarantee checked INDEPENDENTLY of the engine's own self-check: floor <= price <= ceiling
(incl. tenant limits), max_change, the waterfall adds up, same input -> same output. Deterministic: the same pack
gets the same cases. On a violation, Hypothesis shrinks it to the smallest input that still fails."""
from __future__ import annotations

import json
import random
import time
import zlib
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Iterator, Mapping

from hypothesis import HealthCheck, Phase, Verbosity, given, settings
from hypothesis import strategies as st

from monetize360.engine import (
    Context,
    EngineError,
    Policy,
    SignalInvalid,
    SignalMissing,
    SignalStale,
    compile_pack,
    evaluate,
)

CENT = Decimal("0.01")
MAX_VIOLATIONS_SHOWN = 10
HYPOTHESIS_CASES = 1_000     # Hypothesis hunts edges and nasty values but costs ~2 ms a case; the rest of the
                             # cases are a seeded random sweep over the same specs (~0.2 ms a case)
PROPERTIES = [
    "every valid input gets a price",
    "floor <= price <= ceiling (pack and tenant limits)",
    "price moves at most max_change from the reference",
    "waterfall adds up to the price",
    "same input -> same output",
]


def q(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


class _Violation(Exception):
    def __init__(self, kind: str, message: str, ctx: Context):
        super().__init__(message)
        self.kind, self.message, self.ctx = kind, message, ctx


def verify(pack: Mapping[str, Any], *, cases: int = 10_000, policy: Policy | None = None,
           base_price_range: tuple[Decimal, Decimal] | None = None,
           progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    """Fuzz a (schema-valid) pack dict. Returns {ok, cases, violations, violation_counts, rejected_inputs,
    smallest, badge, properties, base_price_range, seconds}. `progress(done, total)` is called as cases run."""
    started = time.perf_counter()
    compiled = compile_pack(pack)
    lo, hi = base_price_range or _default_base_range(compiled, policy)
    contexts = _contexts(compiled, lo, hi)

    seen, kinds, rejected, run = [], Counter(), Counter(), 0

    def check(ctx: Context) -> None:
        nonlocal run
        run += 1
        if progress is not None and run % 250 == 0:
            progress(run, cases)
        try:
            _case(compiled, ctx, policy)
        except _Violation as v:
            kinds[v.kind] += 1
            if len(seen) < MAX_VIOLATIONS_SHOWN:
                seen.append({"kind": v.kind, "message": v.message, "input": _show(ctx)})
        except (SignalStale, SignalInvalid, SignalMissing) as e:
            rejected[e.code] += 1           # the pack's own declared policy (e.g. on_stale: reject), not a bug

    explore = settings(max_examples=min(cases, HYPOTHESIS_CASES), database=None, derandomize=True, deadline=None,
                       phases=[Phase.generate], verbosity=Verbosity.quiet,
                       suppress_health_check=list(HealthCheck))(given(contexts)(check))
    explore()
    seed = zlib.crc32(json.dumps(pack, sort_keys=True, default=str).encode())      # same pack -> same cases
    for ctx in _sweep(compiled, lo, hi, cases - run, random.Random(seed)):          # noqa: S311 - not crypto
        check(ctx)
    if progress is not None:
        progress(run, cases)

    smallest = _shrink(compiled, contexts, policy, cases) if seen else None
    return {
        "ok": not seen,
        "cases": run,
        "violations": seen,
        "violation_counts": dict(kinds),
        "rejected_inputs": dict(rejected),
        "smallest": smallest,
        "badge": "verified_safe" if not seen else None,
        "properties": PROPERTIES,
        "base_price_range": [str(lo), str(hi)] if compiled.base.op == "from_request" else None,
        "seconds": round(time.perf_counter() - started, 2),
    }


# ---------------------------------------------------------------- one case: price it, then check it independently

def _case(compiled, ctx: Context, policy: Policy | None) -> None:
    try:
        r = evaluate(compiled, ctx, policy)
    except (SignalStale, SignalInvalid, SignalMissing):
        raise
    except EngineError as e:
        raise _Violation(e.code, e.message, ctx) from None

    rows = r.waterfall
    if r.price != sum((row.delta for row in rows[1:]), rows[0].delta) or r.price != rows[-1].after:
        raise _Violation("waterfall", f"waterfall doesn't add up to {r.price}", ctx)

    base = rows[0].after
    floor = max(_limit(compiled.guards.floor, base, ctx, r), CENT)
    ceiling = _limit(compiled.guards.ceiling, base, ctx, r)
    if policy is not None:
        if policy.min_price is not None:
            floor = max(floor, q(Decimal(policy.min_price)))
        if policy.max_price is not None:
            ceiling = min(ceiling, q(Decimal(policy.max_price)))
        if policy.max_surge_x is not None:
            ceiling = min(ceiling, q(base * Decimal(policy.max_surge_x)))
    if not floor <= r.price <= ceiling:
        raise _Violation("bounds", f"price {r.price} is outside floor {floor} .. ceiling {ceiling}", ctx)

    pct = compiled.guards.max_change_pct
    if pct is not None and ctx.reference_price is not None:
        ref = Decimal(ctx.reference_price)
        soft_lo, soft_hi = q(ref * (1 - pct / 100)), q(ref * (1 + pct / 100))
        reachable = max(soft_lo, floor) <= min(soft_hi, ceiling)
        if reachable and not soft_lo <= r.price <= soft_hi:
            raise _Violation("max_change", f"price {r.price} moved more than {pct}% from reference {ref}", ctx)

    again = evaluate(compiled, ctx, policy)
    if again != r:
        raise _Violation("nondeterministic", "the same input gave a different result", ctx)


def _limit(limit, base: Decimal, ctx: Context, r) -> Decimal:
    if limit.op == "abs":
        return q(limit.value)
    if limit.op == "pct_of_base":
        return q(base * limit.value / 100)
    return q(Decimal(str(r.signals_used[limit.signal]["value"])) * limit.pct / 100)


# ---------------------------------------------------------------- inputs: generated from the pack's own specs

def _default_base_range(compiled, policy: Policy | None) -> tuple[Decimal, Decimal]:
    """Base prices a caller could plausibly send: up to the pack's fixed ceiling (nothing above it can be sold)."""
    hi = compiled.guards.ceiling.value if compiled.guards.ceiling.op == "abs" else Decimal(100_000)
    if policy is not None and policy.max_price is not None:
        hi = min(hi, Decimal(policy.max_price))
    return Decimal(1), hi


def _value(spec, table_keys: list[str]):
    if spec.type in ("ratio", "number", "money"):
        lo, hi = spec.range or (Decimal(0), Decimal(100_000))
        places = 4 if spec.type == "ratio" else 2
        return st.one_of(st.sampled_from([lo, hi]),
                         st.decimals(min_value=lo, max_value=hi, places=places, allow_nan=False, allow_infinity=False))
    if spec.type == "int":
        lo, hi = spec.range or (Decimal(0), Decimal(1_000))
        return st.integers(min_value=int(lo), max_value=int(hi))
    if spec.type == "bool":
        return st.booleans()
    if spec.type == "enum":
        return st.sampled_from(spec.values)
    free_text = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-", min_size=1, max_size=12)
    return st.one_of(st.sampled_from(table_keys), free_text) if table_keys else free_text      # text


def _text_keys(compiled) -> dict[str, list[str]]:
    keys: dict[str, set[str]] = {}
    for adj in [compiled.base] + [r.then for g in compiled.groups for r in g.rules]:
        if adj.op == "lookup_table" and adj.signal:
            keys.setdefault(adj.signal, set()).update(adj.table)
    return {k: sorted(v) for k, v in keys.items()}


def _contexts(compiled, base_lo: Decimal, base_hi: Decimal):
    utc = timezone.utc
    any_time = st.datetimes(min_value=datetime(2025, 1, 1), max_value=datetime(2027, 12, 31, 23, 59),
                            timezones=st.just(utc))
    calendar_days = sorted({d for s in compiled.signals.values() if s.calendar for d in s.calendar})
    on_calendar_day = st.tuples(st.sampled_from(calendar_days), st.integers(0, 23)).map(
        lambda dh: datetime(dh[0].year, dh[0].month, dh[0].day, dh[1], tzinfo=compiled.timezone).astimezone(utc))
    as_of_strategy = st.one_of(any_time, on_calendar_day) if calendar_days else any_time
    text_keys = _text_keys(compiled)
    # strategies are built ONCE per pack: building them per case was 70% of the run time
    inputs = [(spec, _value(spec, text_keys.get(spec.name, [])))
              for spec in compiled.signals.values() if spec.source != "public"]   # public: computed from as_of
    one_in_ten = st.integers(0, 9)
    staleness = st.sampled_from([0, 0.5, 0.99, 1.5, 3])
    base_strategy = st.decimals(min_value=base_lo, max_value=base_hi, places=2, allow_nan=False, allow_infinity=False)
    ref_strategy = st.one_of(st.none(), st.decimals(min_value=CENT, max_value=max(base_hi, Decimal(1)) * 3,
                                                    places=2, allow_nan=False, allow_infinity=False))

    @st.composite
    def build(draw):
        as_of = draw(as_of_strategy)
        signals, feeds = {}, {}
        for spec, strategy in inputs:
            if spec.has_default and draw(one_in_ten) == 0:
                continue                                  # sometimes missing: exercises the default
            value = draw(strategy)
            if spec.source == "request":
                signals[spec.name] = value
            else:                                         # redis feed: fresh, or stale on purpose
                limit = spec.max_staleness or timedelta(hours=1)
                age = limit * draw(staleness)
                feeds[spec.name] = (value, as_of - age)
        base = draw(base_strategy) if compiled.base.op == "from_request" else None
        ref = draw(ref_strategy)
        return Context(base_price=base, signals=signals, redis_signals=feeds, as_of=as_of, reference_price=ref)

    return build()


def _sweep(compiled, base_lo: Decimal, base_hi: Decimal, n: int, rng: random.Random) -> Iterator[Context]:
    """Fast seeded cases over the same specs as `_contexts`. Exact: values are whole units of 0.01 / 0.0001."""
    def between(lo: Decimal, hi: Decimal, unit: Decimal) -> Decimal:
        return lo + unit * rng.randint(0, int((hi - lo) / unit))

    def value(spec, keys: list[str]):
        if spec.type in ("ratio", "number", "money"):
            lo, hi = spec.range or (Decimal(0), Decimal(100_000))
            if rng.random() < 0.1:
                return rng.choice([lo, hi])
            return between(lo, hi, Decimal("0.0001") if spec.type == "ratio" else CENT)
        if spec.type == "int":
            lo, hi = spec.range or (Decimal(0), Decimal(1_000))
            return rng.randint(int(lo), int(hi))
        if spec.type == "bool":
            return rng.random() < 0.5
        if spec.type == "enum":
            return rng.choice(spec.values)
        if keys and rng.random() < 0.8:
            return rng.choice(keys)
        return "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-") for _ in range(rng.randint(1, 12)))

    keys = _text_keys(compiled)
    inputs = [s for s in compiled.signals.values() if s.source != "public"]
    days = sorted({d for s in compiled.signals.values() if s.calendar for d in s.calendar})
    start, span = datetime(2025, 1, 1, tzinfo=timezone.utc), 3 * 365 * 24 * 3600
    for _ in range(max(n, 0)):
        if days and rng.random() < 0.2:
            d = rng.choice(days)
            as_of = datetime(d.year, d.month, d.day, rng.randint(0, 23), tzinfo=compiled.timezone).astimezone(
                timezone.utc)
        else:
            as_of = start + timedelta(seconds=rng.randint(0, span))
        signals, feeds = {}, {}
        for spec in inputs:
            if spec.has_default and rng.random() < 0.1:
                continue
            v = value(spec, keys.get(spec.name, []))
            if spec.source == "request":
                signals[spec.name] = v
            else:
                limit = spec.max_staleness or timedelta(hours=1)
                feeds[spec.name] = (v, as_of - limit * rng.choice([0, 0.5, 0.99, 1.5, 3]))
        base = between(base_lo, base_hi, CENT) if compiled.base.op == "from_request" else None
        ref = None if rng.random() < 0.5 else between(CENT, max(base_hi, Decimal(1)) * 3, CENT)
        yield Context(base_price=base, signals=signals, redis_signals=feeds, as_of=as_of, reference_price=ref)


# ---------------------------------------------------------------- smallest failing input

def _shrink(compiled, contexts, policy: Policy | None, cases: int) -> dict[str, Any] | None:
    found: list[_Violation] = []

    def must_pass(ctx: Context) -> None:
        try:
            _case(compiled, ctx, policy)
        except _Violation as v:
            found.append(v)
            raise
        except (SignalStale, SignalInvalid, SignalMissing):
            pass

    falsify = settings(max_examples=cases, database=None, derandomize=True, deadline=None,
                       phases=[Phase.generate, Phase.shrink], verbosity=Verbosity.quiet,
                       suppress_health_check=list(HealthCheck))(given(contexts)(must_pass))
    try:
        falsify()
    except _Violation as v:
        return {"kind": v.kind, "message": v.message, "input": _show(v.ctx)}
    except Exception:            # noqa: BLE001 - shrinking is a nicety; the violations list already stands
        return None
    return None


def _show(ctx: Context) -> dict[str, Any]:
    def s(v: Any) -> Any:
        return str(v) if isinstance(v, Decimal) else v
    out: dict[str, Any] = {"as_of": ctx.as_of.isoformat()}
    if ctx.base_price is not None:
        out["base_price"] = str(ctx.base_price)
    if ctx.signals:
        out["signals"] = {k: s(v) for k, v in ctx.signals.items()}
    if ctx.redis_signals:
        out["redis_signals"] = {k: {"value": s(v), "written_at": w.isoformat()}
                                for k, (v, w) in ctx.redis_signals.items()}
    if ctx.reference_price is not None:
        out["reference_price"] = str(ctx.reference_price)
    return out
