"""Build demo/packs/*.yaml from packs/templates/*.yaml: upload-ready files whose header says what to do and what each
sample price should be. Every expected price is computed by the real engine, never typed.

    python scripts/make_demo_packs.py        # run again whenever a template changes; tests/unit/test_demo_packs.py
                                             # checks every file still uploads, lints and is Verified Safe"""
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from monetize360.engine import Context, compile_pack, evaluate  # noqa: E402

OUT = ROOT / "demo" / "packs"
FAIL = OUT / "should-fail"
OUT.mkdir(parents=True, exist_ok=True)
FAIL.mkdir(parents=True, exist_ok=True)


def utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# domain -> (title, what it shows, [(scenario, base_price or None, signals, as_of)])
DOMAINS = {
    "hotel": ("Hotel rooms", "occupancy, weekends and festivals from the pack's own calendar", [
        ("Diwali Sunday, almost full", "3000", {"occupancy": "0.85"}, utc(2026, 11, 8, 9)),
        ("Ordinary Wednesday, half full", "3000", {"occupancy": "0.5"}, utc(2026, 10, 14, 9)),
    ]),
    "airline": ("Airline seats", "fare buckets by seats sold, a days-to-departure curve, cabin class", [
        ("Business, 3 days out, plane 65% sold", None,
         {"seats_sold": "0.65", "days_to_departure": 3, "cabin": "business"}, utc(2026, 10, 14, 9)),
        ("Economy, a month out, plane 10% sold", None,
         {"seats_sold": "0.10", "days_to_departure": 30, "cabin": "economy"}, utc(2026, 10, 14, 9)),
    ]),
    "ride": ("Ride hailing", "a fare built from km and minutes, demand surge, a night surcharge", [
        ("Sedan, 10 km / 30 min, demand 2x, afternoon", None,
         {"distance_km": "10", "duration_min": "30", "demand_ratio": "2", "vehicle": "sedan"}, utc(2026, 10, 14, 8, 30)),
        ("Auto, 5 km / 15 min, normal demand, midnight", None,
         {"distance_km": "5", "duration_min": "15", "demand_ratio": "1", "vehicle": "auto"}, utc(2026, 10, 14, 18, 30)),
    ]),
    "retail": ("Retail shelf price", "cost + margin, overstock clearance vs low stock, bulk tiers, floor = 85% of base", [
        ("Overstocked, buying 12", None, {"cost": "1000", "inventory": 600, "qty": 12}, utc(2026, 10, 14, 9)),
        ("Only 10 left, buying 1", None, {"cost": "1000", "inventory": 10, "qty": 1}, utc(2026, 10, 14, 9)),
    ]),
    "parking": ("Parking", "price by hours parked, vehicle type, a nearly full lot, weekends", [
        ("Car, 2.5 h, lot 95% full, Wednesday", None,
         {"hours": "2.5", "occupancy": "0.95", "vehicle": "car"}, utc(2026, 10, 14, 9)),
        ("SUV, all day, Saturday", None, {"hours": "10", "occupancy": "0.5", "vehicle": "suv"}, utc(2026, 10, 17, 9)),
    ]),
    "ev_charging": ("EV charging", "grid price + 40%, charger speed, time-of-day curve, member discount", [
        ("Fast charger at 6 pm, member", None,
         {"grid_price": "8", "charger": "dc_fast", "member": True}, utc(2026, 10, 14, 12, 30)),
        ("Slow charger at 3 am", None,
         {"grid_price": "8", "charger": "ac_slow", "member": False}, utc(2026, 10, 13, 21, 30)),
    ]),
    "event_tickets": ("Event tickets", "section price, sell-through vs last minute (bigger wins), early bird", [
        ("Premium, 85% sold, show tomorrow", None,
         {"capacity_sold": "0.85", "days_to_event": 1, "section": "premium"}, utc(2026, 10, 14, 9)),
        ("VIP, 30% sold, 90 days out", None,
         {"capacity_sold": "0.30", "days_to_event": 90, "section": "vip"}, utc(2026, 10, 14, 9)),
    ]),
    "saas": ("SaaS subscription", "a plan x region price table, seat tiers, annual billing discount", [
        ("Pro, India, 25 seats, annual", None,
         {"seats": 25, "plan": "pro", "region": "india", "annual": True}, utc(2026, 10, 14, 9)),
        ("Starter, US, 3 seats, monthly", None,
         {"seats": 3, "plan": "starter", "region": "us", "annual": False}, utc(2026, 10, 14, 9)),
    ]),
    "cloud_gpu": ("Cloud GPU (USD)", "GPU type, a utilisation curve, spot discount, cent-level prices", [
        ("A100, fleet 90% busy", None, {"utilization": "0.9", "gpu": "a100"}, utc(2026, 10, 14, 9)),
        ("T4 spot, fleet half busy", None, {"utilization": "0.5", "gpu": "t4", "spot": True}, utc(2026, 10, 14, 9)),
    ]),
    "food_delivery": ("Food delivery fee", "distance bands, rain and few riders added together", [
        ("Raining, few riders, 4 km", None,
         {"distance_km": "4", "rain": True, "rider_supply": "0.2"}, utc(2026, 10, 14, 9)),
        ("Dry, plenty of riders, 1.5 km", None,
         {"distance_km": "1.5", "rain": False, "rider_supply": "0.6"}, utc(2026, 10, 14, 9)),
    ]),
}


