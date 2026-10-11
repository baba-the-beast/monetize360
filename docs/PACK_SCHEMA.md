# Domain Pack Schema

File: [`packs/schema/pack.schema.json`](../packs/schema/pack.schema.json) (JSON Schema draft 2020-12)
Tests: [`tests/unit/test_pack_schema.py`](../tests/unit/test_pack_schema.py) — run `pytest tests/unit/test_pack_schema.py`
Runtime meaning of every field: [ENGINE_SPEC.md](ENGINE_SPEC.md) — **it wins any disagreement with this file.**

---

## 1. Why we need it

A Domain Pack is written by a **business user**, not an engineer. People make typos. The schema is the
**contract** that defines what a valid pack looks like, so mistakes are caught when the pack is saved —
never when a customer is being priced.

| Without the schema | With the schema |
|---|---|
| `ceilng: 5000` (typo) is silently ignored → **price has no ceiling** | Rejected: unknown field `ceilng` |
| `stacking: multiplly` crashes the engine at request time | Rejected at save: must be one of `multiply, add, max, first_match, exclusive` |
| A pack with 20,000 rules slows every server | Rejected: max 200 rules per group |
| A pack with no floor/ceiling → unbounded prices | Rejected: `floor` and `ceiling` are required |
| `pct: -150` → negative prices | Rejected: pct must be between -100 and 1000 |
| Business user must read docs to know field names | VS Code autocompletes fields and shows help on hover |
| Engine code defends against bad shapes everywhere | Engine trusts its input → simpler, faster code |

### Where it sits — gate 1 of 3

```
 pack.yaml ──► ① SCHEMA ──► ② LINTER ──► ③ FUZZ VERIFY ──► approve ──► publish
              is the shape   does it make   does it stay safe
              right?         sense?          on 10,000 random inputs?
```

| Gate | Checks | Example it catches |
|---|---|---|
| ① Schema | Field names, types, required fields, allowed values, size limits | `ceilng`, missing `reason`, `mode: half_even` |
| ② Linter | Meaning across fields | floor > ceiling, rule uses an undeclared signal, banned signal, CEL type error |
| ③ Verify | Behaviour | some input combination breaks `max_change` |

The schema checks **shape only**. Anything that needs to compare two fields or understand a CEL
expression belongs to the linter.

---

## 2. What it enforces

### Top level

| Field | Required | Rule |
|---|---|---|
| `domain` | ✅ | lowercase snake_case, 2–40 chars — used in the URL `/v1/price/{domain}` |
| `currency` | ✅ | ISO code, 3 capitals: `INR` |
| `signals` | ✅ | 1–100 inputs |
| `base` | ✅ | where the starting price comes from |
| `groups` | ✅ | 1–20 rule groups |
| `guards` | ✅ | floor + ceiling mandatory |
| `rounding` | ✅ | step + mode |
| `timezone` | — | IANA name, default `Asia/Kolkata`. Decides weekday / weekend / hour / minute / day / month / calendar days for `public` signals |
| `calendar` | — | Dates for public calendar signals — see below |
| `version` | — | set by the system on save; don't write it |
| `description` | — | free text |

**Unknown fields are rejected everywhere** (`additionalProperties: false`). This single setting is what
catches typos.

### Signals

```yaml
occupancy:
  type: ratio            # ratio | number | money | int | bool | enum | text
  source: request        # request | redis | public
  range: [0, 1]          # optional, not allowed on bool/enum
  default: 0.5           # optional; null = reject request if missing
```

| Conditional rule | Why |
|---|---|
| `type: enum` → `values` required (and only then allowed) | An enum without allowed values is meaningless |
| `source: redis` → `max_staleness` required | A pushed feed can go stale; you must say how stale is too stale |
| `max_staleness` / `on_stale` only allowed on `source: redis` | Request and public signals can't be stale |
| Names must be snake_case, and not `price`, `base`, `min`, `max`, `abs` | They're used inside CEL expressions |
| `type: text` — any string of 1–64 characters | Route codes (`DEL-BOM`), SKUs (`A100X`), pin codes. Usable in `==`, `in` and as a lookup key; a table on a text signal **must** have a `default`, and its keys must be **quoted** — YAML reads unquoted `NO`, `yes`, `0110` as true/false or numbers (compile error) |
| `enum` values: `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,39}$`, up to 1,000 | Real codes like `2BHK`, `DEL-BOM`, `zone:12` |

**Public signals:** `is_weekend`, `weekday`, `hour`, `minute`, `day`, `month`, and calendar signals:

