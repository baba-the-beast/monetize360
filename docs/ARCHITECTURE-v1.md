# Monetize360 — Architecture

> A universal dynamic pricing engine. Companies define pricing rules as config ("Domain Packs"),
> call one API, and get back a price that is explainable, safe, reproducible and audited.

Status: **DRAFT — to be finalized by the team.** Open decisions are listed in section 12.

---

## 1. Design goals

| Goal | What it means in practice |
|---|---|
| **Any domain, no code** | A new industry = a new pack file. The `engine/` folder never changes. |
| **Fast** | Pricing path p95 < 50 ms inside the engine, < 150 ms at the API. |
| **Elastic** | Handles a sudden 10x spike (festival sale, 9 AM rush) and shrinks at night to save cost. |
| **Never wrong, never rogue** | Exact decimal math, guardrails on every price, same input → same output. |
| **Explainable & auditable** | Every price returns its breakdown and leaves a tamper-evident record. |
| **Not a second database** | We store rules and receipts. The company keeps its own catalog, inventory and customers. |
| **Multi-tenant** | Many companies on one deployment, fully isolated from each other. |

### The one principle everything follows

**Keep the request path (pricing) tiny, stateless and in-memory. Push everything slow (saving audit, publishing rules, heavy queries) off the request path.**

Every scaling decision below follows from that one rule.

---

## 2. Big picture

```
                              COMPANIES' SYSTEMS
              (hotel backend, airline backend, e-commerce server ...)
                                     │  HTTPS + API key
                                     ▼
                       ┌───────────────────────────┐
                       │   Load Balancer / Gateway  │  TLS, rate limit per tenant,
                       │                           │  routes /v1/price vs /v1/packs
                       └─────────────┬─────────────┘
                 ┌───────────────────┴────────────────────┐
                 ▼                                        ▼
 ┌───────────────────────────────┐        ┌───────────────────────────────┐
 │  DECISION PLANE  (hot path)   │        │  CONTROL PLANE  (cold path)   │
 │  pricing-api  × N replicas    │        │  control-api  × 2 replicas    │
 │  - stateless, autoscaled      │        │  - pack CRUD, versions        │
 │  - compiled packs IN MEMORY   │        │  - lint, verify (fuzz)        │
 │  - /price /batch /simulate    │        │  - approve, publish, rollback │
 └───────┬──────────────┬────────┘        │  - copilot (Gemini)           │
         │              │                 │  - audit queries              │
   reads │       writes │ (async)         └───────┬───────────────┬───────┘
         ▼              ▼                         │               │
 ┌──────────────────────────────┐                 │ publish event │
 │  REDIS                       │◄────────────────┘               │
 │  - live signals (with TTL)   │                                 │
 │  - last price per entity     │                                 │
 │  - rate-limit counters       │                                 ▼
 │  - audit stream (buffer)     │                 ┌───────────────────────────────┐
 │  - "pack published" pub/sub  │                 │  POSTGRES                     │
 └──────────────┬───────────────┘                 │  - tenants, api_keys          │
                │ consume                         │  - pack versions (immutable)  │
                ▼                                 │  - audit log (append-only,    │
 ┌──────────────────────────────┐   batch insert  │    hash-chained, partitioned) │
 │  audit-worker × 1..M         │────────────────►│  + read replica for queries   │
 │  - hash-chains records       │                 └───────────────────────────────┘
 │  - writes in batches         │
 └──────────────────────────────┘

 Signal ingestors (optional): pull company feeds / simulators → write signals into Redis with TTL
 Observability: Prometheus metrics + OpenTelemetry traces + structured logs from every service
```

**Three deployable services, one codebase:** `pricing-api`, `control-api`, `audit-worker`.
Not microservice sprawl: they share the same `engine/` package and differ only in which routes/loops they run.

---

## 3. The hot path — one price request, step by step

```
POST /v1/price
{ "domain": "hotel", "entity": "deluxe",
  "base_price": 3000,
  "signals": { "occupancy": 0.85, "is_weekend": true, "festival": "diwali" },
  "idempotency_key": "booking-8812" }
```