def show(v):
    return "yes" if v is True else "no" if v is False else str(v)


def when(t: datetime) -> str:
    local = t.astimezone(ZoneInfo("Asia/Kolkata"))
    return f"{local:%a %d %b %Y, %H:%M} IST"


def body(pack_text: str) -> str:
    """The template's YAML without its editor-schema line (re-added with the right path)."""
    kept = (line for line in pack_text.splitlines() if not line.startswith("# yaml-language-server"))
    return "\n".join(kept).strip() + "\n"


def price(pack, base, signals, as_of) -> str:
    ctx = Context(as_of=as_of, base_price=Decimal(base) if base else None, signals=signals)
    r = evaluate(compile_pack(pack), ctx)
    return f"{r.currency} {r.price:,.2f}"


def header(n, title, what, scenarios, extra=None, schema="../../packs/schema/pack.schema.json"):
    lines = [f"# yaml-language-server: $schema={schema}",
             "# " + "=" * 98,
             f"# DEMO {n:02d} - {title}: {what}",
             "#",
             "# Upload: admin console -> Domains -> Upload rules -> drop this file (it saves a DRAFT).",
             "# Then on the version page: Check -> Safety test -> Impact -> Approve (a second key) -> Publish.",
             "#",
             "# Try a price (version page -> Try a price), expected result computed by the engine:"]
    for name, base, signals, as_of in scenarios:
        inputs = ", ".join(f"{k} {show(v)}" for k, v in signals.items())
        bp = f"base price {base}, " if base else ""
        lines.append(f"#   {name}")
        lines.append(f"#     {bp}{inputs}; date {when(as_of)}  ->  {scenarios_price[(n, name)]}")
    if extra:
        lines += ["#"] + [f"# {x}" for x in extra]
    lines.append("# " + "=" * 98)
    return "\n".join(lines) + "\n"


scenarios_price = {}
order = list(DOMAINS)
for n, domain in enumerate(order, 1):
    text = (ROOT / "packs/templates" / f"{domain}.yaml").read_text(encoding="utf-8")
    pack = yaml.safe_load(text)
    title, what, scenarios = DOMAINS[domain]
    for name, base, signals, as_of in scenarios:
        scenarios_price[(n, name)] = price(pack, base, signals, as_of)
    (OUT / f"{n:02d}-{domain}.yaml").write_text(header(n, title, what, scenarios) + body(text), encoding="utf-8")