```yaml
signals:
  festival: { type: enum, values: [none, diwali, christmas], source: public }
  sale_day: { type: enum, values: [none, big_billion], source: public }
calendar:                          # quote the dates
  festival: { "2026-11-08": diwali, "2026-12-25": christmas }
  sale_day: { "2026-10-03": big_billion }
```

Every value except `none` must have at least one date, or the pack won't compile. `festival` without a
calendar uses the engine's built-in Diwali/Holi table.

**Numbers are exact.** YAML reads `0.85` as a float; the compiler converts every pack number with
`Decimal(str(x))` straight away (ENGINE_SPEC §2), so no price, factor or range is ever used as a float.

### Base (starting price)

| `op` | Fields | Use |
|---|---|---|
| `from_request` | — | Caller sends `base_price` (we keep no catalog) |
| `set` | `price` | Fixed starting price |
| `band` | `signal`, `bands[{upto, price}]` | Airline fare buckets |
| `lookup_table` | `signal` + `table`, or `signals` + `rows` | Price by zone / cabin / multi-dimensional |
| `cost_plus` | `cost_signal`, `margin_pct` | Cost × (1 + margin) |
| `competitor_rel` | `competitor_signal`, `pct` | Undercut / match / premium |
| `formula` | `expr` (signals only) | Calculated base, e.g. `"50 + distance_km * 12"` for rides, delivery, energy. `pct_of_base` guards then measure the real fare |

### Rules

```yaml
- id: high-occ                     # kebab-case, required
  when: "occupancy > 0.8"          # CEL, required, max 500 chars
  then: { op: pct, value: 20 }     # required
  reason: "Occupancy above 80%"    # required — shown in the waterfall
  priority: 0                      # optional, higher runs first
  effective_from: 2026-10-20T00:00:00Z   # optional, inclusive
  effective_to:   2026-10-25T00:00:00Z   # optional, exclusive — both compared with the request's as_of
  owner: "revenue-team"                  # optional, free text
```

A rule **must** have a `reason`. That's how we guarantee every price is explainable.

**Order:** fired rules run by `priority` (higher first); equal priorities run in **file order**
(ENGINE_SPEC §5). Reordering rules inside a `multiply` group can change rounding of the deltas, so treat
rule order as part of the pack. Rule `id`s must be unique across the whole pack.

### Adjustment operators (`then`)

| `op` | Fields | Result | Limits |
|---|---|---|---|
| `pct` | `value` | +/- percent | -100 … 1000 |
| `abs` | `value` | +/- amount | — |
| `multiplier` | `value` | factor | > 0, ≤ 100 |
| `set` | `value` | override price | > 0 |
| `lookup_table` | `signal`+`table` or `signals`+`rows` | factor | ≤ 1000 entries, ≤ 5 dimensions |
| `band` | `signal`, `bands[{upto, factor}]` | factor | ≤ 100 bands |
| `piecewise_curve` | `signal`, `points[[x, factor]]` | factor (interpolated) | 2–100 points |
| `tier` | `signal`, `tiers[{from, pct}]` | percent | ≤ 50 tiers |
| `formula` | `expr` (CEL, may use `price`, `base`, `min()`, `max()`, `abs()`) | new price | linter type-checks it; bounds are checked by `/verify` (fuzz) — a static check can't bound arbitrary arithmetic |

Each operator only accepts its own fields — `{op: pct, value: 20, signal: x}` is rejected.

**No match.** A `lookup_table` key that isn't in the table uses `default`; with no `default` →
`LookupMiss` (422). A `band` value above the last `upto` → `LookupMiss`, so make the last band cover the
signal's whole `range`. Never a silent skip (ENGINE_SPEC §4–5).

### Groups

| Field | Rule |
|---|---|
| `name` | snake_case |
| `stacking` | `multiply` · `add` · `max` · `first_match` · `exclusive` |
| `rules` | 1–200 |

| `stacking` | How the fired rules combine (exact math: ENGINE_SPEC §5.2) |
|---|---|
| `multiply` | Every fired rule applies in order, each to the running price (compounding) |
| `add` | Each fired rule's change is computed against the group's starting price, then added. `set` / `formula` not allowed |
| `max` | Only the fired rule with the largest change applies (in a discount-only group: the *smallest* discount) |
| `min` | Only the fired rule with the smallest change applies — "the customer gets the single best offer" |
| `first_match` | Only the first fired rule applies |
| `exclusive` | At most one rule may fire; two or more → `ExclusiveConflict` (409), no price. The linter flags overlaps it can see |

### Guards (layer 1 guardrails)

```yaml
guards:
  floor:      { op: pct_of_base, value: 70 }   # required: abs | pct_of_base
  ceiling:    { op: abs, value: 5000 }         # required
  max_change: { pct: 15, per: 1h }             # optional
```

