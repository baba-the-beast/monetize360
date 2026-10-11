# AI Prompts — one section per person

Each person gets: **files to attach** · **a learning prompt** (understand your part) · **build prompts**
(one per task, in order). If you use an AI that works inside the repo (Claude Code, Cursor, Copilot),
it reads `AGENTS.md` automatically — you don't need to attach files, just paste the prompt.

**Rules for everyone**
- One task per prompt. Always name the spec section and the test that proves it.
- Paste test failures back to the AI as-is. Never let it "fix" a golden test by changing the expected value.
- The docs are the truth. If the AI suggests something not in them (Kafka, an ORM, AI pricing, floats), say no.

---

## Person A — Engine

### Attach
`AGENTS.md` · `docs/ENGINE_SPEC.md` · `docs/PACK_SCHEMA.md` · `packs/schema/pack.schema.json` ·
`packs/templates/hotel.yaml` · `packs/templates/airline.yaml` · `tests/golden/hotel.yaml` ·
`tests/golden/airline.yaml` · `tests/golden/test_golden.py`

### Learning prompt
```
I'm building the pricing engine of "Monetize360" in Python for a 12-hour hackathon.
The attached docs are the source of truth; ENGINE_SPEC.md defines the exact math.

Before I write code, teach me the engine:
1. Walk through hotel golden case 1 step by step: base, each rule, guards, rounding, self-check.
2. Explain each stacking policy (multiply, add, max, first_match, exclusive) with a small example.
3. Explain why every delta is quantized and the running price is the sum of deltas.
4. Explain the guard interval (hard vs soft, which limit wins) using airline golden case 3.
5. Explain rounding with ending 99 using airline golden case 2.
6. Explain why the engine must be pure (no clock, no DB, no randomness).
Then quiz me with 5 questions. Don't invent anything that isn't in the docs.
```

### Build prompts (in order)
```
A1. Implement src/monetize360/engine/models.py: Context, Result, Row and the error classes
    exactly as ENGINE_SPEC.md §1. Use Decimal, never float. Export them from engine/__init__.py.
```
```
A2. Implement control/schema.py: validate(pack_dict) against packs/schema/pack.schema.json (path from
    config.PACK_SCHEMA_PATH) and return every error with its path. Then engine/compiler.py:
    compile_pack(dict) assumes a schema-valid pack and reads NO files; converts numbers with
    Decimal(str(x)), compiles rule expressions once with engine/expr.py (bad CEL -> PackInvalid), sorts
    bands/curve points. Spec: ENGINE_SPEC.md §1, §2.
```
```
A3. Implement engine/signals.py and engine/publicsignals.py per ENGINE_SPEC.md §3.
    Write tests in tests/unit for: missing signal -> default + flag, out of range -> SignalInvalid,
    public override rejected unless allow_public_override.
```
```
A4. Implement engine/operators.py and engine/stacking.py per ENGINE_SPEC.md §4, §5.
    Fill tests/unit/test_operators.py and test_stacking.py, including 4950 (multiply) vs 4650 (add).
```
```
A5. Implement engine/guards.py (§6) and engine/rounding.py (§7). Fill test_guards.py and
    test_rounding.py: 4125 -> 4150 half_up, ending 99, rounding never leaves the guard interval.
```
```
A6. Implement engine/evaluate.py: the full pipeline plus the self-check from ENGINE_SPEC.md §8.
    Done when `pytest tests/golden` passes 100%. Do not modify any file in tests/golden/.
```
```
A7. Implement engine/explain.py (§9: counterfactuals by re-running evaluate with a rule disabled),
    tests/unit/test_engine_is_pure.py, and tests/property/test_invariants.py with Hypothesis:
    bounds hold, waterfall sums, same input -> same output, counterfactual == fresh evaluate.
```
```
A8. Implement control/linter.py: the checks listed in ARCHITECTURE.md §9 (unknown signals via CEL
    type-check, floor > ceiling, ceiling above tenant max, banned signals, unreachable rules,
    exclusive groups that can double-fire, unsorted bands, more than 500 rules in the pack,
    ending unreachable with step: need 100 % step == 0 and ending % step == 0 — see
    PACK_SCHEMA.md "Checked by the linter"). Return {ok, errors, warnings}. Write one test per
    check in tests/unit/test_linter.py; hotel.yaml and airline.yaml must lint clean.
```
```
A9. Implement control/verifier.py: Hypothesis generates N random contexts from each pack's signal
    specs; assert bounds, max_change and the self-check always hold. Return
    {ok, cases, violations[first 10], badge}.
```
```
A10. Implement control/replay.py and control/impact.py per docs/API.md (Audit & replay, Packs /impact).
     They take audit records as plain dicts — B's store/audit.py fetches them.
```

---

## Person B — Platform

### Attach
`AGENTS.md` · `docs/ARCHITECTURE.md` · `docs/API.md` · `docs/TECH_STACK.md` · `docs/ENGINE_SPEC.md` (§1 only matters) ·
`docker-compose.yml` · `Dockerfile` · `deploy/nginx/nginx.conf` · `deploy/postgres/init/002_schema.sql` ·
`.env.example` · `pyproject.toml`