| # | Step | Where data comes from | Cost |
|---|---|---|---|
| 1 | Authenticate API key → `tenant_id` | in-memory key cache (refreshed every 60 s) | ~0 ms |
| 2 | Rate-limit check | Redis token bucket | ~1 ms |
| 3 | Load compiled pack for (tenant, domain) | **process memory** — never the DB | ~0 ms |
| 4 | Resolve signals: request → Redis (if provider-fed) → default | request / Redis | 0–1 ms |
| 5 | Evaluate: base → adjust → tier/discount | pure CPU, `Decimal` math | ~1 ms |
| 6 | Guards: floor, ceiling, **max change** (needs last price) | Redis `last_price:{tenant}:{entity}` | ~1 ms |
| 7 | Round, build waterfall + counterfactuals | pure CPU | ~1 ms |
| 8 | Generate `price_id` (ULID), push audit record to Redis Stream | Redis (fire-and-forget) | ~1 ms |
| 9 | Respond | — | — |

Response:
```json
{ "price_id": "01JA7Z...", "price": "4950.00", "currency": "INR",
  "pack_version": 3, "valid_until": "2026-10-11T10:15:00Z",
  "waterfall": [ {"step":"base","value":"3000.00"},
                 {"rule":"high-occ","op":"pct","value":"+20%","after":"3600.00"}, ... ],
  "counterfactuals": [ {"if":"festival = none","price":"3950.00"} ],
  "flags": [] }
```

**Postgres is never touched on the hot path.** That is what makes it fast and what lets it survive a DB outage.

### Why the request carries `base_price` and signals
The company already knows its base price and its occupancy. Sending them in the request means:
- we don't keep a copy of their catalog (not a "second database"),
- no lookups → lower latency,
- the price is fully determined by the request + pack version → perfectly replayable.

Signals the company *can't* easily send (competitor rates, weather, festival calendar) come from **signal ingestors** that write into Redis with a TTL. If a signal is stale or missing, the engine uses the pack's default and adds a flag: `"flags": ["competitor signal stale, used default"]`.

---

## 4. Scaling — handling peaks and quiet hours

### 4.1 Why it scales horizontally
`pricing-api` replicas share **nothing** except Redis. Any replica can answer any request.
Double the replicas → roughly double the throughput. No sticky sessions, no coordination.

### 4.2 Autoscaling policy

| Setting | Value (initial, tune after load test) | Why |
|---|---|---|
| Min replicas | **2** | High availability even at 3 AM; no cold start on the first request |
| Max replicas | 50 | Cost ceiling |
| Scale-out trigger | CPU > 60 % **or** p95 latency > 80 ms for 1 min | Pricing is CPU-bound; scale before users feel it |
| Scale-in trigger | CPU < 30 % for 10 min | Slow scale-in avoids flapping |
| Scale-out step | up to +100 % per minute | Doubling handles sudden spikes fast |

### 4.3 The four layers of peak protection

```
 Spike arrives
     │
     ▼
 ① Scheduled pre-scaling   ── known peaks (festival sale, 9 AM, flash sale) → raise min replicas in advance
     │
     ▼
 ② Autoscaling             ── unknown spikes → add replicas within ~1 min
     │
     ▼
 ③ Per-tenant rate limits  ── one company's spike can't starve others (noisy-neighbour protection)
     │
     ▼
 ④ Load shedding           ── if still overloaded: reject excess with 429 + Retry-After,
                              never slow down everyone. Clients fall back to last price (valid_until)
```

### 4.4 Reducing load in the first place
- **`/v1/price/batch`** — listing pages price 500 rooms in one call instead of 500.
- **`valid_until` on every price** — clients may reuse a price until it expires instead of asking again.
- **Determinism enables caching** — same (pack version, inputs) always gives the same price, so a short-TTL response cache is safe if ever needed. *(Not built initially; add if load tests show repeated identical requests.)*

