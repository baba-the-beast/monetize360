# Monetize360 — Architecture v2

> **One engine. Any industry. Every price explainable, provably bounded, replayable and tamper-evident.**
> Companies describe pricing rules as a YAML "Domain Pack", call one API, and get a price plus the
> exact reasons behind it. We store rules and receipts — never their catalog or their customers.

| | |
|---|---|
| Event | Hackfest 2026 — 12-hour build, AI-assisted ("vibe") coding |
| Constraint | **100% free and open-source.** No paid APIs, no paid cloud, no paid software |
| Judged on | Architecture depth and real-company fit (not UI) |
| Status | v2 — supersedes `ARCHITECTURE-v1.md` |

---

## 0. The 30-second version

```
Company backend ──(API key)──► pricing-api ──► price + waterfall + counterfactuals + price_id
                                   │
                    rules in memory, never touches the DB while pricing
                                   │
                         audit record ──► hash-chained, replayable, tamper-evident
```

**What no other team will show** (section 13 has the demo script):
1. Every price **proves its own arithmetic** — the response is rejected internally if the waterfall doesn't add up to the price.
2. **Replay any price ever issued** by its `price_id` and get the identical answer — then replay it against a *draft* rule set to see what would have changed.
3. **Tamper detection live** — edit one audit row in the database by hand; `/audit/verify` points to the exact broken row.
4. **Pull the plug on Postgres mid-demo** — pricing keeps working.
5. **Add a new industry with `git diff engine/` empty.**
6. **Three-layer guardrails** — no rule set, however badly written, can produce a price outside the tenant's and the platform's limits.

---

## 1. Design principles

1. **Hot path is tiny, stateless, in-memory.** Pricing never waits on a database. Everything slow happens off the request path.
2. **Deterministic by construction.** Price = f(pack version, request inputs). No hidden clock, no randomness, no AI in the loop.
3. **Strictest rule wins.** Safety limits stack upward (pack → tenant → platform); a lower layer can tighten but never loosen a higher one.
4. **Identity comes from the key, never from the request body.** A caller cannot claim to be someone else.
5. **We are a calculator, not a second database.** The company sends facts; we return a price; their data stays theirs.
6. **Boring, free, proven tools.** Python, Postgres, Redis, nginx, Docker. Nothing exotic, nothing paid.

---

## 2. Big picture

```
                         COMPANIES' BACKENDS (server-to-server only)
                     hotel_001            airline_002            parking_003
                         │ HTTPS + Authorization: Bearer m360_live_...
                         ▼
               ┌─────────────────────────┐
               │  nginx (load balancer)  │  TLS termination, request size limits,
               └───────────┬─────────────┘  routes /v1/price* vs /v1/admin*
             ┌─────────────┴──────────────────────────┐
             ▼                                        ▼
┌──────────────────────────────┐        ┌──────────────────────────────┐
│ DECISION PLANE  (hot path)   │        │ CONTROL PLANE  (cold path)   │
│ pricing-api × N (autoscaled) │        │ control-api × 1–2            │
│  1 auth → tenant             │        │  pack drafts & versions      │
│  2 rate limit                │        │  lint (static checks)        │
│  3 load compiled pack (RAM)  │        │  verify (fuzz, 10k cases)    │
│  4 resolve signals           │        │  impact preview (replay)     │
│  5 evaluate pipeline         │        │  approve → publish → rollback│
│  6 guardrails (3 layers)     │        │  tenant policy, kill switch  │
│  7 self-check + explain      │        │  API key issue / revoke      │
│  8 emit audit event          │        │  audit query / verify        │
└──────┬───────────────┬───────┘        └──────┬───────────────┬───────┘
       │ read          │ write (async)         │ events        │ read/write
       ▼               ▼                       ▼               ▼
┌───────────────────────────────┐      ┌───────────────────────────────┐
│ REDIS  (free, open source)    │      │ POSTGRES 16  (free)           │
│  signals:{tenant}:{domain}:*  │      │  tenants, api_keys (hashes)   │
│  pub/sub: key revoked,        │      │  packs (immutable versions)   │
│   pack published, kill switch │
│  (nothing is stored only here)│
│                               │      │  pack_pointer (what is live)  │
│                               │      │  tenant_policy, price_state,  │
│  pubsub: pack_published,      │      │  audit_log (append-only,      │
│          key_revoked, kill    │      │   hash-chained, partitioned)  │
└──────────────┬────────────────┘      └───────────────▲───────────────┘
               │ consume                               │ batch insert
               ▼                                       │
        ┌───────────────────────────┐                  │
        │ audit-worker              │──────────────────┘
        │  builds hash chain,       │
        │  writes in batches        │
        └───────────────────────────┘

 Shared PUBLIC signals (read-only, all tenants): Indian festival calendar (built-in, versioned with the engine so replay never drifts), weekday.
 Observability: Prometheus /metrics on every service (free).
```

**One codebase, started in different modes.** `pricing-api` and `control-api` (or `create_app`, both in one process for local demos) all import the same `engine/` package. This is not microservice sprawl — it's one program started in three modes.

---

## 3. Multi-tenancy and domain identification  *(answers Q1)*

### 3.1 How we know *who* is calling and *which* domain

We don't guess the domain from the data — guessing can be wrong, and pricing must never be wrong. Identity is **explicit and layered**:

```
API key  ──►  tenant_id  (who you are — from the key, cannot be faked)
         ──►  allowed domains  (what this key may price, e.g. ["hotel"])
         ──►  role  (pricing | editor | approver | admin)

Request  ──►  domain  (which recipe — URL path: POST /v1/price/hotel)

Pack lookup key = (tenant_id from KEY, domain from URL)
```

