"""Resolve signals (request -> redis snapshot -> public -> default), validate type/range, apply on_stale policy,
record the source of each (ENGINE_SPEC §3)."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import MAX_ABS, CompiledPack, Context, SignalInvalid, SignalMissing, SignalSpec, SignalStale
from .publicsignals import compute

NUMERIC = {"ratio", "number", "money"}
_INT = re.compile(r"-?\d+")


def show(v: Any) -> str:
    """How a value is written in flags and reasons."""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def coerce(spec: SignalSpec, raw: Any) -> Any:
    """Convert a raw value to the signal's type, or raise SignalInvalid."""
    name, t = spec.name, spec.type

    def bad(why: str) -> SignalInvalid:
        return SignalInvalid(f"{name} {why}", [{"path": f"signals.{name}", "value": raw}])

    if t in NUMERIC:
        if isinstance(raw, bool) or not isinstance(raw, (int, float, Decimal, str)):
            raise bad("must be a number")
        try:
            v: Any = Decimal(str(raw).strip())
        except InvalidOperation:
            raise bad("must be a number") from None
        if not v.is_finite():
            raise bad("must be a finite number")
    elif t == "int":
        if isinstance(raw, int) and not isinstance(raw, bool):
            v = raw
        elif isinstance(raw, str) and _INT.fullmatch(raw.strip()):
            v = int(raw.strip())
        else:
            raise bad("must be a whole number")
    elif t == "bool":
        if not isinstance(raw, bool):
            raise bad("must be true or false")
        v = raw
    else:  # enum
        if not isinstance(raw, str) or raw not in spec.values:
            raise bad(f"must be one of {', '.join(spec.values)}")
        v = raw

    if t in NUMERIC or t == "int":
        if abs(v) > MAX_ABS:
            raise bad(f"is too large (limit {MAX_ABS})")
    if spec.range is not None:
        lo, hi = spec.range
        if not lo <= v <= hi:
            raise bad(f"must be between {lo} and {hi}")
    return v


def resolve(pack: CompiledPack, ctx: Context) -> tuple[dict[str, Any], dict[str, dict], list[str]]:
    """Returns (values for the rules, signals_used for the response, flags)."""
    for key in ctx.signals:
        if key not in pack.signals:
            raise SignalInvalid(f"unknown signal {key}", [{"path": f"signals.{key}", "value": ctx.signals[key]}])

    values: dict[str, Any] = {}
    used: dict[str, dict] = {}
    flags: list[str] = []
    local = None

    def use_default(spec: SignalSpec, why: str, error: type) -> None:
        if not spec.has_default:
            raise error(f"{spec.name} {why} and has no default", [{"path": f"signals.{spec.name}"}])
        values[spec.name] = spec.default
        used[spec.name] = {"value": spec.default, "source": "default"}
        flags.append(f"{spec.name} {why}, used default {show(spec.default)}")

    for spec in pack.signals.values():
        name = spec.name
        if name in ctx.signals:
            if spec.source != "request" and not ctx.allow_public_override:
                raise SignalInvalid(f"{name} is a {spec.source} signal and can't be sent in the request",
                                    [{"path": f"signals.{name}", "value": ctx.signals[name]}])
            values[name] = coerce(spec, ctx.signals[name])
            used[name] = {"value": values[name], "source": "request"}

        elif spec.source == "request":
            use_default(spec, "missing", SignalMissing)

        elif spec.source == "redis":
            if name not in ctx.redis_signals:
                use_default(spec, "missing", SignalMissing)
                continue
            raw, written_at = ctx.redis_signals[name]
            age = ctx.as_of - written_at
            if spec.max_staleness is not None and age > spec.max_staleness:
                why = f"stale ({age} > {spec.max_staleness})"
                if spec.on_stale == "reject":
                    raise SignalStale(f"{name} is {why}", [{"path": f"signals.{name}"}])
                if spec.on_stale == "use_default":
                    use_default(spec, why, SignalStale)
                    continue
                flags.append(f"{name} {why}, used last known value")
            values[name] = coerce(spec, raw)
            used[name] = {"value": values[name], "source": "redis"}

        else:  # public
            if local is None:
                local = ctx.as_of.astimezone(pack.timezone)
            values[name] = compute(name, local, spec.values)
            used[name] = {"value": values[name], "source": "public"}

    return values, used, flags
