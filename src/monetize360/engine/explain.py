"""Counterfactuals (ENGINE_SPEC §9): RE-RUN evaluate() with one fired rule disabled, for the 5 fired rules with
the largest |delta|; sorted by how much the price changes. Never by subtracting numbers."""
from __future__ import annotations

from decimal import Decimal

from .evaluate import evaluate
from .models import CompiledPack, Context, EngineError, InvariantViolation, Policy, Result

MAX_COUNTERFACTUALS = 5


def explain(pack: CompiledPack, ctx: Context, result: Result,
            policy: Policy | None = None) -> list[dict[str, str | Decimal]]:
    # Cost is bounded: at most 5 re-runs, however many rules fired (500 fired rules used to mean 500 re-runs).
    rule_rows = [r for r in result.waterfall if r.kind == "rule"]
    candidates = sorted(rule_rows, key=lambda r: abs(r.delta), reverse=True)[:MAX_COUNTERFACTUALS]   # stable
    out = []
    for row in candidates:
        try:
            alt = evaluate(pack, ctx, policy, frozenset({row.step}))
        except InvariantViolation:
            raise                 # an engine bug must surface, never hide inside an explanation
        except EngineError:
            continue              # e.g. a lookup only reachable with this rule on: no honest counterfactual
        out.append({"if": f"without: {row.reason}", "price": alt.price})
    out.sort(key=lambda c: abs(c["price"] - result.price), reverse=True)   # stable for ties
    return out
