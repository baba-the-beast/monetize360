"""Stacking policies per group: multiply, add, max, first_match, exclusive (ENGINE_SPEC §5, §5.2)."""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from .expr import ExprError
from .models import ExclusiveConflict, Group, InvariantViolation, Row, Rule, q
from .operators import effect
from .signals import show

_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def delta(kind: str, number: Decimal, price: Decimal) -> Decimal:
    """Money change caused by an operator's number applied to `price`, quantized to 0.01."""
    if kind == "factor":
        return q(price * (number - 1))
    if kind == "amount":
        return q(number)
    return q(number - price)          # kind == "price": new price


def fires(rule: Rule, values: dict, as_of: datetime) -> bool:
    if rule.effective_from is not None and as_of < rule.effective_from:
        return False
    if rule.effective_to is not None and as_of >= rule.effective_to:
        return False
    try:
        result = rule.when(values)
    except ExprError as e:
        raise InvariantViolation(f"rule {rule.id}: {e}") from None
    if not isinstance(result, bool):
        raise InvariantViolation(f"rule {rule.id}: condition returned {result!r}, not true/false")
    return result


def reason(rule: Rule, values: dict) -> str:
    return _PLACEHOLDER.sub(lambda m: show(values[m.group(1)]) if m.group(1) in values else m.group(0),
                            rule.reason)


def apply_group(group: Group, price: Decimal, values: dict, as_of: datetime,
                disabled: frozenset[str]) -> tuple[list[Row], Decimal]:
    """Apply one group to the running price. Returns its waterfall rows and the new running price."""
    fired = [r for r in group.rules if r.id not in disabled and fires(r, values, as_of)]
    if not fired:
        return [], price
    if group.stacking == "exclusive" and len(fired) > 1:
        ids = [r.id for r in fired]
        raise ExclusiveConflict(f"rules {', '.join(ids)} all fired in exclusive group {group.name}",
                                [{"path": f"groups.{group.name}", "value": ids}])

    start = price
    rows: list[Row] = []

    def emit(rule: Rule, d: Decimal) -> None:
        nonlocal price
        price = price + d
        rows.append(Row(rule.id, "rule", d, price, reason(rule, values)))

    if group.stacking == "multiply":
        for r in fired:
            emit(r, delta(r.then.kind, effect(r.then, price, values), price))
    elif group.stacking == "add":
        for r in fired:
            emit(r, delta(r.then.kind, effect(r.then, start, values), start))
    elif group.stacking == "max":
        best = None
        for r in fired:
            d = delta(r.then.kind, effect(r.then, start, values), start)
            if best is None or d > best[1]:
                best = (r, d)
        emit(*best)
    else:  # first_match, or exclusive with exactly one fired rule
        r = fired[0]
        emit(r, delta(r.then.kind, effect(r.then, start, values), start))
    return rows, price