### Learning prompt
```
I'm building the platform around the pricing engine of "Monetize360" (FastAPI, Postgres, Redis,
nginx, Docker) for a 12-hour hackathon. The attached docs are the source of truth.

Before I write code, teach me:
1. The hot path vs control path, and why pricing never touches Postgres.
2. How API keys work here: format, SHA-256 storage, scopes, 60s cache, instant revocation via pub/sub.
3. How tenant isolation works in 3 layers: key -> tenant, (tenant, domain) lookup, Postgres RLS.
   Why the app uses a separate DB role and why lookup_api_key() is SECURITY DEFINER.
4. How hot reload works: publish -> Redis pub/sub -> every replica swaps its in-memory pack.
5. How the audit worker builds the hash chain from a Redis Stream and why only one writer per chain.
6. How max_change anchoring works (ENGINE_SPEC §6.2) and where B stores the anchor.
Then quiz me with 5 questions. Don't invent anything that isn't in the docs.
```

### Build prompts (in order)
```
B1. Make `docker compose up --build` work: Postgres runs the init scripts, Redis, nginx, and a
    pricing-api with /healthz. Fix anything in the compose/nginx/SQL files that doesn't start.
```
```
B2. Implement api/app.py (create_pricing_app, create_control_app), api/schemas.py, api/errors.py
    and api/routes/price.py + health.py per docs/API.md. Until the real engine lands, use a FAKE
    evaluate() that returns the exact /v1/price example response from API.md.
```
```
B3. Implement security/keys.py, security/principal.py, store/db.py, store/api_keys.py and
    api/deps.py per ARCHITECTURE.md §4: key format, SHA-256, hmac.compare_digest, 60s cache,
    evict on Redis "key_revoked". Every DB transaction runs SET LOCAL app.tenant_id.
    Then scripts/seed_demo.py: tenants hotel_001 and airline_002, one key per role.
```
```
B4. Write tests/integration/test_isolation.py: the hotel key gets 404 on airline for price, packs
    and audit; a pricing key gets 403 on PUT /v1/packs; a revoked key gets 401.
```
```
B5. Implement store/packs.py, store/packcache.py, store/cache.py, control/publisher.py and
    api/routes/packs.py: draft -> approve (different key owner) -> publish -> rollback.
    Publishing sends Redis pub/sub; every replica swaps the pack atomically; 10s poll as backup;
    keep last-known-good if compile fails.
```
```
B6. Wire the real engine into /v1/price: resolve the max_change anchor from Redis (ENGINE_SPEC §6.2),
    call evaluate() + explain(), XADD the audit event to a Redis Stream, return the API.md response.
```
```
B7. Implement workers/audit_worker.py and store/audit.py: consume the stream, chain
    hash = sha256(prev_hash + canonical_json(record)) per (tenant, domain), batch INSERT, ACK.
    Routes: GET /v1/audit/{id}, POST /v1/audit/verify, GET /v1/audit/export. Plus
    tests/integration/test_tamper.py.
```
```
B8. Implement api/routes/admin.py: kill switch (normal|freeze|base_only via pub/sub), tenant policy,
    key create/revoke. Pass tenant policy into evaluate() as ENGINE_SPEC §6 describes.
```
```
B9. Write bench/locustfile.py for /v1/price. Run with 1, 2 and 4 pricing replicas and record req/s
    and p50/p95/p99. Then stop Postgres and confirm /v1/price still returns 200.
```

---

## Person C — Packs, Tests & Demo (learner)

### Attach
`docs/ARCHITECTURE.md` · `docs/ENGINE_SPEC.md` · `docs/TECH_STACK.md` · `docs/PACK_SCHEMA.md` · `docs/API.md` ·
`docs/BUILD_GUIDE.md` · `packs/templates/hotel.yaml` · `packs/templates/airline.yaml` ·
`tests/golden/hotel.yaml` · `tests/golden/airline.yaml`

### Learning prompt
```
I'm the learner/tester on a 3-person team building "Monetize360", a universal dynamic pricing
engine, in a 12-hour hackathon. Judges will grill us on the architecture and I'll present it.
The attached docs are the source of truth.

Teach me the project lesson by lesson:
1. The journey of one price request  2. Domain Packs and the schema  3. Engine math
4. Explainability  5. Safety (schema, linter, fuzz, 3 guard layers, CEL)  6. Security
7. Postgres vs Redis  8. Audit, replay, tamper detection  9. Scaling and peaks
10. Failure handling  11. Testing strategy  12. Hard judge questions

For every part: what problem it solves, what we chose, why, and why NOT the alternatives.
Use the hotel numbers from the docs. Simple language. End each lesson with 3 quiz questions
and wait for my answers. If something isn't in the docs, say so. Don't invent features.
```

### Task prompts
```
C1. Help me write more golden cases for tests/golden/hotel.yaml, following ENGINE_SPEC.md exactly:
    max_change clamp up, max_change clamp down, floor clamp, missing occupancy -> default 0.5,
    weekday-only, rounding near the ceiling. For each case show every arithmetic step as a comment,
    in the same format as the existing cases. I will recheck every number by hand.
```
```
C2. Design a Domain Pack for a parking lot (and one for EV charging) using only the operators in
    PACK_SCHEMA.md. Realistic Indian prices. Then write 5 hand-calculated golden cases for each,
    with arithmetic comments, in the format of tests/golden/airline.yaml.
```
```
C3. Act as a strict hackathon judge who knows pricing systems (PROS, Pricefx, airline revenue
    management). Ask me 15 hard questions about this architecture, one at a time. After each
    answer, tell me what was weak and give a better answer using only the docs.
```
```
C4. Write a 6-minute demo script from ARCHITECTURE.md §13: exact words to say, which screen to
    show, and what to do if each step fails. Keep claims honest — only say what we measured.
```

**C — always double-check AI arithmetic by hand.** AI tools often make small mistakes in multi-step
decimal math, and a wrong golden case is worse than a missing one.
