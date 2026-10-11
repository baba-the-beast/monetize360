# Engine Specification

The exact semantics of `src/monetize360/engine/`. **If the code and this document disagree, the code is
wrong.** Golden tests (`tests/golden/*.yaml`) are hand-calculated from this document.

---

## 1. Public interface

```python
from monetize360.engine import compile_pack, evaluate, explain, Policy

pack   = compile_pack(pack_dict)            # raises PackInvalid(errors)  — once, at publish; pack_dict is already schema-valid
result = evaluate(pack, ctx, policy=None, disabled_rules=frozenset())   # every request
cfs    = explain(pack, ctx, result, policy=None)                        # counterfactuals
```

### Policy (tenant limits — passed by the API layer, recorded in the audit)

`policy` is `None` or a `Policy` with these optional fields (from the `tenant_policy` table, ARCHITECTURE §9 L2):

| Field | Type | Used in |
|---|---|---|
| `min_price` | `Decimal \| None` | §6.1 `hard_lower` (`tenant_min`) |
| `max_price` | `Decimal \| None` | §6.1 `hard_upper` (`tenant_max`) |
| `max_surge_x` | `Decimal \| None` | §6.1 `hard_upper` = `B × max_surge_x` (`tenant_surge`) |

`banned_signals` and `require_two_person` are authoring-time checks (linter / publisher), not engine inputs.
**Kill modes are not engine logic:** `freeze` is answered by the API layer from the stored last price;
`base_only` calls `evaluate()` with `disabled_rules` = every rule id, so base + guards + rounding still apply.

### Context (all inputs — nothing else may influence the price)

| Field | Type | Meaning |
|---|---|---|
| `base_price` | `Decimal \| None` | Required when `base.op == from_request` |
| `signals` | `dict[str, Any]` | Raw values from the request |
| `redis_signals` | `dict[str, (value, written_at)]` | Snapshot read by the API layer for `source: redis` signals |
| `as_of` | `datetime` (UTC, aware) | "Now". The engine never calls the clock |
| `reference_price` | `Decimal \| None` | Anchor for `max_change` (§6.2). `None` = no max_change check |
| `allow_public_override` | `bool` | `True` only in `/simulate` and tests: lets `signals` set public signals directly |

### Result

| Field | Type |
|---|---|
| `price` | `Decimal`, 2 decimal places |
| `currency` | `str` |
| `waterfall` | `list[Row]` — `Row(step, kind, delta, after, reason)`; `kind` ∈ `base, rule, clamp, rounding` |
| `signals_used` | `dict[name, {value, source}]` — source ∈ `request, redis, public, default` |
| `bounds` | `{lower, upper}` actually applied |
| `flags` | `list[str]` — e.g. `occupancy missing, used default 0.5` |

### Errors (all subclass `EngineError`)

| Error | When | API status |
|---|---|---|
| `PackInvalid` | compile failure (e.g. CEL syntax). Schema errors are caught earlier by `control/schema.py`, also as `pack_invalid` | 422 at save/publish |
| `SignalInvalid` | wrong type / out of range / not in enum / public signal overridden in `/price` | 422 |
| `SignalMissing` | missing, no default | 422 |
| `SignalStale` | stale and `on_stale: reject` | 503 |
| `LookupMiss` | key not in table and no `default`; value above last band | 422 |
| `ExclusiveConflict` | more than one rule fired in an `exclusive` group | 409 |
| `InvariantViolation` | self-check failed (§8) — **a bug, never a wrong price** | 500 |

**The engine reads no files.** It never loads `pack.schema.json`; callers (`control/schema.py`, used by
`PUT /v1/packs`, the linter and the tests) validate the pack dict first. That keeps the engine pure and
means the installed package never needs to find `packs/` on disk.

---

## 2. Number rules

1. All math uses `Decimal` inside a **fixed local context**: `decimal.Context(prec=28, rounding=ROUND_HALF_UP)`.
   Never the global context, never `float`, never built-in `round()`.
