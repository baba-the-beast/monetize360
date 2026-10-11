# Engine review — main @ e5867a0 (2026-10-11)

Scope: all of `src/monetize360/engine/`, `control/schema.py`, `config.py`, the tests. Method: read every line,
ran the suite, then ran targeted probes (script results quoted below).

**Verified:** 203 tests pass · `ruff check .` clean · golden files and templates byte-identical to the
hand-calculated originals · all API.md counterfactuals reproduce (3950 / 4150 / 4500).

**Verdict:** solid core — clean, pure, exact Decimal discipline, a self-check that works, a secure expression
evaluator. **Not demo-ready until #1–#3 are fixed** (each is small).

---

## What's good (keep it)

- `expr.py` security holds. Rejected at compile time: `__import__('os')`, `x.__class__`, comprehensions,
  f-strings, lambdas, `**`, `~`, subscripts, conditional expressions. Safety comes from the AST node whitelist,
  not the keyword regex — the right design.
- One fixed Decimal context (`DEC`) everywhere, quantize per row, running price = sum of rows → waterfall adds up by construction; `self_check` catches the rest.
- Guard interval: hard vs soft, max_change ignored when it falls outside hard limits, and the clamp row names the hardest binding limit. Matches ENGINE_SPEC §6.
- Counterfactuals truly re-run `evaluate()`.
- Built-in festival table + `tzdata` for exact replay.

---

## Findings (most severe first)

### HIGH

**1. A type error in a rule passes compile and then fails every request.**
```
when: "festival > 3"   → compile_pack: OK
                       → every price: InvariantViolation "can't order 'diwali' and Decimal('3')"  (HTTP 500)
when: "festival == 3"  → compiles, silently NEVER fires (price 3950 instead of 4950)
```
One bad publish takes a whole domain down, and the linter/verifier that would catch it are still 2-line stubs.
The old CEL plan gave us compile-time type checking; the custom evaluator lost it.
**Fix:** static type check in `compile_expr`. Pass `{name: type}` (num / str / bool) from the signal specs and infer the type of every node. Reject ordering across types, `==` across types, arithmetic on non-numbers, `in` whose right side isn't a list, and a `when` that isn't bool. About 40 lines, plus tests for each case.

**2. Counterfactuals cost one full evaluation per fired rule.**
```
500 fired rules: evaluate 3.2 ms, explain 1,136 ms
```
`/v1/price` returns counterfactuals on every call, so a big pack blows the p95 < 50 ms target by 20×.
**Fix:** re-run only the top 10 rule rows by `abs(delta)` and skip zero-delta rows. Then keep the top 5 by real
price difference as now. Worst case drops to about 10 evaluations (~35 ms).

**3. A huge number crashes with a raw `decimal.InvalidOperation` instead of an `EngineError`.**
```
base_price "1e30" → decimal.InvalidOperation   (not caught as EngineError → unhandled 500)
```
`q()` can't quantize more than 28 digits. The same applies to `money`/`number` signals without a `range`.
**Fix:** in `signals.coerce` and `operators.base_price`, reject `abs(x) > 1e12` (or a constant `MAX_MONEY`) with
`SignalInvalid` (422). Add a test.

### MEDIUM

**4. Pack and input problems are reported as `InvariantViolation`, which means "engine bug".**
```
hotel, base 8000         → InvariantViolation: limits cross 5600 > 5000
tenant max_surge_x 0.5   → InvariantViolation: limits cross 2100 > 1500
formula price / 0        → InvariantViolation: DivisionByZero
rule runtime type error  → InvariantViolation
```
The pitch says "invariant_violation = our bug, never a wrong price". Mixing in config and input errors muddies it
and returns the wrong HTTP status.
**Fix:** `BoundsConflict` (code `bounds_conflict`, 422) in `guards.interval`, and `RuleError` (code `rule_error`,
500, names the rule) in `stacking.fires` and `operators.formula`. Keep `InvariantViolation` only for `self_check`.
Update the ENGINE_SPEC error table and API.md. Add the linter warning with the max base_price (₹7,142.86 for hotel).

**5. Lookup table string keys aren't normalized, so they silently miss.**
```
table: { "0.50": 1.1 }, occupancy 0.50 → key_of → "0.5" ≠ "0.50" → default used, +0.00
```
**Fix:** in `compiler.adjustment`, for numeric signals convert keys with `key_of(Decimal(str(k)))`. A key that
isn't a number for a numeric signal → `PackInvalid`.

**6. `explain()` swallows `InvariantViolation`.**
`except EngineError: continue` also hides real engine bugs in counterfactual runs.
**Fix:** `except InvariantViolation: raise` before the generic skip.

### LOW / design decisions

**7. Number literals go through float.** `0.80000000000000000001` becomes `0.8`, and the probe fired `occupancy > …`
wrongly. Fix: `Decimal(ast.get_source_segment(src, node))` for numeric constants (the translated source keeps
the digits).

**8. `ending` ignores `mode`.** `mode: down, ending: 99`: 6150.40 → **6199** (goes up). This matches the spec
("nearest"), but it's surprising. Decide: `down` → largest ending ≤ R, `up` → smallest ≥ R, `half_up` → nearest.

**9. `max` stacking with discounts picks the smallest discount.** −5% and −20% → picks −5%. This matches the spec
("largest delta"). Document it in PACK_SCHEMA, or add a `min` policy for discount groups.

**10. Some non-CEL syntax is accepted:** `True`, `1 < x < 2`, `'a' in 'abc'` (the last fails at runtime). #1 fixes
the dangerous one; the others are harmless.

**11. Linter and verifier are still stubs** (expected, M7). Until they exist, step/ending compatibility, the
500-rule cap and #1 aren't enforced. Today `step: 50, ending: 99` returns prices that aren't multiples of 50.

**12. `compile_pack` trusts the caller to schema-validate first.** When `control/publisher.py` is built, add a test
that publishing always runs `control.schema.validate` before `compile_pack`.

---

## Suggested order

1 → 3 → 2 → 4 → 5 → 6 → 7, then decide 8 and 9. Each fix should come with a test that fails before it and passes after.
Do not touch `tests/golden/`.