- If a key is scoped to exactly one domain, the domain can be omitted — this is the "automatic" identification, and it's safe because it comes from the key.
- A travel company (one tenant) can own several domains: `hotel`, `flight`, `cab`. Each has its own pack, signals and audit chain.
- The tenant is **never** read from the request body. `hotel_001`'s key can only ever load `hotel_001`'s packs — the lookup physically cannot reach `airline_002`'s.

### 3.2 Does isolation matter? Yes — for three reasons

1. **Business secrets.** A company's pricing rules are its strategy. Leaking "we add 25% on Diwali" to a competitor is real damage.
2. **Legal risk.** Pricing tools that let competitors' private data influence each other's prices have drawn antitrust action (the US DOJ's 2025 case against RealPage). Our rule: **no tenant's private data ever affects another tenant's price.**
3. **Blast radius.** A bug or leaked key at one company must not affect any other.

### 3.3 Isolation, layer by layer

| Layer | How isolation is enforced |
|---|---|
| Auth | `tenant_id` derived only from the API key |
| In-memory packs | Dictionary keyed by `(tenant_id, domain)` — no global lookup by domain name |
| Redis | Every key prefixed `{tenant}:{domain}:` |
| Postgres | `tenant_id` on every row + **Row-Level Security** policies (second line of defence if app code has a bug) |
| Audit | Separate hash chain per (tenant, domain) |
| Rate limits | Per key and per tenant — one company's spike can't starve another |
| Errors | "Not yours" and "doesn't exist" both return `404 pack not found` — attackers can't probe what other tenants have |
| Public signals | Only genuinely public data (festival calendar, weekday) is shared, read-only |

---

## 4. API key security  *(answers Q3 — the main concern)*

### 4.1 Key format and storage

```
m360_live_K7Q2M9_8fJ2...(43 random chars, 256 bits)
│    │    │      └── secret: shown ONCE at creation, never stored
│    │    └── key_id: public, used to look the key up
│    └── environment: live | test
└── prefix: makes leaked keys easy to detect in code/logs (secret scanners)
```

| Rule | Why |
|---|---|
| Store only `SHA-256(secret)` | A database leak doesn't leak usable keys. (SHA-256 is correct here because the secret is 256 random bits; slow hashes like bcrypt exist for weak human passwords.) |
| Compare with `hmac.compare_digest` | Constant-time compare — no timing attacks |
| Show the secret once | Like GitHub tokens; lost key → issue a new one |
| Log only `key_id`, never the secret | Logs are not a leak path |
| Generate with `secrets.token_urlsafe(32)` | Cryptographically secure randomness (Python stdlib) |

### 4.2 Least privilege — every key has a scope

```json
{ "key_id": "K7Q2M9", "tenant_id": "hotel_001",
  "domains": ["hotel"], "role": "pricing",
  "rate_limit_rps": 200, "expires_at": "2027-10-01" }
```

- A **pricing** key (lives on the company's server, used constantly) can only call `/price`, `/batch`, `/simulate`. If it leaks, the attacker can *read prices* — they **cannot** change rules, publish, or read the audit log.
- **editor / approver / admin** keys are separate, used rarely, by people.
- Publishing requires an **editor** to draft and a *different* **approver** to approve (two-person rule).

### 4.3 Caching and instant revocation

Checking the database on every request would be slow, so verified keys are cached in each replica's memory. The risk: a revoked key could keep working while cached. Solution — two mechanisms:

```
Revoke key ──► Postgres: revoked_at = now()
           ──► Redis pub/sub "key_revoked K7Q2M9" ──► every replica evicts it IMMEDIATELY
Backstop:  cache entries expire after 60 s anyway (covers a missed event / Redis outage)
```

Worst case (Redis down at the moment of revocation): the key dies within 60 s. Documented, bounded, acceptable.

### 4.4 Other protections

- **Server-to-server only.** Keys must never ship inside a mobile app or browser — a customer could extract the key and send `occupancy: 0` to get a cheap price. The integration guide says this in bold.
- **Signals are validated** against declared type and range — `occupancy: 7` is rejected, not priced.
- **Rotation without downtime:** a tenant can hold two active keys, deploy the new one, then revoke the old.
- **Expiry:** keys carry `expires_at`.
- **HTTPS everywhere** (free certificates via Let's Encrypt in production; self-signed in the demo).
- **Per-key rate limiting** (Redis token bucket) — a leaked key can't be used to flood us.

---

## 5. Writing a domain — the Domain Pack  *(answers Q6)*

A pack is one YAML file. It is validated against a JSON Schema, so editors like VS Code (free YAML extension) give **autocomplete and red squiggles while typing** — a business user sees mistakes before saving.

### 5.1 Anatomy

```yaml
domain: hotel
version: 3                       # set by the system on save
currency: INR
description: Room pricing for city hotels

signals:                         # the inputs this domain understands
  occupancy:
    type: ratio                  # ratio | number | money | int | bool | enum
    range: [0, 1]
    source: request              # request | redis | public
    default: 0.5                 # used if missing
  is_weekend:  { type: bool, source: public }        # computed from the date
  festival:    { type: enum, values: [none, diwali, holi], source: public, default: none }
  competitor:
    type: money
    source: redis                # pushed by the company's feed
    max_staleness: 1h
    on_stale: use_default        # use_default | use_last_known | reject

base: { op: from_request }       # company sends base_price (we keep no catalog)

groups:                          # evaluated top to bottom
  - name: demand
    stacking: multiply           # multiply | add | max | first_match | exclusive
    rules:
      - id: high-occ
        when: "occupancy > 0.8"                     # CEL expression
        then: { op: pct, value: 20 }
        reason: "Occupancy above 80%"
      - id: weekend
        when: "is_weekend"
        then: { op: pct, value: 10 }
        reason: "Weekend"
      - id: festival
        when: "festival != 'none'"
        then: { op: pct, value: 25 }
        reason: "Festival: {festival}"

guards:
  floor:      { op: pct_of_base, value: 70 }
  ceiling:    { op: abs, value: 5000 }
  max_change: { pct: 15, per: 1h }

rounding: { step: 50, mode: half_up }
```

### 5.2 A different industry, same engine

```yaml
domain: airline
signals:
  seats_sold:        { type: ratio, range: [0, 1], source: request }
  days_to_departure: { type: int, range: [0, 365], source: request }
  cabin:             { type: enum, values: [economy, business], source: request }
base:
  op: band                       # fare buckets, exactly like real airlines
  signal: seats_sold
  bands:
    - { upto: 0.20, price: 3500 }   # bucket Q
    - { upto: 0.50, price: 4800 }   # bucket M
    - { upto: 0.80, price: 6200 }   # bucket H
    - { upto: 1.00, price: 9000 }   # bucket Y
groups:
  - name: timing
    stacking: multiply
    rules:
      - id: last-minute
        when: "true"
        then: { op: piecewise_curve, signal: days_to_departure,
                points: [[0, 1.6], [7, 1.3], [21, 1.0], [60, 0.9]] }
        reason: "{days_to_departure} days before departure"
      - id: cabin
        when: "true"
        then: { op: lookup_table, signal: cabin, table: { economy: 1.0, business: 2.8 } }
        reason: "{cabin} cabin"
guards:
  floor:   { op: abs, value: 2500 }
  ceiling: { op: abs, value: 45000 }
rounding: { step: 1, mode: half_up, ending: 99 }   # ₹6,199 style
```

### 5.3 Operator library (built once, reused by every domain)

| Operator | Does | Example |
|---|---|---|
| `pct`, `abs` | ± percent / ± fixed amount | +20%, −₹200 |
| `multiplier`, `set` | scale / override | ×1.5, set ₹999 |
| `lookup_table` | value by key (1 or more keys) | cabin, zone, `[zone, vehicle]` |
| `band` | value by range | occupancy bands, fare buckets |
| `piecewise_curve` | linear interpolation between points | days-to-departure, hour-of-day |
| `tier` | quantity tiers | 1–9 units, 10–49, 50+ |
| `competitor_rel` | match / undercut / premium | competitor −5% |
| `cost_plus` | cost × (1 + margin) | cost ₹800, margin 30% |
| `formula` | safe arithmetic (CEL) | `base * (1 + demand_index * 0.1)` |

Multi-dimensional pricing (region × tier × time) is just `lookup_table` with a list of keys, indexed at compile time for constant-time lookup.

### 5.4 The authoring workflow

```
copy a template (hotel / airline / ride / retail / parking / EV)
   → edit YAML (schema autocomplete)
   → POST /lint          static checks, instant
   → POST /simulate      try any inputs, see the waterfall
   → POST /verify        10,000 random cases, "Verified Safe" badge
   → POST /impact        replay recent real traffic against the draft
   → approve (2nd person) → publish → live on all replicas in < 1 s
```

---

## 6. Rule expressions — why CEL  *(answers Q5)*

Rules need conditions like `occupancy > 0.8 && is_weekend`. These are written by business users, so they are **untrusted text**. Options:

| Option | Problem |
|---|---|
| Python `eval()` | Runs arbitrary code. `__import__('os').system('rm -rf /')` is a valid "rule". Never. |
| Write our own parser | Hours of work in a 12-hour event, and our own bugs |
| **CEL** (Common Expression Language) | ✅ chosen |

**Why CEL** (created by Google, used in Kubernetes, Envoy and Firebase security rules):
- **Cannot run code** — no imports, no files, no network. It can only read the signals we give it.
- **Always terminates** — no loops, no recursion. A rule can't hang a server.
- **No side effects** — evaluating a rule can't change anything, so the same inputs always give the same answer.
- **Type-checked at publish time** — `occupancy > 'high'` or a typo like `ocupancy` is caught by the linter, not by a customer.
- **Compiled once at publish**, then evaluated fast on every request.
- **Free:** no extra dependency.

**Measured, then decided:** `cel-python` was tried first and failed our own rules — it computes in floats
(`price * 1.1` = 3300.0000000000005), rejects `int > double`, and took ~300 µs per condition (≈150 ms for a
500-rule pack). So packs keep **CEL syntax**, but the engine evaluates it with a ~200-line whitelisted
evaluator over Python's `ast` (`engine/expr.py`): exact `Decimal` math, compiled once, no `eval`, no calls,
no attribute access. Measured: ~50 µs for a full hotel price, ~3.4 ms for a 500-rule pack (ENGINE_SPEC §5.4).

How it stays safe: `ast.parse` only builds a syntax tree — nothing is executed. The evaluator accepts just
these node types: literals, declared signal names, `[..]` lists, `! - +`, `&& ||`, `+ - * /`, and
comparisons including `in`. Everything else — function calls, attribute access (`x.__class__`), subscripts,
lambdas, comprehensions, `**`, walrus, Python's `and/or/not/is/if` — is rejected when the pack is compiled.
`tests/unit/test_expr.py` proves it with 35+ attack strings, including `__import__('os').system(...)` and
`x.__class__.__mro__[1].__subclasses__()`.

---

## 7. The evaluation pipeline

| # | Stage | Detail |
|---|---|---|
| 1 | Auth | key → tenant, domains, role (memory cache) |
| 2 | Idempotency | `idem:{tenant}:{key}` hit → return the stored response (same `price_id`, no new audit row) |
| 3 | Rate limit | Redis token bucket per key and tenant |
| 4 | Load pack | `(tenant, domain)` → compiled bundle in RAM. Kill mode `freeze` → return the last price (`price_state.last_price`) and stop here |
| 5 | Resolve signals | request → Redis (check staleness) → public → default. Validate type/range. Record source of each |
| 6 | Base | `from_request`, `band`, `lookup_table`, `cost_plus`, `competitor_rel` … |
| 7 | Adjust | groups in order; fired rules by priority, then file order; combine per stacking policy. Volume tiers and promos are ordinary rules (`tier`, `pct`) in a group — no separate stage. Kill mode `base_only` → every rule disabled |
| 8 | Guardrails | pack → tenant → platform (section 9), one interval, one clamp |
| 9 | Round | step + mode + optional ending (₹49 / ₹99), never leaving the guard interval |
| 10 | Self-check | waterfall invariant (section 8) — fail → 500, never a wrong price |
| 11 | Explain | waterfall + up to 5 counterfactuals |
| 12 | Audit | append the row to the (tenant, domain) hash chain in Postgres (§11.1); respond |

The engine's own stages are exactly ENGINE_SPEC's: **base → adjust → guard → round → self-check**. Steps 1–4 and 12 belong to the API layer.

**No hidden inputs.** The engine never calls `now()` or reads anything not in the request context. Time comes from the request's `as_of` field (defaulted by the API layer and *recorded*). The `max_change` anchor is read once, passed in as `reference_price` and *recorded*. That is what makes replay exact.

**What "the same thing" means for `max_change`.** The anchor is kept per `entity`. Callers put anything that must be kept apart into the entity string — for hotels the stay date: `deluxe-101@2026-11-08`. Otherwise two different nights of one room would limit each other. (API.md states this convention; no extra field is needed.)

**Idempotency.** `idempotency_key` is optional; results are kept 24 h. Same key + same body → the stored response. Same key + different body → `409 idempotency_conflict`.

**One transaction per price (as built).** A real `/price` is a single Postgres transaction:

```
lock the idempotency key  → already used?  same body: return the stored response · other body: 409
lock the entity           → read its anchor → reference_price
evaluate (engine)         → an engine error rolls everything back: nothing is recorded, nothing moves
append the audit row      → the (tenant, domain) hash chain
save the anchor / last price, store the idempotent response
COMMIT
```

So the audit row, the anchor and the idempotency record can never disagree: a crash leaves all three or none.
Locks are always taken in that order (key → entity → chain), so two requests can't deadlock. Ten identical
requests at once produce one price; sixty requests for one room from two servers all see the same anchor
(both tested). Measured: about 7 ms per price, the same as with the audit row alone.

These live in Postgres, not Redis, on purpose: a lost anchor would silently switch the `max_change` guard off,
and a lost idempotency key would price a retry twice. If Postgres is unreachable the price is still returned
without them, and the response says so: `max_change not enforced: database unreachable`,
`idempotency_key not honoured: database unreachable`, `audit deferred: ...`.

---

## 8. Correctness — the price must never be wrong  *(answers Q2)*

### 8.1 Worked example (hotel pack above)

Inputs: base ₹3,000 · occupancy 0.85 · weekend · Diwali · previous price ₹4,500

| Step | Change | Running price |
|---|---|---|
| Base (from request) | — | 3,000.00 |
| high-occ: +20% | +600.00 | 3,600.00 |
| weekend: +10% | +360.00 | 3,960.00 |
| festival: +25% | +990.00 | 4,950.00 |
| floor ₹2,100 | 0.00 | 4,950.00 |
| ceiling ₹5,000 | 0.00 | 4,950.00 |
| max change 15% vs ₹4,500 → max ₹5,175 | 0.00 | 4,950.00 |
| round to ₹50, half-up | 0.00 | **4,950.00** |

Check: 3,000 + 600 + 360 + 990 = **4,950** ✓

**Counterfactuals:**
- No festival → 3,000 × 1.2 × 1.1 = 3,960 → rounds to **₹3,950**
- Occupancy ≤ 0.8 → 3,000 × 1.1 × 1.25 = 4,125 → exactly halfway between 4,100 and 4,150 → half-up → **₹4,150**

That last case is exactly why rounding mode must be **declared**, not left to the language default (Python's `round()` uses banker's rounding and would give a different answer).

### 8.2 Seven guarantees and how each is enforced

| # | Guarantee | How it's enforced | How it's verified |
|---|---|---|---|
| G1 | **Exact money math** | `decimal.Decimal` everywhere; floats rejected at the API boundary; 2-decimal quantize at each step | Unit tests; a lint rule bans `float` in `engine/` |
| G2 | **Waterfall adds up** | After every evaluation: `base + Σ(step deltas) == final`. Guard clamps and rounding are *their own rows*. Mismatch → HTTP 500, no price returned | Runs on **every live request** + property test |
| G3 | **Counterfactuals are real** | Computed by **re-running the engine** with one rule disabled (ENGINE_SPEC §9) — never by subtracting numbers | Property test: counterfactual == fresh `evaluate()` with that rule disabled |
| G4 | **Determinism** | No clock, no randomness, no I/O inside the engine; all context recorded | Replay test: 10,000 audit records replayed → 100% identical |
| G5 | **Bounds always hold** | Three guard layers; final clamp is unconditional | Fuzz (Hypothesis): 10,000 random contexts per pack → `floor ≤ price ≤ ceiling`, `max_change` respected |
| G6 | **Order is defined** | Fixed stage order; groups top-to-bottom; rules by priority then file order; stacking explicit per group | Golden tests cover each stacking policy |
| G7 | **Hand-checked truth** | **Golden set:** cases calculated by humans *before* code is written | `pytest tests/golden` — must be 100% |

### 8.3 Vibe-coding rule that protects correctness

AI-written code can look right and be subtly wrong in arithmetic. So:

1. **Humans write the golden test cases first** (inputs + hand-calculated expected price + expected waterfall rows).
2. Then let the AI write the engine **until the golden tests pass.**
3. Never edit an expected value to make a test pass without re-calculating it by hand.

Target: 15+ golden cases per domain during the event (covering each operator, each stacking policy, each guard, rounding ties).

---

## 9. Guardrail policy engine — three layers  *(answers Q4)*

**Yes, we need it** — but as a ~100-line module inside the pipeline, not a separate product (OPA etc. would be overkill for 12 hours).

```
             strictest limit always wins
                        ▲
 ┌──────────────────────┴───────────────────────┐
 │ L3  PLATFORM POLICY  (us — cannot be changed) │  price > 0 and finite · max 2 decimals
 │                                               │  no personal-data signal types
 │                                               │  no cross-tenant signals
 │                                               │  emergency mode: cap surge (price-gouging protection)
 ├───────────────────────────────────────────────┤
 │ L2  TENANT POLICY  (company admin only)       │  absolute min / max price
 │                                               │  max surge vs base (e.g. ≤ 2.0×)
 │                                               │  banned signals (gender, religion, caste…)
 │                                               │  require 2-person approval
 │                                               │  KILL SWITCH: normal | freeze | base_only
 ├───────────────────────────────────────────────┤
 │ L1  PACK GUARDS  (pricing team, per domain)   │  floor · ceiling · max_change · rounding
 └───────────────────────────────────────────────┘
```

**Enforced twice:**
- **At lint time (static):** a pack whose ceiling exceeds the tenant max, or that references a banned signal, cannot be published.
- **At runtime:** after L1, the price is clamped by L2 then L3. Every clamp is a waterfall row (`"tenant policy: capped at 2.0× base"`), so it's visible and explained.

**`formula` bounds** can't be proven by a static check (CEL arithmetic can be anything). `/verify` fuzzes it, and the guard interval clamps it at runtime anyway.

**Kill switch** (`POST /v1/admin/kill`, admin role): instantly, via Redis pub/sub, a domain switches to
- `freeze` — return the last issued price per entity (`price_state.last_price` in Postgres; `503 killed` if there is none), or
- `base_only` — return base price with guards only.

Use case: "something looks wrong at 2 AM" → one call, every replica obeys in under a second, investigate calmly.

**Linter checks** (`control/linter.py`; errors block publish): schema · compile (unknown or misspelled signals, type errors, floor > ceiling, `ending` unreachable with `step`, more than 500 rules) · **fairness**: a protected attribute (gender, religion, caste, ethnicity, disability, …) or personal data (email, customer_id, phone_number, …) can't be a signal at all; words with an innocent meaning too (age, race, health, phone, mobile, address) block **by default** — `age_group`, `child_age`, `home_address` — and pass only when the name says which thing they describe and names no person (`vehicle_age`, `race_day`, `battery_health`, `phone_model`); protected **values** are caught under any name — enum values, table keys and text compared in rules (`segment == 'female'`, `men`/`women`); a tenant can grant a named fairness exception, which becomes a warning an approver must sign off (never for personal data or a banned signal) · tenant-banned signals · pack limits outside tenant limits · a rule that always fires in an `exclusive` group. Warnings: proxy attributes (pincode, postcode), calendars about to stop working (no date in the next 12 months, or a yearly calendar ending within 6), rules that never fire or are shadowed, expired rules, missing defaults, the highest base price the pack can price, band and lookup gaps, unused signals.

**Fuzz verifier** (`control/verifier.py`): 10,000 random but valid requests generated from the pack's own specs, in about 4 s. Hypothesis runs the first 1,000 (it hunts edge values) and a seeded sweep the rest; the same pack always gets the same cases. Each price is checked **independently of the engine's self-check**: every valid input gets a price, floor ≤ price ≤ ceiling (pack and tenant), `max_change`, the waterfall adds up, same input → same output. A violation is shrunk to the smallest input that still fails (e.g. two overlapping exclusive rules → `inventory = 501`). All 10 templates are Verified Safe. Demo: `python scripts/demo.py safety`.

---

## 10. Scaling — peaks and quiet hours

### 10.1 Why it scales
`pricing-api` replicas share nothing but Redis. Any replica can serve any request. Double the replicas → roughly double the throughput.

### 10.2 Four layers of peak protection

```
① Scheduled pre-scaling  known peaks (festival sale, 9 AM) → raise minimum replicas in advance
② Autoscaling            unknown spikes → add replicas when CPU > 60% or p95 > 80 ms
③ Per-tenant limits      one company's spike can't starve others
④ Load shedding          still overloaded → 429 + Retry-After; client uses last price until valid_until
```

### 10.3 Reducing load
- `/v1/price/batch` — a listing page prices 500 rooms in one call.
- `valid_until` on every price — clients reuse it instead of re-asking.

### 10.4 Quiet hours
Scale down to **2 replicas** (high availability, no cold start). Audit worker → 1.

### 10.5 New replicas warm up before taking traffic
`/readyz` returns 503 until: (1) all live packs are compiled into memory, (2) Redis is reachable, (3) a built-in self-test price matches its expected value. The load balancer only sends traffic to ready replicas — no latency spike during scale-out.

### 10.6 Free tooling for scaling
- **Demo:** Docker Compose, `docker compose up --scale pricing-api=6` behind nginx, with Locust driving load.
- **Production design:** Kubernetes (free, open source) with the Horizontal Pod Autoscaler; KEDA (free) for schedule-based pre-scaling. Runs on any cloud or on-premise hardware.

### 10.7 Honest limits and upgrade paths
| Bottleneck | When | Upgrade (all free) |
|---|---|---|
| One Redis | ~100k ops/s | Redis Cluster, sharded by tenant |
| Audit write volume | very high sustained load | Monthly partitions (built in) → Kafka + ClickHouse |
| Python CPU per request | if measured p95 misses target | Rewrite only `engine/evaluate` in Rust/Go — packs, API and tests unchanged |

**All performance numbers are measured with Locust and published as measured — never claimed in advance.**

---

## 11. Audit, replay and tamper detection

### 11.1 Hash chain: written to Postgres in the same request

**As built (changed from the first design).** Every `/price` appends its audit row to Postgres before it
answers. Redis is **not** in the audit path.

```
record.hash = SHA-256( prev_hash + canonical_json(record) )        first row: prev_hash = 32 zero bytes
```

Editing, removing or inserting a row breaks its hash and every hash after it; `/v1/audit/verify` names the
first bad row and why.

**Why not the Redis stream + worker we first drew.** Two measured facts changed the decision:

| | Redis stream + worker (first design) | Postgres in the request (as built) |
|---|---|---|
| A price is durable | ~1 s later, and only if Redis persists to disk | before the response is sent |
| If the queue is lost | prices exist with no record | nothing to lose: there is no queue |
| Moving parts | Redis streams, leases, shards, a worker | one INSERT |
| Cost | none on the request | **~3 ms** per price (measured: p50 7 ms with audit vs 3.6 ms without) |

A hosted Redis without persistence (and 140 ms away) made the first design unsafe; a local Postgres makes the
second one cheap. "Every price is audited" is a promise, so we pay 3 ms for it.

**One chain, many writers.** One chain per `(tenant, domain)`. Appends to a chain are serialised by a Postgres
advisory lock (`pg_advisory_xact_lock`), which works across replicas: two servers and eight threads writing
200 prices produce rows 1..200 with no gap and a valid chain (tested).
Ceiling, stated openly: one append at a time per chain (~1 ms each). A single domain needing thousands of
prices per second would batch appends in a worker — the upgrade path, not built.

**If Postgres is unreachable.** The price is still returned; its record is written to a local spool file
(fsynced, one folder per database) and the response carries the flag
`audit deferred: database unreachable, record spooled`. A background thread appends spooled records, in order
and exactly once, when the database is back. Replicas with disposable disks would lose a spool on scale-in:
in Kubernetes the spool directory must be a persistent volume.

**What the chain does not catch:** deleting the newest rows (the tail). Publishing the head hash somewhere
else from time to time would; not built. And the application account cannot edit or delete audit rows at all
(no UPDATE / DELETE grant) — the chain is the second line of defence, for someone with owner access.

**What a record holds:** price_id, pack version, entity, API key id, `as_of`, the inputs exactly as received
(numbers kept as text so they come back unchanged), where each signal came from, the reference price, and
the full output. That is everything `POST /v1/replay` needs to reproduce the price.

### 11.2 Endpoints
| Endpoint | Does |
|---|---|
| `GET /v1/audit/{price_id}` | Full record: inputs, signal sources, pack version, owner, approver, waterfall |
| `POST /v1/audit/verify` | Recompute the chain; report the first broken row, if any |
| `POST /v1/replay` `{price_id}` | Re-run with the recorded inputs and version → must be identical |
| `POST /v1/replay` `{price_id, pack_version}` | Same inputs, different version → diff. Simulation only; never rewrites history |
| `POST /v1/packs/{domain}/{version}/impact` | Replay the last N real requests against a draft → % up/down/unchanged, p10/p50/p90 change, new floor/ceiling hits |
| `GET /v1/audit/export?format=csv\|json` | Tenant takes all their data, any time — no lock-in |

### 11.3 Compliance — stated honestly
We don't claim legal compliance (that's the customer's lawyers' call). We provide what makes it easy:
- **No personal data** in requests or signals (platform policy L3 blocks personal-data signal types).
- **No cross-tenant data** influencing prices.
- **A full, tamper-evident record** of how any price was set — inputs, rule version, who approved it — reproducible on demand.
- Optional `disclosure` text per tenant for customer-facing explanations.

---

## 12. Failure modes

| What fails | Pricing works? | Behaviour |
|---|---|---|
| One pricing replica | ✅ | nginx stops routing to it; replaced |
| A signal goes stale | ✅ | Per-signal `on_stale` policy; flag in response |
| Redis down | ✅ | Redis only broadcasts events (key revoked, pack published, kill switch). Without it replicas learn of those by the 60 s key-cache expiry and the 10 s pack poll instead of instantly. Nothing is stored only in Redis, so nothing is lost |
| Postgres down | ✅ | Packs are in memory, so pricing continues. API keys this replica has already seen keep working (unseen keys: 503 `auth_unavailable`). Audit rows go to a local spool and are appended when Postgres returns (response flag `audit deferred`). Publish, rollback, audit queries and replay pause |
| Bad rule published | ✅ | Lint + verify + impact preview should stop it; guards clamp it anyway; kill switch; one-call rollback |
| New pack fails to load | ✅ | Replica keeps last known good version, raises alert |
| Engine bug | ✅ no wrong price | Self-check fails → `500 invariant_violation`, no price returned |
| Pack rule fails at runtime | ✅ no wrong price | e.g. formula ÷ 0 → `500 rule_error` naming the rule; fix the pack or roll back |
| Request outside what the pack can price | ✅ no wrong price | e.g. base too high for floor/ceiling → `422 bounds_conflict` |
| Whole service unreachable | client-side | Client uses last price until `valid_until`, then its own fallback |

### 12.1 Known limits (stated, not hidden)

Things a real industry may need that a pack **can't express yet** — each is an additive change, none needs a
redesign:

| Limit | Today | Planned fix |
|---|---|---|
| Prices below 0.01 (per-second cloud, per-token, KWD's 3 decimals) | Refused with a clear error, never rounded to 0.00 | Pack field `precision: 0..6` driving rounding and the self-check |
| Free items (`price: 0`, 100% promo) | Floor is at least 0.01 | `guards.allow_free: true` |
| Operator input from an expression (`piecewise_curve` on `demand / supply`) | Caller sends the ratio as a signal | `input: "<expr>"` instead of `signal:` |
| CEL ternary `c ? a : b` | Use `min`/`max` or two rules | Rewrite to a conditional before parsing, same-type branches |

---

## 13. Demo script (6 minutes, all live, all free tools)

| Time | Scene | Proves |
|---|---|---|
| 0:00 | Swagger `/docs`: price a hotel room → waterfall + counterfactual | Explainability |
| 0:45 | Change occupancy in `/simulate` → watch rules switch on/off | Correctness, determinism |
| 1:30 | Draft a rule with a typo + a ceiling above tenant max → `/lint` rejects both | Safety at authoring time |
| 2:00 | `/verify` → "10,000 cases, 0 violations — Verified Safe" | Provable bounds |
| 2:30 | `/impact` → "this change raises 34% of prices, median +6%" → approve → publish | Real-company workflow |
| 3:15 | Add **airline** pack (fare buckets) → price it → show `git diff engine/` is **empty** | Domain-agnostic |
| 4:00 | Hotel key tries to price airline → `404` | Tenant isolation |
| 4:20 | `docker stop postgres` → keep pricing → it works | Resilient architecture |
| 4:45 | Edit an audit row by hand in psql → `/audit/verify` → "chain broken at row 1,042" | Tamper evidence |
| 5:15 | `/replay` an old `price_id` → identical | Reproducibility |
| 5:30 | Locust: scale 1 → 4 replicas, show req/s and p95 | Scalability (measured) |
| 6:00 | Close on the scoreboard (`GET /v1/scoreboard`, JSON) | Every KPI, measured |

Fallback: every scene has a pre-recorded terminal capture and a pre-validated pack.

---

## 14. Tech stack (all free and open source)

| Layer | Choice | License |
|---|---|---|
| Language | Python 3.12 | PSF |
| API | FastAPI + Uvicorn + Pydantic v2 | MIT / BSD |
| Expressions | CEL syntax, in-house exact-Decimal evaluator (`engine/expr.py`) | — |
| Packs | PyYAML + jsonschema | MIT |
| Money | `decimal` (stdlib) | — |
| Database | PostgreSQL 16 | PostgreSQL License |
| Cache / queue / events | Redis 7 (or Valkey, a fully open-source drop-in) | free to self-host |
| Load balancer | nginx | BSD |
| Tests | pytest + Hypothesis | MIT / MPL-2.0 |
| Load test | Locust | MIT |
| Metrics | prometheus-client | Apache-2.0 |
| Calendar signals | built-in festival table (`engine/publicsignals.py`) | — |
| Packaging | Docker + Docker Compose | Apache-2.0 |
| Production design | Kubernetes + HPA, KEDA | Apache-2.0 |

**No paid APIs. No cloud account required. No AI/LLM anywhere. Runs fully offline on one laptop.**

Why no AI: every price must be deterministic and explainable. An LLM adds non-determinism, an external dependency and a "can the AI be trusted?" question — with nothing the rules engine needs. Templates + schema autocomplete give non-engineers a fast authoring path without it.

---

## 15. Why Python — and not C/C++  *(answers Q7)*

### Speed
- Our engine does ~1–3 ms of work per price. The network round-trip to the customer is 10–50 ms. A 20× faster engine would make the **total** response only a few percent faster.
- We scale by adding replicas (section 10), not by making one CPU faster.
- If measurement ever shows the engine is the bottleneck, we rewrite **only** `engine/evaluate` in Rust or Go; packs, API and tests stay the same. Optimize what's measured, not what's guessed.

### Security
- "Python isn't secure" is a myth worth correcting. Most real vulnerabilities come from **application mistakes** — `eval`, injection, weak auth — which we've designed out (CEL, parameterized SQL, hashed scoped keys).
- **C/C++ is the less safe choice:** manual memory management causes buffer overflows and use-after-free bugs. Microsoft and Google have each reported that roughly 70% of their serious security bugs are memory-safety bugs. Python, Go, Java and Rust are memory-safe.
- Python's `decimal` gives exact money math out of the box; C/C++ would need a third-party library.

### Practicality
- 12 hours, AI-assisted coding: AI tools are most reliable in Python; FastAPI generates the API docs we demo with for free.
- Pricing platforms in industry commonly use Python and Java for exactly these reasons.

**Verdict:** Python for the whole system. Rust/Go is the documented upgrade path for one hot function, if and only if measured.

---

## 16. Data model

```sql
tenants        (id, name, created_at)
tenant_policy  (tenant_id, min_price, max_price, max_surge_x, banned_signals[],
                require_two_person, kill_mode)
api_keys       (key_id, tenant_id, secret_sha256, role, domains[], rate_limit_rps,
                expires_at, revoked_at, created_at)
packs          (tenant_id, domain, version, status, yaml, compiled, owner, approver,
                lint_report, verify_report, created_at)   PK (tenant_id, domain, version)
pack_pointer   (tenant_id, domain, live_version)           -- rollback = change this
audit_log      (price_id, tenant_id, domain, seq, pack_version, entity, as_of,
                inputs, signal_sources, reference_price, output,   -- output includes the waterfall
                prev_hash, hash, created_at)               PARTITION BY RANGE (created_at)
```

Exact DDL, grants and RLS policies: `deploy/postgres/init/002_schema.sql`. Idempotency results (`idempotency_keys`), anchors and
last prices (`price_state`) live in Postgres, tenant-isolated by the same RLS policy.

- Row-Level Security on every table by `tenant_id`.
- The app's DB user has **no UPDATE/DELETE** on `audit_log` or published `packs`.
- Audit retention default 90 days — drop old monthly partitions instantly.

---

## 17. API surface

| Method | Path | Role |
|---|---|---|
| POST | `/v1/price/{domain}` | pricing |
| POST | `/v1/price/{domain}/batch` | pricing |
| POST | `/v1/simulate/{domain}` | pricing |
| GET | `/v1/audit/{price_id}` · POST `/v1/audit/verify` · GET `/v1/audit/export` | admin |
| POST | `/v1/replay` | admin |
| GET/PUT | `/v1/packs/{domain}` (list versions / save draft) · GET `/v1/packs/{domain}/{version}` | editor |
| POST | `/v1/packs/{domain}/{version}/lint` · `/verify` · `/impact` | editor |
| POST | `/v1/packs/{domain}/{version}/approve` | approver (≠ author) |
| POST | `/v1/packs/{domain}/{version}/publish` · `/v1/packs/{domain}/rollback` | approver |
| POST/PUT | `/v1/admin/kill` · `/v1/admin/keys` · `/v1/admin/keys/{id}/revoke` · PUT `/v1/admin/policy` | admin |
| GET | `/v1/scoreboard` · `/healthz` · `/readyz` · `/metrics` | public / ops |

Exact request/response JSON: `docs/API.md`. OpenAPI docs auto-generated at `/docs`.

---

## 18. 12-hour build plan

**Rule: humans write golden tests; AI writes code until they pass. Feature freeze at hour 10.**

| Hours | Build | Done when |
|---|---|---|
| 0–1 | Repo, Docker Compose (api, postgres, redis, nginx), pack JSON Schema, hotel pack, **15 golden cases by hand** | `docker compose up` works |
| 1–4 | Engine: signal resolution, operators, 5 stacking policies, guards, rounding, waterfall + self-check, counterfactuals | golden tests 100% |
| 4–6 | API: keys (hash, scope, cache, revoke), tenants, `/price`, `/simulate`, publish/rollback with pub/sub reload, audit stream + worker + hash chain | hotel priced over HTTP with audit row |
| 6–8 | Linter, `/verify` (Hypothesis fuzz), 3-layer guardrails + kill switch, `/replay`, `/impact`, `/audit/verify`; airline + ride packs | demo scenes 1–5 & 8–9 work |
| 8–9.5 | Locust + scale test, Postgres-down test, `/scoreboard`, README | measured numbers on scoreboard |
| **10** | **Feature freeze** | |
| 10–12 | Rehearse demo twice, record fallback captures, finalize this doc with measured numbers | |

**Team split (3 people):** A — engine, linter, verifier, replay/impact · B — API, keys, tenancy, Redis/Postgres, audit worker, Docker, Locust · C — packs, golden cases, demo, docs. Details and hour-by-hour plan: `docs/BUILD_GUIDE.md`; ready-made prompts: `docs/PROMPTS.md`.

### Explicitly out of scope (and why)
React UI (mentor: backend judged) · AI/LLM copilot (determinism, no paid/external dependency) · A/B price experiments (needs conversion data; ethics questions) · billing/usage metering (business model, not architecture) · multi-language SDKs (OpenAPI generates clients) · Kafka, service mesh, multi-region (no need at this scale; upgrade paths documented).

---

## 19. Scoreboard (fill with measured values)

| KPI | Metric | Target | Measured |
|---|---|---|---|
| Domain-agnostic | domains live / engine lines changed for new domain | 3+ / 0 | |
| Flexibility | operators / stacking policies / guard layers | 11 / 5 / 3 | |
| Correctness | golden pass rate · replay identical · waterfall self-check failures | 100% · 100% · 0 | |
| Safety | fuzz cases · violations · seeded bad rules caught by linter | 10,000 · 0 · 100% | |
| Performance | p95 engine / API · req/s per replica | < 50 ms / < 150 ms · measure | |
| Resilience | pricing success with Postgres down | 100% | |
| Integrity | tampered audit rows detected | 100% | |
