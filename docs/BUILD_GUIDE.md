# Build Guide — handoff

A milestone is done only when the Tester has run its checks — not when the code "looks right".

---

## Team split — 3 people

Split by **folder ownership**, so nobody edits the same files (almost no merge conflicts).

| Person | Role | Owns (only they edit) | Milestones |
|---|---|---|---|
| **A — Engine** | The math. Most critical, needs the most care | `src/monetize360/engine/`, `control/schema.py`, `control/linter.py`, `control/verifier.py`, `control/replay.py`, `control/impact.py`, `tests/unit/`, `tests/property/` | M1, M2, M7 (lint/verify), M6 (replay) |
| **B — Platform** | Everything around the engine | `api/`, `security/`, `store/`, `workers/`, `control/publisher.py`, `deploy/`, `docker-compose.yml`, `scripts/seed_demo.py`, `bench/`, `tests/integration/` | M3, M4, M5, M6 (audit), M7 (kill/policy), M8 |
| **C — Packs, Tests & Demo** (learner/tester) | Correctness truth + the story | `packs/`, `tests/golden/`, `docs/`, `scripts/check_engine_frozen.py`, demo script | Golden cases, extra domains, Part B checks, Part C research, demo |

### Hour-by-hour

| Hour | A — Engine | B — Platform | C — Packs, Tests & Demo |
|---|---|---|---|
| 0–0.5 | **All three:** read `AGENTS.md`, `ENGINE_SPEC.md`, `API.md`. Agree on the plan | ← | ← |
| 0.5–4 | **M1** engine core until `pytest tests/golden` = 100% | Docker Compose up, DB init runs; **M3** API skeleton using a **fake `evaluate()`** that returns the API.md example (so B never waits for A) | Write golden cases: hotel + airline to 15 each (Part B list). Research R1–R3 |
| **4** | **Sync 1:** A merges engine → B swaps the fake for the real engine → C runs M1–M3 checks | ← | ← |
| 4–6 | **M2** explain, purity test, Hypothesis invariants | **M4** keys, tenants, RLS, seed script | Ride + retail (+ parking) packs **with golden cases** — A's engine must pass them with zero engine changes |
| 6–8 | Linter + verifier (**M7** part), replay + impact | **M5** publish / approve / rollback / hot reload; **M6** audit worker + routes | Run M4–M5 checks; write the demo script (ARCHITECTURE §13) |
| **8** | **Sync 2:** full system test end-to-end, fix the top 3 bugs together | ← | ← |
| 8–9.5 | Bug fixes, profile engine latency | **M7** kill switch + tenant policy; **M8** Locust, scale, Postgres-down test | Run M6–M8 checks; fill scoreboard with measured numbers |
| **10** | **FEATURE FREEZE — no new features** | ← | ← |
| 10–12 | Fix only what breaks the demo | Record fallback terminal captures | **Lead 2 demo rehearsals**; final README + ARCHITECTURE numbers |

### Working rules
- **Interfaces are fixed in the docs:** A builds exactly `ENGINE_SPEC.md §1`; B calls exactly that. Neither changes it without telling the other.
- **Git:** one branch per person, merge to `main` at least every 2 hours and at each sync. Pull before you start each task.
- **C's golden cases are the contract.** If A thinks a golden case is wrong, A and C recalculate it by hand together — A never edits it alone.
- **Cut order if behind at hour 8:** impact preview → scoreboard endpoint → batch endpoint → ride/retail packs (keep hotel + airline).

---

## Part A — Builder

### 1. Read in this order (30 min)
1. `README.md` — setup
2. `AGENTS.md` — the rules (your AI tool reads this automatically)
3. `docs/ENGINE_SPEC.md` — **the exact math. Most important file.**
4. `docs/API.md` — exact request/response JSON
5. `docs/PACK_SCHEMA.md` — what a pack looks like
6. `docs/ARCHITECTURE.md` §2, §3, §4, §7, §9 — the why

### 2. Setup
```sh
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
pytest tests/unit/test_pack_schema.py      # all pass = setup OK
pytest tests/golden                         # fails until the engine exists - that's the target
```
Install Docker (Docker Desktop is free for students; or Podman / Rancher Desktop) before milestone 3.

### 3. How to prompt the AI (copy this pattern)

> Implement `src/monetize360/engine/rounding.py`. Its docstring says what it does.
> Follow `docs/ENGINE_SPEC.md` §7 exactly and the rules in `AGENTS.md`.
> Done when `pytest tests/unit/test_rounding.py tests/golden` passes for rounding cases.
> Do not change any test expectations.

One file (or one small group of files) per prompt. Always name the spec section and the test that proves it.

### 4. Milestones