# ---- 11: a new hotel version, for the Impact preview and approve -> publish
hotel_text = (ROOT / "packs/templates/hotel.yaml").read_text(encoding="utf-8")
v2 = body(hotel_text).replace('''      - id: weekend
        when: "is_weekend"
        then: { op: pct, value: 10 }''', '''      - id: weekend
        when: "is_weekend"
        then: { op: pct, value: 20 }        # was 10: the change this demo publishes''')
assert "value: 20 }        # was 10" in v2
v2_pack = yaml.safe_load(v2)
sat = [("Saturday, half full", "3000", {"occupancy": "0.5"}, utc(2026, 10, 17, 9))]
scenarios_price[(11, "Saturday, half full")] = price(v2_pack, *sat[0][1:])
old = price(yaml.safe_load(hotel_text), *sat[0][1:])
(OUT / "11-hotel-v2-weekend-20.yaml").write_text(header(
    11, "Hotel, NEW VERSION", "weekend uplift raised from 10% to 20%", sat,
    extra=["Upload this AFTER 01-hotel is live. Impact shows what it would do to recent real prices",
           f"before anyone approves it (same Saturday request: {old} today -> {scenarios_price[(11, 'Saturday, half full')]} with this version).",
           "Then approve with a second key, publish, and roll back with one click."]) + v2, encoding="utf-8")

# ---- should-fail: one per safety gate
typo = body(hotel_text).replace("  ceiling:    { op: abs, value: 5000 }", "  ceilng:     { op: abs, value: 5000 }   # typo")
assert "ceilng" in typo
unfair = body(hotel_text).replace('''  festival:    { type: enum, values: [none, diwali, holi], source: public, default: none }''',
                                  '''  festival:    { type: enum, values: [none, diwali, holi], source: public, default: none }
  religion:    { type: enum, values: [a, b], source: request, default: a }''').replace(
    '''        reason: "Festival: {festival}"''', '''        reason: "Festival: {festival}"
      - id: religion-uplift
        when: "religion == 'b'"
        then: { op: pct, value: 15 }
        reason: "Uplift"''')
assert "religion-uplift" in unfair
retail_text = (ROOT / "packs/templates/retail.yaml").read_text(encoding="utf-8")
overlap = body(retail_text).replace("when: inventory < 20", "when: inventory < 600       # overlaps 'inventory > 500'")
assert "inventory < 600" in overlap

fail_schema = "../../../packs/schema/pack.schema.json"
for name, text, gate, story in [
    ("gate1-typo-hotel.yaml", typo, "GATE 1 (schema)",
     ["A typo: 'ceilng' instead of 'ceiling'. Without the schema, the pack would silently lose its price ceiling.",
      "Expected: the upload is REFUSED at once - nothing is saved - and the error names guards/ceilng."]),
    ("gate2-unfair-hotel.yaml", unfair, "GATE 2 (linter: fairness)",
     ["A rule that charges more by religion. It is valid YAML and compiles, so the draft IS saved.",
      "Expected: Check fails with \"Fairness violation: 'religion' is a protected attribute\"; it can't be approved."]),
    ("gate3-overlap-retail.yaml", overlap, "GATE 3 (fuzz verifier)",
     ["Two 'exclusive' rules that overlap (inventory > 500 and inventory < 600). Lint passes: it looks fine.",
      "Expected: the Safety test runs 10,000 requests, fails, and shows the smallest breaking input: inventory = 501."]),
]:
    hdr = [f"# yaml-language-server: $schema={fail_schema}", "# " + "=" * 98,
           f"# SHOULD FAIL - {gate}", "#"] + [f"# {s}" for s in story] + ["# " + "=" * 98]
    (FAIL / name).write_text("\n".join(hdr) + "\n" + text, encoding="utf-8")

for (n, name), p in scenarios_price.items():
    print(f"{n:02d} {name:48} {p}")