### 4.5 Quiet hours
Replicas scale down to the minimum (2). Audit workers scale down to 1. Postgres and Redis stay up (they're small).
On serverless platforms (e.g. Cloud Run) idle cost is close to zero beyond the minimum instances.

### 4.6 Capacity estimate (to be replaced with measured numbers)
- One evaluation ≈ 1–3 ms CPU in Python → roughly 300–1,000 req/s per CPU core.
- Target: **1,000+ req/s per node**, linear growth with replicas.
- **Measure with Locust before claiming anything.** Publish measured p50 / p95 / p99 and req/s on the scoreboard.

### 4.7 Where it stops scaling (and the upgrade path)
| Bottleneck | When it bites | Upgrade |
|---|---|---|
| Single Redis | ~100k ops/s | Redis Cluster (shard by tenant) |
| Single Postgres writer for audit | very high sustained volume | Monthly partitions (already planned) → then Kafka + columnar store (ClickHouse) for audit |
| Python CPU per request | latency target missed | Profile; move evaluator hot loop to a compiled extension. Not before measured |

---

## 5. Control plane — how rules go live safely

```
 draft ──► lint ──► verify (fuzz) ──► approve ──► publish ──► (rollback any time)
```

1. **Draft** — `PUT /v1/packs/{domain}` saves a new *draft* version (never edits a published one).
2. **Lint** — static checks: schema valid, unknown signals, floor > ceiling, overlapping/contradicting/unreachable rules, banned attributes (gender, religion, caste…), missing defaults, unbounded formulas.
3. **Verify** — property-based fuzzing (Hypothesis): thousands of random signal combinations; proves `floor ≤ price ≤ ceiling` and max-change always hold. Pass → "Verified Safe" badge.
4. **Approve** — a second person (role `approver`) signs off. Owner + approver recorded.
5. **Publish** — pack is **compiled** once (expressions parsed, operators bound, lookup tables indexed) into an immutable bundle stored in Postgres. A `pack_published` event goes out on Redis pub/sub.
6. **Hot reload** — every `pricing-api` replica receives the event and swaps the in-memory bundle atomically. Safety net: replicas also poll the "current version" every 10 s in case an event was missed.
7. **Rollback** — `POST /rollback` re-points "current" to the previous version; same event mechanism. Takes effect in seconds.

**Automatic fallback:** if a newly published bundle fails to load on a replica, the replica keeps serving the **last known good** version and raises an alert.

---

## 6. Data model (Postgres)

```
tenants        (id, name, plan, rate_limit_rps, created_at)
api_keys       (id, tenant_id, key_hash, role, created_at, revoked_at)   -- only the hash is stored
packs          (tenant_id, domain, version, status[draft|published|archived],
                yaml, compiled_bundle, owner, approver, lint_report, verify_report,
                created_at)                       PK (tenant_id, domain, version)
pack_pointer   (tenant_id, domain, current_version)          -- what "live" means; rollback = update this
audit_log      (price_id, tenant_id, domain, pack_version, entity,
                inputs_json, output_json, prev_hash, hash, created_at)
                PARTITION BY RANGE (created_at)  -- one partition per month
```

- **Tenant isolation:** every table has `tenant_id`; Postgres Row-Level Security enforces it as a second line of defence.
- **Immutability:** published pack versions and audit rows are never updated. The app DB user has no `UPDATE`/`DELETE` permission on `audit_log`.
- **Retention:** old audit partitions dropped/archived after the tenant's retention period (e.g. 90 days default) — dropping a partition is instant.
- **No personal data:** price requests carry no names, phones or emails. Nothing to leak.

---

## 7. Audit — tamper-evident without slowing pricing

**Problem:** a hash chain means each record contains the hash of the previous one. If 50 replicas all try to append at once, they must coordinate — that would kill latency.

**Solution:** the chain is built by the **audit-worker**, not by the pricing API.

```
pricing-api ──XADD──► Redis Stream "audit:{partition}" ──► audit-worker
                                                            - reads in order
                                                            - hash = sha256(prev_hash + record)
                                                            - batch INSERT 500 rows at a time
```

- One chain per tenant (partitioned so tenants don't block each other).
- Verifying integrity: recompute hashes along the chain; any edited row breaks every hash after it.
- **Trade-off (stated openly):** there's a window of ~1 second between "price returned" and "audit row durable". Redis runs with append-only persistence to keep that loss risk tiny. For tenants that need strict guarantees, a **synchronous audit mode** writes to Postgres before responding (slower, opt-in).

---

## 8. Failure modes — what happens when things break

| What fails | Pricing keeps working? | Behaviour |
|---|---|---|
| One `pricing-api` replica | ✅ | Load balancer stops routing to it (`/readyz` fails), autoscaler replaces it |
| A signal feed is stale | ✅ | Pack default used, flag added to the response |
| Redis down | ✅ (degraded) | Packs are in memory; provider signals → defaults; max-change guard uses in-memory last price; audit buffered to local disk and replayed later; rate limiting falls back to per-replica limits |
| Postgres down | ✅ | Hot path never reads it. Publishing, rollback and audit queries pause; audit waits in Redis |
| Bad pack published | ✅ | Lint + verify should stop it; if not, guards clamp the price; one-call rollback |
| Whole service unreachable | — on our side | Client uses its last price until `valid_until`, then its own fallback price. Documented in the integration guide |
| Gemini API down | ✅ | Copilot is control-plane only; pricing never depends on AI |

---

## 9. Security

- **HTTPS only**; API keys sent as `Authorization: Bearer`, stored as hashes.
- **Roles:** `pricing` (call price APIs), `editor` (draft packs), `approver` (approve/publish), `admin`.
- **Server-to-server only:** keys must not be shipped inside mobile apps or browsers — otherwise customers could forge signals (e.g. `occupancy: 0`) for a cheaper price. Stated in the integration guide.
- **Sandboxed expressions:** rule conditions use CEL (no `eval`, no loops, no I/O).
- **Fairness:** linter blocks rules that reference sensitive attributes.
- **Input validation:** every signal checked against its declared type and range; out-of-range → reject or clamp per pack setting.

---

## 10. Observability

| Signal | Tool | What we watch |
|---|---|---|
| Metrics | Prometheus `/metrics` | req/s, p50/p95/p99 latency per tenant, error rate, guard hits (floor/ceiling clamps), stale-signal count, audit lag |
| Traces | OpenTelemetry | per-request span: auth → resolve → evaluate → guard → audit |
| Logs | structured JSON | always include `price_id`, `tenant_id`, `pack_version` |
| Health | `/healthz` (alive), `/readyz` (pack loaded, Redis reachable) | used by load balancer and autoscaler |

**Business alerts** (not just technical): spike in ceiling clamps, a tenant's average price moving > X % in an hour (anomaly), audit lag > 10 s.

---

## 11. Tech choices

| Layer | Choice | Why | Rejected alternative |
|---|---|---|---|
| Language / API | Python 3.12, FastAPI, Pydantic | Fast to build, auto OpenAPI docs, team knows it | Go (faster, but slower to build in a hackathon) |
| Money math | `decimal.Decimal` | No float rounding errors | float |
| Rule expressions | `cel-python` | Sandboxed, deterministic | Python `eval` (unsafe) |
| Hot cache / queue | Redis 7 (Streams, pub/sub, TTL keys) | One tool covers signals, rate limits, audit buffer, events | Kafka — too heavy until volume demands it |
| Database | Postgres 16 (JSONB, partitioning, RLS) | Versioning, audit, isolation in one battle-tested DB | MongoDB (weaker for append-only audit + RLS) |
| AI copilot | Gemini Flash, structured JSON output | Drafts packs only; output must pass lint | AI setting prices (non-deterministic, unsafe) |
| Testing | pytest, Hypothesis | Golden tests + fuzz verification | — |
| Load testing | Locust | Python, matches stack | — |
| Packaging | Docker; Docker Compose locally | `docker compose up` runs everything | — |
| Production target | Kubernetes (HPA) **or** Cloud Run / ECS Fargate | Autoscaling on CPU/latency, min/max replicas, scheduled scaling | VMs with manual scaling |

### Deliberately skipped (and when we'd add them)
- **Kafka** — add when audit volume outgrows Redis Streams + Postgres.
- **Microservices per operator/domain** — never; domains are config, not services.
- **Service mesh, multi-region** — add when a customer needs cross-region latency/compliance.
- **ML-set prices / reinforcement learning** — ML may feed a `demand_index` *signal*; rules always decide.

---

## 12. Decisions to finalize

| # | Decision | Recommended | Alternatives |
|---|---|---|---|
| 1 | Production platform to describe | **Kubernetes + HPA** (most familiar to judges) | Cloud Run (simplest, scale-to-near-zero) |
| 2 | Audit write mode default | **Async via Redis Stream** | Sync to Postgres (simpler, slower) |
| 3 | Base price source | **Sent in request** | Stored catalog in our DB (becomes "second database") |
| 4 | Pack hot-reload | **Redis pub/sub + 10 s poll** | Poll only (simpler, up to 10 s delay) |
| 5 | Approval step | **Required for publish** | Optional per tenant |
| 6 | Over-limit behaviour | **429 + Retry-After** | Queue requests (adds latency for everyone) |
| 7 | Default audit retention | **90 days** | Per-tenant setting |

---

## 13. Hackathon build vs production

| Component | Hackathon (demo) | Production |
|---|---|---|
| pricing-api | 3 replicas in Docker Compose behind nginx | K8s deployment + HPA |
| control-api | 1 container | 2 replicas |
| audit-worker | 1 container | 1..M, per partition |
| Redis | 1 container | Managed Redis (HA) |
| Postgres | 1 container | Managed Postgres + read replica |
| Autoscaling | **Demonstrated** by load test + manual `docker compose up --scale pricing-api=6` | Automatic |

Same code, same architecture — only the deployment target changes.