2. Pack numbers are converted with `Decimal(str(x))` at compile time.
3. **Every waterfall `delta` is quantized to `0.01` (ROUND_HALF_UP).**
4. **The running price is the sum of quantized deltas** — `after = previous_after + delta`. This makes the
   waterfall add up by construction; the self-check (§8) catches bugs that break it.
5. Factors (curves, lookups) are *not* quantized; only money deltas are.

---

## 3. Signal resolution (`signals.py`)

For each declared signal, in this order:

| `source` | Resolution |
|---|---|
| `request` | `ctx.signals[name]` → else `default` (+flag) → else `SignalMissing` |
| `redis` | If present in `ctx.signals` and `allow_public_override` is False → `SignalInvalid` (a caller can't forge a feed value). Otherwise `ctx.redis_signals[name]`; if `as_of - written_at > max_staleness` → apply `on_stale`: `use_default` (+flag; no `default` declared → `SignalStale`) / `use_last_known` (use value anyway, +flag) / `reject` (`SignalStale`). Missing → `default` (+flag) → else `SignalMissing` |
| `public` | computed from `as_of` in the pack `timezone` (default `Asia/Kolkata`). If present in `ctx.signals` and `allow_public_override` is False → `SignalInvalid` |

Then validate: type, `range` (inclusive), `values`. Undeclared keys in `ctx.signals` → `SignalInvalid`.
Numeric types (`ratio`, `number`, `money`) accept a number or a numeric string, converted with
`Decimal(str(x))`; `int` accepts an integer (or integer string); `bool` accepts only `true`/`false`.
Numeric and `int` signals, and `base_price`, must satisfy `|x| ≤ 10¹²` (`MAX_ABS`) → else `SignalInvalid`
("too large"). This keeps every amount far inside the 28-digit context, so quantizing never fails. Any
arithmetic error that still happens (only a pathological pack can cause one) → `InvariantViolation`,
never a raw exception.

Public signals: `is_weekend` (Sat/Sun), `weekday` (`mon`…`sun`), `hour` (0–23), `festival`
(name from the built-in festival table, else `none`; must be one of the signal's `values`, else `none`).

---

## 4. Base (`stage: base`)

One row: `Row(step="base", kind="base", delta=B, after=B, reason=…)`.

| op | B = |
|---|---|
| `from_request` | `ctx.base_price` (must be > 0) |
| `set` | `price` |
| `band` | `price` of the **first** band with `x <= upto` (bands ascending; `upto` **inclusive**). None → `LookupMiss` |
| `lookup_table` | table value (§5 lookup rules) |
| `cost_plus` | `cost × (1 + margin_pct/100)` |
| `competitor_rel` | `competitor × (1 + pct/100)` |

B is quantized to 0.01.

---

## 5. Adjust groups (`stage: adjust`)

Groups run top to bottom. Within a group, a rule **fires** when its `when` is true **and**
`effective_from <= as_of < effective_to` (missing bound = open) **and** it isn't in `disabled_rules`.
Fired rules are ordered by `priority` (high first), then **file order** (the order they appear in the
group). Every golden case and the §10 examples are calculated this way. Rule ids must be unique in a pack
(they name waterfall rows and `disabled_rules`).

### 5.1 What each operator produces

| op | Effect on running price `P` |
|---|---|
| `pct` v | factor `1 + v/100` |
| `multiplier` v | factor `v` |
| `lookup_table` | factor from table |
| `band` | factor of first band with `x <= upto` |
| `piecewise_curve` | factor by linear interpolation (§5.3) |
| `tier` | factor `1 + pct/100` of the highest tier with `from <= x` (none → factor 1) |
| `abs` v | amount `+v` |
| `set` v | new price `v` |
| `formula` | new price = CEL result |

Lookup rules: key = signal value as string (`true`/`false` for bools; numbers without trailing zeros, so
`0.50` and `0.5` are the same key). Table keys are normalised **by the signal's type** at compile time — a
key written as text `"0.50"` for a numeric signal matches 0.5. A key the signal can never take (not a number
for a numeric signal, not one of an enum's `values`, not true/false for a bool) → `PackInvalid`.
Missing key → `default` → else `LookupMiss`.
Multi-dimensional: key = list of the signal values in `signals` order.

### 5.2 Stacking — `G` = price at group start

| stacking | Rows and result |
|---|---|
| `multiply` | Apply fired rules in order to the running price. factor: `delta = q(P × (f − 1))`; amount: `delta = q(v)`; new price: `delta = q(v − P)`. One row per fired rule |
| `add` | Every delta computed against `G`: factor: `q(G × (f − 1))`; amount: `q(v)`. One row per fired rule. `set`/`formula` not allowed (linter) |
| `max` | Compute each fired rule's delta alone against `G`; keep the **largest** (tie → first in order). One row. In a discount-only group (all deltas negative) this keeps the *smallest* discount |
| `first_match` | Only the first fired rule in order, computed against `G`. One row |
| `exclusive` | 0 fired → nothing; 1 fired → as `first_match`; >1 → `ExclusiveConflict` |

`q()` = quantize to 0.01, half-up. A fired rule with a zero delta **still emits a row** (it fired; that's
explanation). Rules that didn't fire emit nothing.

Row: `Row(step=rule.id, kind="rule", delta, after, reason=rule.reason with {signal} placeholders filled)`.

### 5.3 Piecewise curve

Points sorted by x. `x <= x0` → `f0`; `x >= xn` → `fn`; otherwise between neighbours
`f = fa + (x − xa) × (fb − fa) / (xb − xa)`.

---

### 5.4 Expressions (`when` and `formula`)

Packs write CEL syntax: `&&`, `||`, `!`, `==`, `!=`, `<`, `<=`, `>`, `>=`, `in [..]`, `+ - * /`, parentheses,
`true` / `false` / `null`, `'strings'`, numbers and declared signal names (`price` too, in a `formula`).
Anything else — calls, attribute access, Python's `and`/`or`/`not` — is rejected at compile time
(`PackInvalid`), as is an unknown signal name.

- **Exact:** number literals and signals are `Decimal`; `x >= 0.8` with `x = 0.80` is true.
- **Type-checked at compile time** → `PackInvalid`, never a failure on a live request. Signal types map to
  number (`ratio`, `number`, `money`, `int`), true/false (`bool`) and text (`enum`). `==`/`!=` need the same
  type on both sides (`festival == 3` is an error, not a rule that silently never fires); `< <= > >=` need
  two numbers or two texts; `+ - * /` need numbers; `&& || !` need true/false; `x in [..]` needs a list of
  x's type. Comparing an enum signal with a text it can never have (`festival == 'diwaly'`) is an error.
  `when` must be true/false and `formula` must be a number.
- The only runtime expression error left is arithmetic (e.g. division by zero in a `formula`) →
  `InvariantViolation`.
- **Implementation:** `engine/expr.py` walks Python's `ast` parse tree through a whitelist (no `eval`, no
  calls, no attributes, no loops), compiled once per pack. **cel-python was measured and rejected:** it
  computes in floats (`price * 1.1` → `3300.0000000000005`), errors on `int > double`, and took ~300 µs per
  condition (a 500-rule pack ≈ 150 ms). The in-house evaluator: ~50 µs for a whole hotel price, ~3.4 ms
  for a 500-rule pack. The pack format is unchanged — packs remain valid CEL.

---

## 6. Guards (`stage: guard`) — one clamp, strictest wins

### 6.1 Build the interval

```
hard_lower = max( pack floor, tenant min_price, 0.01 )
hard_upper = min( pack ceiling, tenant max_price, B × tenant max_surge_x )      # absent limits ignored
soft       = [ ref × (1 − pct/100), ref × (1 + pct/100) ]  if max_change and reference_price, else none

if hard_lower > hard_upper  → InvariantViolation (the linter should have prevented this)
interval = soft ∩ hard   if that is non-empty
         = hard          otherwise   (hard limits always beat smoothness)
```

`floor: pct_of_base` = `B × value/100`. All bounds quantized to 0.01.

### 6.2 `reference_price` (owned by the API layer, not the engine)

`max_change: {pct: 15, per: 1h}` means *the price may move at most 15% away from the price at the start of
the current window*. The store keeps `(anchor_price, anchor_time)` per (tenant, domain, entity) — callers encode stay date /
channel in `entity`, e.g. `deluxe-101@2026-11-08` (API.md) — plus the last issued price. When
`as_of − anchor_time > per`, the anchor resets to the last issued price. The anchor is passed as
`ctx.reference_price` and recorded in the audit, so replay is exact.
(Comparing to the *last* price instead would let the price creep 15% on every single request.)

### 6.3 Clamp row

If `P` is outside the interval, clamp to the nearest edge and emit **one** row whose `step` names the
binding limit: `floor`, `ceiling`, `max_change`, `tenant_min`, `tenant_max`, `tenant_surge`, `platform_min`.
If several limits are equal at that edge, name the hardest: platform → tenant → pack → max_change.
No row when nothing is clamped.

---

## 7. Rounding (`stage: round`)

1. `R = P` rounded to a multiple of `step` with `mode` (`half_up` | `down` | `up`).
2. If `ending` is set: candidates `⌊R/100⌋×100 + ending` and that ±100; pick the nearest to `R`;
   a tie goes up.
3. If `R` is outside the guard interval, move to the nearest valid value (one that matches the step and
   ending) inside it. If none exists, `R = P` and add flag `rounding skipped: bounds tighter than step`.
4. Emit `Row(step="rounding", kind="rounding", delta=R − P)` only if non-zero.

---

## 8. Self-check (runs on every evaluation)

All must hold, else `InvariantViolation`:
- `waterfall[0].kind == "base"` and every `after == previous_after + delta`
- `price == waterfall[-1].after == base + Σ other deltas`
- every delta has exactly 2 decimal places
- `bounds.lower <= price <= bounds.upper` (unless the `rounding skipped` flag is set, in which case P itself must be inside)

---

## 9. Counterfactuals (`explain.py`)

Take the fired `rule` rows, pick the **5 with the largest `|delta|`** (ties: waterfall order), and for each
re-run `evaluate()` with `disabled_rules={rule.id}` and the **same context**:
`{"if": "without: <reason>", "price": <new price>}`. Sort by `abs(price − result.price)` descending.
Never compute a counterfactual by subtracting deltas.

- **Bounded cost:** at most 5 re-runs however many rules fired (500 fired rules: 12 ms, not 1.1 s).
- A re-run that raises `InvariantViolation` is re-raised — an engine bug is never hidden in an
  explanation. Other engine errors (e.g. `LookupMiss` only reachable with the rule off) skip that rule.

---

## 10. Worked examples

Hotel (`packs/templates/hotel.yaml`), base 3000, occupancy 0.85, weekend, Diwali, reference 4500:
`3000 → +600 (high-occ) → +360 (weekend) → +990 (festival) = 4950`. Interval: hard [2100, 5000],
soft [3825, 5175] → [3825, 5000]. No clamp. Rounding 4950 → 4950. **Price 4950.00.**
Counterfactual without festival: 3960 → rounds to **3950**.

Airline (`packs/templates/airline.yaml`), seats_sold 0.65, 3 days out, business, no reference:
base band → 6200; curve f = 1.6 + 3×(1.3−1.6)/7 = 1.4714285… → +2922.86 → 9122.86;
cabin ×2.8 → +16421.15 → 25544.01; step 1 → 25544; ending 99 → candidates 25499 / 25599 → nearest
**25499.00** (rounding delta −45.01).

All golden cases: [`tests/golden/`](../tests/golden/).
