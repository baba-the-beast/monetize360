"""Engine data types (frozen dataclasses, cheap on the hot path): Context, Policy, Row, Result, the compiled
pack structures, the error classes, and the Decimal context every calculation runs in (ENGINE_SPEC §1-2)."""
from __future__ import annotations

import decimal
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

DEC = decimal.Context(prec=28, rounding=ROUND_HALF_UP)   # fixed local context — never the global one
CENT = Decimal("0.01")
MAX_ABS = Decimal(10) ** 12      # largest accepted base price / numeric signal: keeps every amount well inside
                                 # the 28-digit context, so quantizing to 0.01 can never fail (ENGINE_SPEC §2)


def q(x: Decimal) -> Decimal:
    """Quantize money to 0.01, half-up."""
    return x.quantize(CENT, rounding=ROUND_HALF_UP, context=DEC)


# ---------------------------------------------------------------- errors

class EngineError(Exception):
    code = "engine_error"

    def __init__(self, message: str, details: list[dict] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


class PackInvalid(EngineError):
    code = "pack_invalid"


class SignalInvalid(EngineError):
    code = "signal_invalid"


class SignalMissing(EngineError):
    code = "signal_missing"


class SignalStale(EngineError):
    code = "signal_stale"


class LookupMiss(EngineError):
    code = "lookup_miss"


class ExclusiveConflict(EngineError):
    code = "exclusive_conflict"


class BoundsConflict(EngineError):
    """The limits for THIS request cross (e.g. base_price 8000 -> floor 70% = 5600 > ceiling 5000). HTTP 422."""
    code = "bounds_conflict"


class RuleError(EngineError):
    """A pack rule fails at runtime (formula divides by zero, arithmetic out of range). Names the rule. HTTP 500."""
    code = "rule_error"


class InvariantViolation(EngineError):
    """A bug in the ENGINE, never a pack or input problem: the self-check failed, or code reached a state the
    compiler rules out (an unknown operator, a curve with no segment). HTTP 500."""
    code = "invariant_violation"


# ---------------------------------------------------------------- inputs and outputs

@dataclass(frozen=True, kw_only=True)
class Policy:
    """Tenant limits (ENGINE_SPEC §1, §6.1). Absent limits are ignored."""
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    max_surge_x: Decimal | None = None


@dataclass(frozen=True, kw_only=True)
class Context:
    """Everything that may influence a price. Nothing else does."""
    as_of: datetime
    base_price: Decimal | None = None
    signals: dict[str, Any] = field(default_factory=dict)
    redis_signals: dict[str, tuple[Any, datetime]] = field(default_factory=dict)
    reference_price: Decimal | None = None
    allow_public_override: bool = False


@dataclass(frozen=True)
class Row:
    step: str
    kind: str          # base | rule | clamp | rounding
    delta: Decimal
    after: Decimal
    reason: str


@dataclass(frozen=True)
class Result:
    price: Decimal
    currency: str
    waterfall: tuple[Row, ...]
    signals_used: dict[str, dict[str, Any]]
    bounds: dict[str, Decimal]
    flags: tuple[str, ...]


# ---------------------------------------------------------------- compiled pack

@dataclass(frozen=True)
class SignalSpec:
    name: str
    type: str                                   # ratio | number | money | int | bool | enum
    source: str                                 # request | redis | public
    range: tuple[Decimal, Decimal] | None = None
    values: tuple[str, ...] = ()
    has_default: bool = False
    default: Any = None
    max_staleness: timedelta | None = None
    on_stale: str = "use_default"
    calendar: dict[date, str] | None = None     # public calendar signals: local date -> value, else "none"


@dataclass(frozen=True)
class Adjustment:
    """One operator. `kind` says how its number is used: factor | amount | price (ENGINE_SPEC §5.1)."""
    op: str
    kind: str
    value: Decimal | None = None                # pct/multiplier (stored as a factor), abs, set, cost/competitor pct
    signal: str | None = None
    signals: tuple[str, ...] = ()
    table: dict[Any, Decimal] | None = None     # key (str or tuple of str) -> value
    default: Decimal | None = None
    bands: tuple[tuple[Decimal, Decimal], ...] = ()     # (upto, value), ascending
    points: tuple[tuple[Decimal, Decimal], ...] = ()    # (x, factor), ascending
    tiers: tuple[tuple[Decimal, Decimal], ...] = ()     # (from, factor), ascending
    expr: Callable[[dict], Any] | None = None


@dataclass(frozen=True)
class Rule:
    id: str
    when: Callable[[dict], Any]
    then: Adjustment
    reason: str
    priority: int = 0
    effective_from: datetime | None = None
    effective_to: datetime | None = None


@dataclass(frozen=True)
class Group:
    name: str
    stacking: str
    rules: tuple[Rule, ...]                     # already in run order: priority desc, then file order


@dataclass(frozen=True)
class Limit:
    op: str                                     # abs | pct_of_base | signal
    value: Decimal | None = None                # abs amount, or pct for pct_of_base
    signal: str | None = None                   # op == signal: the limit is this signal's value ...
    pct: Decimal = Decimal(100)                 # ... times pct/100 (e.g. floor = 105% of cost)


@dataclass(frozen=True)
class Guards:
    floor: Limit
    ceiling: Limit
    max_change_pct: Decimal | None = None
    max_change_per: timedelta | None = None     # used by the API layer to reset the anchor (§6.2)


@dataclass(frozen=True)
class Rounding:
    step: Decimal
    mode: str                                   # half_up | down | up
    ending: int | None = None
    grid: tuple[int, int] | None = None         # (first valid price, spacing) in cents; set by compile_pack


@dataclass(frozen=True)
class CompiledPack:
    domain: str
    currency: str
    timezone: tzinfo
    signals: dict[str, SignalSpec]
    base: Adjustment
    groups: tuple[Group, ...]
    guards: Guards
    rounding: Rounding
    rule_ids: frozenset[str]
