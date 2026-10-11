"""Counterfactuals (ENGINE_SPEC §9): RE-RUN evaluate() with one fired rule disabled; keep the top 5.
Never by subtracting numbers."""
from __future__ import annotations

from decimal import Decimal

from .evaluate import evaluate
from .models import CompiledPack, Context, EngineError, Policy, Result

MAX_COUNTERFACTUALS = 5


def explain(pack: CompiledPack, ctx: Context, result: Result,
            policy: Policy | None = None) -> list[dict[str, str | Decimal]]:
    out = []
    for row in result.waterfall:
        if row.kind != "rule":
            continue
        try:
            alt = evaluate(pack, ctx, policy, frozenset({row.step}))
        except EngineError:
            continue          # e.g. a lookup only reachable with this rule on: no honest counterfactual
        out.append({"if": f"without: {row.reason}", "price": alt.price})
    out.sort(key=lambda c: abs(c["price"] - result.price), reverse=True)   # stable for ties
    return out[:MAX_COUNTERFACTUALS]