| # | Build | Files | Done when |
|---|---|---|---|
| M1 | Engine core | `control/schema.py`, `engine/models.py`, `compiler.py`, `signals.py`, `publicsignals.py`, `operators.py`, `stacking.py`, `guards.py`, `rounding.py`, `evaluate.py` | `pytest tests/golden` **100%** + `tests/unit` pass |
| M2 | Explain + purity | `engine/explain.py`, `tests/unit/test_engine_is_pure.py`, `tests/property/test_invariants.py` | counterfactual for hotel case 1 = 3950; purity test passes; Hypothesis invariants pass |
| M3 | Pricing API (no DB yet) | `api/app.py`, `schemas.py`, `errors.py`, `routes/price.py`, `routes/health.py` | `POST /v1/simulate/hotel` returns the API.md `/price` example (without `price_id`) from Swagger |
| M4 | Keys + tenants + DB | `security/*`, `store/db.py`, `store/api_keys.py`, `api/deps.py`, `scripts/seed_demo.py` | `docker compose up` works; hotel key prices hotel; hotel key on airline → 404; revoked key → 401 |
| M5 | Packs lifecycle + hot reload | `store/packs.py`, `store/packcache.py`, `store/cache.py`, `control/publisher.py`, `routes/packs.py` | draft → approve (2nd key) → publish → price changes on all replicas within 1 s; rollback works |
| M6 | Audit + replay | `workers/audit_worker.py`, `store/audit.py`, `control/replay.py`, `routes/audit.py` | every price has an audit row; replay identical; edit a row in psql → `/audit/verify` names it |
| M7 | Safety | `control/linter.py`, `control/verifier.py`, `control/impact.py`, guards L2/L3, `routes/admin.py` (kill, policy) | bad pack blocked by lint; verify 10k cases 0 violations; kill switch works |
| M8 | Prove it | `bench/locustfile.py`, `routes/scoreboard.py`, `scripts/check_engine_frozen.py` | Locust numbers recorded; Postgres stopped → pricing still works |
| — | **Feature freeze at hour 10** | | |

M1–M3 without Docker. If time runs short, M7's `impact` and M8's scoreboard are the first to cut.

---

## Part B — Tester checklist

Run these yourself after each milestone. Write results (pass/fail + screenshot) in a shared note.

### M1 — engine
- [ ] `pytest tests/golden -v` — all green, none skipped
- [ ] Open the test file — **no expected value was changed** (`git diff tests/golden`)
- [ ] Ask the builder to price hotel with occupancy `7` → must be an error, not a price

### M2
- [ ] Counterfactual "without festival" for hotel case 1 is **3950**
- [ ] `pytest tests/property -v` passes

### M3 — in Swagger (`/docs`)
- [ ] Response matches `docs/API.md` field-by-field (money as strings with 2 decimals)
- [ ] Waterfall: base + all deltas = price (add it up yourself once)
- [ ] Send `festival` in `/price` → 422 (public signals can't be overridden)

### M4 — security
- [ ] No key → 401 · wrong key → 401 · revoked key → 401 within a second
- [ ] Hotel key → `/v1/price/airline` → **404** (not 403)
- [ ] Pricing key → `PUT /v1/packs/hotel` → 403
- [ ] Look in the `api_keys` table: no readable secret, only a hash

### M5 — lifecycle
- [ ] Same person creates and approves → rejected
- [ ] Publish a change → price changes; rollback → old price returns

### M6 — audit
- [ ] Every `price_id` from M3 onward is in `/v1/audit/{id}`
- [ ] `/v1/replay` → `identical: true`
- [ ] In psql: `UPDATE audit_log ...` as the app user → **permission denied**; as owner → then `/audit/verify` reports the row

### M7 — safety
- [ ] Pack with `ceilng` typo → rejected · floor > ceiling → lint error
- [ ] `/verify` → 0 violations
- [ ] Kill switch `freeze` → prices stop changing

### M8 — resilience
- [ ] `docker compose stop postgres` → `/v1/price` still 200
- [ ] `docker compose up -d --scale pricing-api=4` + restart nginx → Locust req/s goes up
- [ ] `python scripts/check_engine_frozen.py` after adding a new pack → engine diff empty

### Golden cases to write by hand (Tester job — highest value)
Use `docs/ENGINE_SPEC.md`. Write the arithmetic as comments like `tests/golden/airline.yaml` does.
- Hotel: max_change clamp **up** and **down**, floor clamp, missing occupancy → default 0.5, two weekend-only cases, rounding near the ceiling
- Airline: each fare bucket boundary (0.50, 0.80, 1.00), curve at 7 and 60 days, business + max_change
- One case per stacking policy (`add`, `max`, `first_match`, `exclusive` conflict) — needs a small test pack
- Target: 15+ per domain

---

## Part C — Research list (Tester)

| # | Question | Why it matters | Where it goes |
|---|---|---|---|
| R1 | Exact Diwali / Holi / Durga Puja / Eid dates for 2026–2027 | `festival` public signal must be right on demo day | `engine/publicsignals.py` table |
| R2 | Judging rubric and weights; what deliverables are mandatory (video? doc? repo?) | Decides what to cut at hour 9 | this guide |
| R3 | Is Docker allowed/available on the demo machine? Internet? | Offline fallback plan | demo script |
| R4 | Realistic numbers: Indian hotel occupancy bands, airline fare spreads by days-to-departure | Templates should look real to judges | `packs/templates/*.yaml` |
| R5 | 2–3 more industries for "judge's choice" (parking, EV charging, event tickets) — draft their packs | Live domain-add demo | `packs/templates/` |
| R6 | How PROS / Pricefx / IDeaS describe their product (one line each) | "How are you different?" answer | pitch |
| R7 | One real public pricing incident (surge-pricing backlash, pricing glitch) | 30-second demo hook | pitch |
