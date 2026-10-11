"""Impact preview (what Orb calls "simulations", for any industry): re-price recent real requests with a DRAFT pack
and report what would change before anything ships: how many prices go up / down / stay, the p10 / p50 / p90 %
change, new floor and ceiling clamps, requests that would start failing, and the biggest movers.
Records are plain dicts from the audit log (store/audit.py builds them):
    {"price_id", "as_of": ISO time, "base_price"?, "signals": {...}, "redis_signals"?: {name: [value, ISO time]},
     "reference_price"?, "price": what was actually charged}
Passing the LIVE pack too re-prices every record with it first: a mismatch means replay isn't exact (counted)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping

from monetize360.engine import Context, EngineError, Policy, compile_pack, evaluate

ONE_DP = Decimal("0.1")


def impact(draft_pack: Mapping[str, Any], records: Iterable[Mapping[str, Any]], *,
           live_pack: Mapping[str, Any] | None = None, policy: Policy | None = None) -> dict[str, Any]:
    draft = compile_pack(draft_pack)
    live = compile_pack(live_pack) if live_pack is not None else None
    changes: list[tuple[Decimal, Mapping[str, Any], Decimal, Decimal]] = []
    up = down = same = failing = replay_mismatch = 0
    new_floor = new_ceiling = 0
    failures: dict[str, int] = {}

    for rec in records:
        ctx = _context(rec)
        old = Decimal(str(rec["price"]))
        old_clamps: set[str] = set()
        if live is not None:
            try:
                replayed = evaluate(live, ctx, policy)
                old_clamps = {row.step for row in replayed.waterfall if row.kind == "clamp"}
                replay_mismatch += replayed.price != old
            except EngineError:
                replay_mismatch += 1
        try:
            new = evaluate(draft, ctx, policy)
        except EngineError as e:
            failing += 1
            failures[e.code] = failures.get(e.code, 0) + 1
            continue
        clamps = {row.step for row in new.waterfall if row.kind == "clamp"}
        new_floor += "floor" in clamps and "floor" not in old_clamps
        new_ceiling += "ceiling" in clamps and "ceiling" not in old_clamps
        pct = ((new.price - old) / old * 100).quantize(ONE_DP) if old else Decimal(0)
        up += new.price > old
        down += new.price < old
        same += new.price == old
        changes.append((pct, rec, old, new.price))

    compared = len(changes)
    pcts = sorted(c[0] for c in changes)
    movers = sorted(changes, key=lambda c: abs(c[0]), reverse=True)[:5]
    return {
        "compared": compared,
        "up_pct": _share(up, compared),
        "down_pct": _share(down, compared),
        "unchanged_pct": _share(same, compared),
        "change_p10": _pct(pcts, 10),
        "change_p50": _pct(pcts, 50),
        "change_p90": _pct(pcts, 90),
        "new_floor_hits": new_floor,
        "new_ceiling_hits": new_ceiling,
        "would_fail": failing,
        "failures": failures,
        "replay_mismatches": replay_mismatch if live is not None else None,
        "biggest_movers": [{"price_id": r.get("price_id"), "old": str(o), "new": str(n), "change_pct": str(p)}
                           for p, r, o, n in movers if p != 0],
    }


def _context(rec: Mapping[str, Any]) -> Context:
    def when(v: Any) -> datetime:
        return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    feeds = {k: (v[0], when(v[1])) for k, v in (rec.get("redis_signals") or {}).items()}
    base = rec.get("base_price")
    ref = rec.get("reference_price")
    return Context(as_of=when(rec["as_of"]), signals=dict(rec.get("signals") or {}), redis_signals=feeds,
                   base_price=Decimal(str(base)) if base is not None else None,
                   reference_price=Decimal(str(ref)) if ref is not None else None)


def _share(n: int, total: int) -> str:
    return str((Decimal(n) * 100 / total).quantize(ONE_DP)) if total else "0.0"


def _pct(sorted_values: list[Decimal], p: int) -> str | None:
    """Nearest-rank percentile; no interpolation, so the answer is always a real request's change."""
    if not sorted_values:
        return None
    rank = max(1, -(-p * len(sorted_values) // 100))          # ceil(p/100 * n)
    return str(sorted_values[rank - 1])
