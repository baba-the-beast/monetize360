# Review — "does it really work for ANY domain?"

Repo: `main @ cd93fdb` · Date: 2026-10-11 · Reviewer tested without changing one line of the engine.

## Short answer

**The architecture is domain-agnostic. The vocabulary is not yet.**

- ✅ **8 brand-new industries priced correctly with zero engine changes** — the headline claim holds.
- ⚠️ **9 things real industries need can't be expressed in a pack today.** Two of them produce a *silently wrong price*.
- None needs a redesign. All are small, additive changes — but they are **engine changes, so they must land before
  the demo freeze** (after it, `git diff engine/` has to stay empty).

---

## Part 1 — proof it generalises

Packs written using only what `pack.schema.json` allows. Expected prices were calculated by hand **before**
running the engine. Files are in this folder: `packs/*.yaml` and `golden/*.yaml`.

| Domain | What it exercises | Hand-calculated | Engine |
|---|---|---|---|
| ride | lookup base, formula (km + minutes), surge curve, `max` stacking, night window | 323.00 | ✅ 323.00 |
| retail | cost-plus base, `exclusive` stock rules, quantity `tier`, floor as % of base | 1105.00 | ✅ 1105.00 |
| parking | duration `band` base, vehicle lookup, occupancy, step-10 rounding | 100.00 | ✅ 100.00 |
| ev_charging | cost-plus on grid price, charger lookup, hour-of-day curve, member discount, 0.5 step | 19.50 | ✅ 19.50 |
| event_tickets | section lookup base, sell-through band vs last-minute (`max`), early-bird | 3000.00 | ✅ 3000.00 |
| saas | 2-dimension lookup (plan × region), seat tiers, annual discount | 1147.00 | ✅ 1147.00 |
| cloud_gpu | USD, cents-level prices, utilisation curve, spot discount | 5.14 | ✅ 5.14 |
| food_delivery | distance band, `add` stacking (rain + few riders) | 60.00 | ✅ 60.00 |

Also verified: all 8 pass schema validation, every waterfall matches row by row, counterfactuals work in all 8,
and dropping the 16 files into the repo gives **48/48** on `pytest tests/golden tests/unit/test_pack_schema.py`.

**➜ Action A0: add these 8 packs to `packs/templates/` and the 8 golden files to `tests/golden/`.**
That turns "works for any domain" from a claim into 10 tested domains. (Paths inside the golden files already point
at `packs/templates/<domain>.yaml`.)

Things that already work and are worth showing off: signals compared with each other (`inventory < reorder`),
time windows across midnight (`hour >= 23 || hour < 5`), multi-dimensional tables, five stacking policies,
three kinds of base price.

---

## Part 2 — gaps (each was tried and blocked; exact evidence included)

### 🔴 HIGH — silently wrong price, or breaks the "zero engine change" claim

**D1. Festivals and calendar are hard-coded inside the engine.**
```
festival: {type: enum, values: [none, eid, christmas], source: public}   → compiles
priced on 25 Dec 2026 → festival = 'none' → rule never fires, no warning
```
`engine/publicsignals.py` holds a table with only Diwali and Holi for 2025–2027. Any other festival, any other
country, any tenant-specific event (IPL final, sale day, school holidays) means **editing the engine** — exactly
what the demo promises never happens. Also blocked: public signals `month` and `minute`
(`unknown public signal 'month' (known: is_weekend, weekday, hour, festival)`), so seasons and "happy hour
17:30–19:00" can't be written.
**Fix:**
1. Move dates out of the engine into data. Add an optional pack section:
   ```yaml
   calendar:
     festival: { "2026-11-08": diwali, "2026-12-25": christmas, "2026-03-21": eid }
   ```
   (keys = signal name; the built-in table becomes the default when the section is absent.) It's part of the pack, so
   it is versioned and replays exactly.
2. Add public signals `month` (1–12), `day` (1–31), `minute` (0–59), `date` isn't needed if `calendar` exists.
3. Compile error if a `festival` enum value never appears in the calendar: *"festival can never be 'eid' — no date
   in the calendar"*.

**D2. Prices below ₹0.01 / $0.01 are silently destroyed.**
```
cloud_gpu, per-second base 0.00102  → base row 0.00 → clamped to floor 0.05  (≈ 49× too high, no error)
rounding step 0.001 (KWD/BHD have 3 decimals) → "step must be a positive multiple of 0.01"
```
Money is fixed at 2 decimals. Per-second cloud, per-token API, per-SMS telecom, ad bidding, some per-kWh tariffs
don't fit. Worse than "unsupported": today it returns a confident wrong number.
**Fix (minimum, do this now):** if the raw base is > 0 but quantizes to 0.00 → `SignalInvalid`
*"base price 0.00102 is below the smallest unit 0.01 — price per 1,000 units instead"*. Same check for a
`set` rule value. **Fix (proper):** pack field `precision: 0..6` (default 2) that drives `q()`, the self-check
exponent and the rounding-step multiple; currencies like JPY use 0, KWD 3.

### 🟠 MEDIUM — a whole class of industry rule can't be written

**D3. The base price can't be calculated.**
`base: {op: formula, expr: "50 + distance_km * 12"}` → schema: `'formula' is not one of [from_request, set, band, …]`.
Rides, deliveries, energy, anything "starting fee + units × rate" has to fake it with a formula *rule*, and then
every `pct_of_base` guard and the tenant surge cap measure against the starting fee:
```
ride, real fare 215, ceiling 300% "of base" → base is 50 → fare clamped to 150   (silent, looks like a normal clamp)
```
**Fix:** allow `formula` as a base op (signals only, no `price`; must type-check to a number). ~10 lines in
`compiler.adjustment` + `operators.base_price`, 1 schema branch.