Limits can also come from a numeric signal — `{op: signal, signal: mrp}` (ceiling = the item's MRP),
`{op: signal, signal: cost, pct: 105}` (floor = cost + 5%).

- `max_change`: within one `per` window the price may move at most `pct` from the price at the **window start**
  (not the last price — that would let it creep `pct` per request). Tracked per `entity`; callers put the stay
  date / channel into `entity` (`deluxe-101@2026-11-08`) to keep them apart (ENGINE_SPEC §6.2).
- Floor, ceiling, tenant policy and `max_change` become one interval; hard limits always beat `max_change`.

### Rounding

```yaml
rounding: { step: 50, mode: half_up }          # mode: half_up | down | up
rounding: { step: 1, mode: half_up, ending: 99 }   # ₹6,199 style
```

`half_even` (banker's rounding, Python's default) is deliberately **not allowed** — 4,125 must become
4,150, every time.

Rounding never leaves the guard interval: an out-of-range result moves to the nearest valid value inside it;
if none exists the price stays unrounded with flag `rounding skipped` (ENGINE_SPEC §7).

### Size limits (protect the servers)

| Thing | Max |
|---|---|
| Signals | 100 |
| Groups | 20 |
| Rules per group | 200 |
| Rules in the whole pack | 500 — **linter**, see below |
| Lookup entries | 1,000 |
| Bands / curve points | 100 |
| CEL expression | 500 chars |

### Already enforced by the schema (each has a test)

| Rule | Schema |
|---|---|
| Floor and ceiling values > 0 | `limit.value` → `exclusiveMinimum: 0` |
| Rounding `step` > 0 | `exclusiveMinimum: 0` |
| `ending` is 0–99 | `minimum: 0, maximum: 99` |

### Checked by the linter, not the schema

JSON Schema checks one field at a time. It can't add up across groups or compare two fields, so these
rules live in `control/linter.py` (Person A, task A8) and return lint **errors** (publish blocked).
The first two are **also enforced by `compile_pack`** (`PackInvalid`), so they hold even before the linter exists:

| Rule | Why |
|---|---|
| Total rules across all groups ≤ 500 | Schema allows 20 groups × 200 = 4,000; cap the real cost |
| If `ending` is set: `100 % step == 0` **and** `ending % step == 0` | `step: 50, ending: 99` can never be satisfied (no multiple of 50 ends in 99). Valid: step 1 + any ending; step 50 + ending 0 or 50; step 10 + ending 90 |
| Absolute floor < absolute ceiling | Two fields; the engine would raise `BoundsConflict` on every request |
| **Warning:** highest `base_price` the pack can price | `floor: pct_of_base` + `ceiling: abs` cross above a base price (hotel: 5000 / 0.70 = ₹7,142.86) → `bounds_conflict` (422) above it |
| Ceiling ≤ tenant `max_price`, floor ≥ tenant `min_price` | Needs the tenant policy |

---

## 3. Using it

### Autocomplete in VS Code (free)

1. Install the **YAML** extension (by Red Hat).
2. Put this as the first line of any pack:
   ```yaml
   # yaml-language-server: $schema=../schema/pack.schema.json
   ```
3. Typing now autocompletes fields, shows descriptions on hover, and underlines mistakes in red.

Both templates (`hotel.yaml`, `airline.yaml`) already have this line.

### In code

`control/schema.py` validates every pack against this schema **before** the compiler sees it (the engine
reads no files — ENGINE_SPEC §1), and
`PUT /v1/packs/{domain}` returns all schema errors at once with their paths, e.g.:

```json
{ "error": "pack_invalid",
  "details": [
    { "path": "guards", "message": "'ceiling' is a required property" },
    { "path": "groups/0/stacking", "message": "'multiplly' is not one of ['multiply', 'add', ...]" }
  ] }
```

### Running the tests

```sh
.venv\Scripts\activate
pytest tests/unit/test_pack_schema.py -q
```

The tests check that every file in `packs/templates/` is valid, and that every kind of mistake listed in the test is rejected
(typos, missing guards, unknown operators, out-of-range values, oversized packs, banker's rounding, …).
**Adding a new template automatically adds it to the test.**

---

## 4. Changing the schema

- Adding an operator = add it to the `op` enum **and** add its `if/then` block, then a test case.
- Never loosen a limit (e.g. allow `pct < -100`) without asking: "could this produce a negative or
  unbounded price?"
- The schema version lives in `$id`; packs are validated against the copy in `packs/schema/` (path from `PACK_SCHEMA_PATH`).
