"""Terminal demos for the judges, engine-side (no Docker needed):
    python scripts/demo.py domains    one engine, three industries, only the pack changes (jaw-drop #1)
    python scripts/demo.py safety     linter blocks 'religion', then 10,000-case fuzz proof -> Verified Safe (#2)
    python scripts/demo.py impact     "what would this change do to real prices?" before it ships"""
from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from monetize360.control.impact import impact  # noqa: E402
from monetize360.control.linter import lint  # noqa: E402
from monetize360.control.verifier import verify  # noqa: E402
from monetize360.engine import Context, compile_pack, evaluate, explain  # noqa: E402

GREEN, RED, YELLOW, BOLD, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[2m", "\033[0m"


def pack(name: str) -> dict:
    return yaml.safe_load((ROOT / "packs/templates" / f"{name}.yaml").read_text(encoding="utf-8"))


def title(text: str) -> None:
    print(f"\n{BOLD}{'=' * 72}\n{text}\n{'=' * 72}{OFF}")


def price(name: str, ctx: Context) -> None:
    p = compile_pack(pack(name))
    r = evaluate(p, ctx)
    print(f"\n{BOLD}{name}{OFF}  ->  {BOLD}{r.currency} {r.price}{OFF}")
    for row in r.waterfall:
        sign = "" if row.kind == "base" else ("+" if row.delta >= 0 else "")
        print(f"   {row.step:<20} {sign}{row.delta:>10}   = {row.after:>10}   {DIM}{row.reason}{OFF}")
    for cf in explain(p, ctx, r):
        print(f"   {DIM}{cf['if']:<44} would be {cf['price']}{OFF}")


def domains() -> None:
    title("One engine. Three industries. Only the pack changes.")
    diwali = datetime(2026, 11, 8, 9, tzinfo=timezone.utc)
    price("hotel", Context(base_price=Decimal("3000"), signals={"occupancy": "0.85"}, as_of=diwali,
                           reference_price=Decimal("4500")))
    price("event_tickets", Context(signals={"capacity_sold": "0.85", "days_to_event": 1, "section": "premium"},
                                   as_of=diwali))
    price("ride", Context(signals={"distance_km": "10", "duration_min": "30", "demand_ratio": "2",
                                   "vehicle": "sedan"}, as_of=datetime(2026, 11, 8, 18, tzinfo=timezone.utc)))
    print(f"\n{GREEN}Same evaluate() call each time. engine/ unchanged: only packs/templates/*.yaml differ.{OFF}")


def safety() -> None:
    title("Step 1 - a pricing team adds a rule that discriminates")
    bad = pack("hotel")
    bad["signals"]["religion"] = {"type": "enum", "values": ["a", "b"], "source": "request", "default": "a"}
    bad["groups"][0]["rules"].append({"id": "religion-uplift", "when": "religion == 'b'",
                                      "then": {"op": "pct", "value": 15}, "reason": "Uplift"})
    report(lint(bad))

    title("Step 2 - remove it and lint again")
    report(lint(pack("hotel")))

    title("Step 3 - prove it: 10,000 random valid requests, checked independently of the engine")

    def bar(done: int, total: int) -> None:
        filled = 40 * done // total
        print(f"\r   [{'#' * filled}{'.' * (40 - filled)}] {done:>6,}/{total:,} cases", end="", flush=True)

    result = verify(pack("hotel"), cases=10_000, progress=bar)
    print()
    for prop in result["properties"]:
        print(f"   {GREEN}OK{OFF}  {prop}")
    badge(result)

    title("Step 4 - and when a rule IS unsafe, it finds the smallest input that breaks it")
    risky = pack("retail")
    risky["groups"][0]["rules"][1]["when"] = "inventory < 600"            # overlaps "inventory > 500"
    result = verify(risky, cases=10_000)
    badge(result)
    s = result["smallest"]
    print(f"   smallest failing input: {BOLD}{s['input']['signals']}{OFF}")
    print(f"   {s['message']}")


def report(result: dict) -> None:
    for e in result["errors"]:
        print(f"   {RED}ERROR{OFF} [{e['code']}] {e['path']}: {e['message']}")
    for w in result["warnings"]:
        print(f"   {YELLOW}warn{OFF}  [{w['code']}] {w['message']}")
    verdict = f"{GREEN}lint passed - can be published{OFF}" if result["ok"] else f"{RED}publish blocked{OFF}"
    print(f"   -> {verdict}")


def badge(result: dict) -> None:
    if result["ok"]:
        print(f"\n   {GREEN}{BOLD}VERIFIED SAFE{OFF}  {result['cases']:,} cases, 0 violations, {result['seconds']} s")
    else:
        print(f"\n   {RED}{BOLD}NOT SAFE{OFF}  {result['cases']:,} cases, violations: {result['violation_counts']}")


def impact_demo() -> None:
    title("What would raising the weekend uplift from 10% to 20% do to last month's real prices?")
    live = pack("hotel")
    p = compile_pack(live)
    records = []
    start = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    for day in range(31):
        for i, occ in enumerate(["0.4", "0.7", "0.85", "0.95"]):
            as_of = start + timedelta(days=day)
            ctx = Context(base_price=Decimal("3000"), signals={"occupancy": occ}, as_of=as_of)
            records.append({"price_id": f"{day}-{i}", "as_of": as_of.isoformat(), "base_price": "3000",
                            "signals": {"occupancy": occ}, "price": str(evaluate(p, ctx).price)})
    draft = copy.deepcopy(live)
    draft["groups"][0]["rules"][1]["then"]["value"] = 20
    r = impact(draft, records, live_pack=live)
    print(f"   {r['compared']} past prices re-priced with the draft (replay mismatches: {r['replay_mismatches']})")
    print(f"   up {r['up_pct']}%   down {r['down_pct']}%   unchanged {r['unchanged_pct']}%")
    print(f"   change p10 / p50 / p90: {r['change_p10']}% / {r['change_p50']}% / {r['change_p90']}%")
    print(f"   new ceiling clamps: {r['new_ceiling_hits']}   would fail: {r['would_fail']}")


if __name__ == "__main__":
    demos = {"domains": domains, "safety": safety, "impact": impact_demo}
    if len(sys.argv) != 2 or sys.argv[1] not in demos:
        sys.exit(__doc__)
    demos[sys.argv[1]]()