**D4. Rules can't react to the price, and formulas have no `min` / `max`.**
```
when: "competitor < price"            → unknown name 'price'
expr: "min(price, cost * 2)"          → not allowed in a rule expression
expr: "inventory > 500 ? price * 0.9 : price" → syntax error
```
The single most common retail rule — *"match the competitor if they're cheaper, but never below cost + 5%"* —
can't be expressed. **Fix:** (a) expose `price` (running) and `base` as numbers in `when`; (b) whitelist exactly
three pure functions in `expr.py`: `min(a, b…)`, `max(a, b…)`, `abs(a)` — number in, number out, by name, still no
general calls; (c) optional: CEL ternary `c ? a : b` (rewrite to Python's conditional before parsing, same-type
branches). Add attack tests: any other function name still rejected.

**D5. Floor and ceiling can't come from a signal.**
`ceiling: {op: signal, signal: mrp}` → schema rejects it. In Indian retail the legal ceiling is the product's
**MRP**, different for every item; the business floor is **cost**. Today limits are only a fixed amount or a % of
base. **Fix:** new limit op `signal` with optional `pct` — `{op: signal, signal: mrp}`,
`{op: signal, signal: cost, pct: 105}`. Signal must be numeric; resolve in `guards._limit`.

**D6. No "best discount wins" stacking.**
`stacking: min` → not allowed. Retail promos are "customer gets the single best offer". `max` keeps the *smallest*
discount (documented). **Fix:** add `min` (mirror of `max`: keep the smallest delta, tie → first in order).
5 lines + schema enum + docs.

**D7. Real-world codes don't fit in an enum.**
```
values: [DEL-BOM, BLR-HYD]   → does not match ^[a-z][a-z0-9_]{0,39}$
values: 260 city zones       → array too long (max 100)
type: string                 → not one of [ratio, number, money, int, bool, enum]
```
Airline routes, SKUs (`A100X`), room types (`2BHK`), pin codes, zone IDs. Lookup tables allow 1,000 rows but the
signal feeding them allows 100 lowercase words. **Fix:** new signal type `text` (any string ≤ 64 chars, usable in
`==`, `in`, and as a `lookup_table` key; a table on a `text` signal must have a `default`). And relax enum values to
`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,39}$`, max 1,000. Type-checker: `text` maps to the same expression type as `enum`.

### 🟡 LOW

**D8. Nothing can be free.** `price: 0` → rejected by schema; `pct: -100` → clamped back up to the floor
(delivery "free above ₹500" returns ₹15). First-15-minutes-free parking, free delivery, 100% promo codes.
**Fix:** pack flag `guards.allow_free: true` → floor may be `0` and the platform minimum becomes `0.00`.
Default stays as it is (safe).

**D9. An operator's input must be one signal.** `piecewise_curve` on `demand / supply` → rejected. Workaround:
the caller sends the ratio (fine). **Fix (optional):** `input: "<expr>"` as an alternative to `signal:`.

---

## Priority for the hackathon

| Order | Item | Why now | Size |
|---|---|---|---|
| 1 | **A0** add the 8 packs + golden files | Free proof; 10 domains tested | 10 min |
| 2 | **D2 (minimum fix)** error on sub-cent base | Removes a silent wrong price | 15 min |
| 3 | **D1** calendar as pack data + `month`/`minute` | Without it "new domain = no engine change" is false for any festival | 1–1.5 h |
| 4 | **D3** formula base | Rides/delivery/energy — likely "judge's choice" domains | 30 min |
| 5 | **D4 (a)+(b)** `price`/`base` in `when`, `min`/`max`/`abs` | Retail competitor rules | 45 min |
| 6 | **D6** `min` stacking · **D5** signal limits | Retail | 20 + 40 min |
| 7 | **D7** `text` type, relaxed enums | Routes, SKUs, zones | 45 min |
| later | D8, D9, D2 `precision`, D4 ternary | Document as known limits | — |

If time is short: **do 1–4** and list D5–D9 honestly in ARCHITECTURE under "known limits" — a stated limit is
fine in front of judges; a silent wrong price is not.

## Rules for every fix

- A test that fails before and passes after; one golden case per new feature (hand-calculated).
- `tests/golden/` existing expected values are never edited.
- Update `pack.schema.json`, `docs/PACK_SCHEMA.md` and `docs/ENGINE_SPEC.md` in the same commit.
- Everything new must type-check at compile time (no new runtime-only failures) and stay pure
  (no clock, no I/O) — the calendar comes from the pack, not from a library or the network.
- After these land, **freeze `engine/`**. New domains from then on must be packs only.

## How this was tested (reproducible)

1. `compile_pack` + `evaluate` + `explain` on the 8 packs with hand-calculated expectations → 8/8.
2. Copied `packs/*.yaml` → `packs/templates/`, `golden/*.yaml` → `tests/golden/` in a scratch copy of the repo →
   `pytest tests/golden tests/unit/test_pack_schema.py` → 48 passed.
3. Each gap: wrote the natural pack for the requirement and ran schema validation + `compile_pack` (+ a price where
   it compiled). The quoted messages are the actual outputs.
