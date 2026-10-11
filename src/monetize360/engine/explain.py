"""Counterfactuals (ENGINE_SPEC §9): RE-RUN evaluate() with one fired rule disabled, for the 10 rule rows with the
largest |delta|; keep the 5 that change the price most. Never by subtracting numbers. A re-run that fails is
reported as {"if", "error": {code, message}}; an engine bug (InvariantViolation) is raised."""
from __future__ import annotations

from decimal import Decimal

from .evaluate import evaluate
from .models import CompiledPack, Context, EngineError, InvariantViolation, Policy, Result

MAX_COUNTERFACTUALS = 5
MAX_RERUNS = 10


def explain(pack: CompiledPack, ctx: Context, result: Result,
            policy: Policy | None = None) -> list[dict[str, str | Decimal]]:
    # Cost is bounded: at most 10 re-runs, however many rules fired (500 fired rules used to mean 500 re-runs).
    # Zero-delta rows rank last but are NOT skipped: in a first_match / max group, switching off a selected
    # zero-delta rule lets the next rule apply, so the price does change.
    rule_rows = [r for r in result.waterfall if r.kind == "rule"]
    candidates = sorted(rule_rows, key=lambda r: abs(r.delta), reverse=True)[:MAX_RERUNS]   # stable
    out = []
    for row in candidates:
        try:
            alt = evaluate(pack, ctx, policy, frozenset({row.step}))
        except InvariantViolation:
            raise                 # an engine bug must surface, never hide inside an explanation
        except EngineError as e:
            # e.g. without this rule another one divides by zero: say so instead of silently dropping it (S3)
            out.append({"if": f"without: {row.reason}", "error": {"code": e.code, "message": e.message}})
            continue
        out.append({"if": f"without: {row.reason}", "price": alt.price})
    # priced ones first, biggest change first (stable for ties); failed re-runs last
    out.sort(key=lambda c: (0, -abs(c["price"] - result.price)) if "price" in c else (1, 0))
    return out[:MAX_COUNTERFACTUALS]
