"""Replay a price_id from its audit record (same or different pack version). Simulation only, never writes audit.

The engine is pure, so the recorded inputs + the recorded pack version must give the recorded price again.
`identical: false` against the SAME version means the record or the pack was altered, or the engine changed."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from monetize360.engine import Context, EngineError, Policy, Result, compile_pack, evaluate


def output_of(result: Result) -> dict[str, Any]:
    """The part of a result that is audited and compared on replay. Money as two-decimal text."""
    return {
        "price": f"{result.price:.2f}",
        "currency": result.currency,
        "waterfall": [{"step": r.step, "kind": r.kind, "delta": f"{r.delta:.2f}", "after": f"{r.after:.2f}",
                       "reason": r.reason} for r in result.waterfall],
        "bounds": {"lower": f"{result.bounds['lower']:.2f}", "upper": f"{result.bounds['upper']:.2f}"},
        "flags": list(result.flags),
    }


def policy_json(policy: Policy | None) -> dict[str, str] | None:
    """Tenant limits as they are stored in an audit record (money as text)."""
    if policy is None:
        return None
    fields = {"min_price": policy.min_price, "max_price": policy.max_price, "max_surge_x": policy.max_surge_x}
    return {k: str(v) for k, v in fields.items() if v is not None} or None


def policy_of(record: Mapping[str, Any]) -> Policy | None:
    """The tenant limits that applied when this price was issued."""
    stored = record["inputs"].get("policy")
    return Policy(**{k: Decimal(str(v)) for k, v in stored.items()}) if stored else None


def context_of(record: Mapping[str, Any]) -> Context:
    """Rebuild the engine's input from an audit record."""
    inputs = record["inputs"]
    base, ref = inputs.get("base_price"), record.get("reference_price")
    feeds = {name: (value, datetime.fromisoformat(written_at))
             for name, (value, written_at) in (inputs.get("redis_signals") or {}).items()}
    return Context(as_of=record["as_of"], base_price=Decimal(str(base)) if base is not None else None,
                   signals=dict(inputs.get("signals") or {}), redis_signals=feeds,
                   reference_price=Decimal(str(ref)) if ref is not None else None,
                   allow_public_override=bool(inputs.get("allow_public_override", False)))


def replay(record: Mapping[str, Any], pack: Mapping[str, Any]) -> dict[str, Any]:
    """Re-price `record` with `pack` (a schema-valid pack dict). Returns {original, replayed, identical, diff}."""
    original = dict(record["output"])
    kill = record["inputs"].get("kill_mode")
    if kill == "freeze":
        # the engine did not run for this price: the entity's previous price was repeated
        return {"original": original, "replayed": None, "identical": None, "diff": [],
                "reason": "issued under kill switch 'freeze': the last price was repeated, nothing was calculated",
                "frozen_from": record["inputs"].get("frozen_from")}
    try:
        compiled = compile_pack(pack)
        rules_off = compiled.rule_ids if kill == "base_only" else frozenset()
        replayed: dict[str, Any] = output_of(evaluate(compiled, context_of(record), policy_of(record), rules_off))
    except EngineError as e:
        replayed = {"error": {"code": e.code, "message": e.message}}
    return {"original": original, "replayed": replayed, "identical": replayed == original,
            "diff": _diff(original, replayed)}


def _diff(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[dict[str, Any]]:
    if "error" in b:
        return [{"field": "error", "original": None, "replayed": b["error"]}]
    out = [{"field": k, "original": a.get(k), "replayed": b.get(k)}
           for k in ("price", "currency", "bounds", "flags") if a.get(k) != b.get(k)]
    rows_a = {r["step"]: r for r in a.get("waterfall", [])}
    rows_b = {r["step"]: r for r in b.get("waterfall", [])}
    for step in list(rows_a) + [s for s in rows_b if s not in rows_a]:
        if rows_a.get(step) != rows_b.get(step):
            out.append({"field": f"waterfall.{step}", "original": rows_a.get(step), "replayed": rows_b.get(step)})
    return out
