# Monetize360 — Tech Stack & Repository Layout

Companion to [ARCHITECTURE.md](ARCHITECTURE.md). Rule for every choice below:
**free and open source, boring and proven, and the simplest thing that meets the architecture.**

---

## 1. Architecture style

**Modular monolith, deployed as three independently scalable services.**

```
                 one repo · one Python package · one Docker image
                                     │
         ┌───────────────────────────┼───────────────────────────┐
         ▼                           ▼                           ▼
   pricing-api                  control-api                 audit-worker
   (hot path, scale 2→50)       (rules lifecycle, 1–2)      (hash chain, 1..M)
   create_pricing_app()         create_control_app()        python -m ...audit_worker
```

| Option | Verdict | Why |
|---|---|---|
| Single process doing everything | ❌ | Can't scale pricing without also scaling admin; a slow audit query could slow prices |
| Microservices (separate repos/packages per service) | ❌ | All three need the **same engine version** — version drift between services would break replay determinism. Triple the setup in a 12-hour event |
| **Modular monolith, 3 deployables** | ✅ | One codebase, one version, impossible to drift. Each service still scales, restarts and fails independently |

Module boundaries are enforced in code, not by convention:

```
api ──► control ──► engine ◄── workers
 │         │          ▲
 └──► security        │   engine imports NOTHING from the project except itself
 └──► store ──────────┘   (tests/unit/test_engine_is_pure.py fails the build otherwise)
```

- `engine/` — pure functions. No database, no Redis, no clock, no randomness, no network. This is what makes prices deterministic and replayable, and what stays frozen during the demo.
- `store/` — the **only** place that talks to Postgres or Redis.
- `api/` — thin HTTP layer: parse, authorize, call, respond.

---

## 2. Runtime stack

| Layer | Choice | Min version | Why this | Rejected | License |
|---|---|---|---|---|---|
| Language | **Python** | 3.11 (image: 3.12) | AI-assisted coding is most reliable in Python; `decimal` in stdlib; see ARCHITECTURE §15 | C/C++ (memory-unsafe, slow to build), Go (team speed) | PSF |
| Web framework | **FastAPI** | 0.115 | Async, auto OpenAPI docs at `/docs` (our demo UI), Pydantic validation built in | Flask (no validation/docs), Django (too heavy) | MIT |
| ASGI server | **Uvicorn** | 0.30 | Standard FastAPI server, multi-worker | Gunicorn+workers (extra layer) | BSD |
| Validation | **Pydantic v2** | 2.8 | Typed models, rejects bad input at the edge, `Decimal` support | marshmallow | MIT |
| Money math | **`decimal`** (stdlib) | — | Exact arithmetic, explicit rounding modes | `float` (0.1 + 0.2 ≠ 0.3) | — |
| Pack files | **PyYAML** + **jsonschema** | 6.0 / 4.23 | YAML readable by business users; JSON Schema gives validation + VS Code autocomplete | JSON packs (less readable), TOML | MIT |
| Rule expressions | **CEL syntax, in-house evaluator** (`engine/expr.py`, stdlib `ast`) | — | Exact `Decimal`, compiled once, whitelisted `ast` nodes only — no eval/exec, no calls, no attribute access (attack tests: `tests/unit/test_expr.py`); ~50 µs per hotel price (ARCHITECTURE §6, ENGINE_SPEC §5.4) | cel-python (floats, ~300 µs/condition — measured), `eval` (unsafe) | — |
| Database | **PostgreSQL** | 16 | Row-Level Security, partitioning, JSONB, battle-tested | MongoDB (no RLS, weaker audit guarantees), SQLite (single writer, no RLS) | PostgreSQL |
| DB driver | **psycopg 3** (+ pool) | 3.2 | Fast, parameterized queries only, no ORM magic | SQLAlchemy ORM (extra layer we don't need) | LGPL-3.0 |
| Cache / queue / events | **Redis** | 7 | One tool for signals (TTL), rate limits, audit stream, pub/sub | Kafka (heavy), RabbitMQ (second tool) | free to self-host¹ |
| IDs | **python-ulid** | 2.7 | Sortable by time, unique across replicas without coordination | UUID4 (not sortable), DB sequences (needs DB on hot path) | MIT |
| Calendar signals | **built-in festival table** | — | Offline, and versioned with the engine so old prices replay identically | `holidays` lib (its dates change between versions → replay drift), calendar APIs | — |
| Timezones | **tzdata** | 2024.1 | IANA zones for the pack `timezone` (Windows and slim Docker images ship none) | system tz files (missing on Windows / slim images) | Apache-2.0 |
| Metrics | **prometheus-client** | 0.20 | `/metrics` endpoint, industry standard | — | Apache-2.0 |
| Load balancer | **nginx** | 1.27 | Round-robin, retries failed replica, path routing | Traefik (fine too, more config) | BSD |
| Containers | **Docker** + Compose | — | `docker compose up` runs the whole system | — | Apache-2.0² |

¹ Redis 7.4+ uses source-available licenses that are free for self-hosting. **Valkey** (BSD, Linux Foundation) is a command-compatible drop-in — swap the image name if the judges care about OSI licensing.
² Docker Engine is open source. Docker Desktop is free for students, personal use and small businesses; **Podman** or **Rancher Desktop** are fully free alternatives.

## 3. Development & quality stack

| Purpose | Tool | Why |
|---|---|---|
| Tests | **pytest** | Standard, simple |
| Fuzz / property tests | **Hypothesis** (a runtime dependency, not only dev) | Powers `/verify` in `control-api` *and* our test suite; shrinks a failure to its smallest input |
| API tests | **httpx** (FastAPI TestClient) | Call the app in-process, no server needed |
| Load test | **Locust** | Python, measures req/s and p50/p95/p99 |
| Lint + security lint | **Ruff** (incl. `S` = bandit rules) | One fast tool: style, imports, security smells like `eval`, hard-coded passwords |
| CI | **GitHub Actions** | Free for public repos; runs lint + unit + golden + property on every push |
| Editor | **VS Code** + Red Hat YAML extension | Pack autocomplete from our JSON Schema |

## 4. Production design (described, not built in 12 h)

| Need | Free tool |
|---|---|
| Orchestration + autoscaling | Kubernetes + Horizontal Pod Autoscaler |
| Scheduled pre-scaling (festival peaks) | KEDA cron scaler |
| Audit partition management | pg_partman |
| Dashboards / alerts | Grafana + Prometheus Alertmanager |
| TLS certificates | Let's Encrypt (cert-manager) |
| Redis at scale | Redis Cluster / Valkey Cluster |

## 5. Deliberately NOT used

| Not used | Why |
|---|---|
| Any AI / LLM in the product | Prices must be deterministic; no paid or external dependency (AI *coding assistants* for building are fine) |
| ORM (SQLAlchemy) | ~6 tables, hand-written parameterized SQL is shorter and clearer |
| Kafka | Redis Streams covers our volume; documented upgrade path |
| Separate microservice repos | Version drift breaks replay (section 1) |
| React / frontend | Mentor: backend and architecture are judged; Swagger `/docs` is the demo UI |
| Migration framework (Alembic) | Schema created by Postgres init scripts; add Alembic when the schema starts changing in production |
| Paid cloud / SaaS | Rule of the event; everything runs on one laptop |

---

## 6. Repository layout

```
monetize360/
├── README.md                     how to run, the golden rule
├── pyproject.toml                dependencies, pytest + ruff config (single source of truth)
├── Dockerfile                    ONE image for all three services, runs as non-root
├── docker-compose.yml            postgres, redis, pricing-api ×2, control-api, audit-worker, nginx
├── .env.example                  owner vs app DB accounts, Redis URL, cache TTLs
├── .gitignore / .dockerignore
│
├── src/monetize360/
│   ├── config.py                 the only module that reads environment variables
│   │
│   ├── engine/                   ★ PURE — frozen during the demo (git diff must be empty)
│   │   ├── models.py             Pack, SignalSpec, Rule, Group, Guards, Context, Result, WaterfallRow
│   │   ├── compiler.py           valid pack → immutable CompiledPack (CEL compile, index tables); reads no files
│   │   ├── signals.py            resolve + validate signals, staleness policy, record sources
│   │   ├── publicsignals.py      weekday / weekend / hour / festival from `as_of`
│   │   ├── operators.py          the 11 operators
│   │   ├── stacking.py           multiply · add · max · first_match · exclusive
│   │   ├── guards.py             pack → tenant → platform, strictest wins, clamp rows
│   │   ├── rounding.py           step + explicit mode + 49/99 endings
│   │   ├── evaluate.py           the pipeline + waterfall self-check
│   │   └── explain.py            counterfactuals by re-evaluation
│   │
│   ├── control/                  rules lifecycle (cold path)
│   │   ├── schema.py             gate 1: JSON Schema validation, all errors with paths
│   │   ├── linter.py             static checks
│   │   ├── verifier.py           Hypothesis fuzz → "Verified Safe"
│   │   ├── impact.py             replay recent traffic against a draft
│   │   ├── replay.py             replay one price_id (same or other version)
│   │   └── publisher.py          approve (2nd person) → publish → rollback, emits events
│   │
│   ├── security/
│   │   ├── principal.py          who is calling: tenant, role, domains — from the key only
│   │   ├── keys.py               generate, SHA-256, constant-time verify, cache, revoke
│   │   └── ratelimit.py          token bucket per key / tenant
│   │
│   ├── store/                    the ONLY code that talks to Postgres / Redis
│   │   ├── db.py                 connection pool, sets app.tenant_id for RLS
│   │   ├── cache.py              Redis: signals, max_change anchors, pub/sub, audit stream
│   │   ├── packcache.py          in-memory compiled packs, hot reload, last-known-good
│   │   ├── packs.py · api_keys.py · audit.py   table queries
│   │
│   ├── api/
│   │   ├── app.py                create_pricing_app() / create_control_app()
│   │   ├── deps.py               auth + role + domain-scope dependencies
│   │   ├── schemas.py            request/response models (Decimal, no floats)
│   │   ├── errors.py             uniform errors, 404 for "not yours"
│   │   └── routes/               price · packs · audit · admin · health · scoreboard
│   │
│   └── workers/
│       └── audit_worker.py       Redis stream → hash chain → batch insert
│
├── packs/
│   ├── schema/                   pack.schema.json (validation + editor autocomplete)
│   └── templates/                10 domains, each with golden cases: hotel · airline · ride · retail · parking · ev_charging · event_tickets · saas · cloud_gpu · food_delivery
│
├── tests/
│   ├── golden/                   ★ HAND-CALCULATED cases (hotel.yaml seeded) + runner
│   ├── unit/                     schema, signals, operators, stacking, guards, rounding, linter, keys, engine purity
│   ├── property/                 Hypothesis invariants
│   └── integration/              API, tenant isolation, replay, tamper detection (need Docker)
│
├── bench/locustfile.py           load test → scoreboard numbers
├── scripts/
│   ├── seed_demo.py              demo tenants, keys, policies, published packs
│   └── check_engine_frozen.py    the "zero engine change" proof
│
├── deploy/
│   ├── nginx/nginx.conf          routing + replica retry
│   └── postgres/init/
│       ├── 001_roles.sh          limited app account (so RLS + no-UPDATE rules apply)
│       └── 002_schema.sql        tables, partitions, grants, Row-Level Security, key lookup fn
│
├── .github/workflows/ci.yml      lint + tests on every push
├── AGENTS.md / CLAUDE.md         rules every AI coding assistant reads automatically
├── .gitattributes                keeps *.sh / *.sql with LF endings (Windows checkouts)
└── docs/
    ├── ARCHITECTURE.md           v2 (current) — the why
    ├── ARCHITECTURE-v1.md        previous draft
    ├── ENGINE_SPEC.md            exact math (wins any disagreement)
    ├── API.md                    exact request/response JSON
    ├── PACK_SCHEMA.md            pack format
    ├── BUILD_GUIDE.md            team split, milestones, tester checklist
    ├── PROMPTS.md                ready-made AI prompts per person
    ├── REVIEW-engine-2026-10-11.md  engine review of main @ e5867a0 (findings #1–#12)
    ├── REVIEW-domain-agnostic.md     "works for any domain?" review of main @ cd93fdb (8 domains, gaps D1–D9)
    └── TECH_STACK.md             this file
```

Every Python file already exists with a one-line docstring stating its single responsibility — read it before writing (or prompting AI to write) the file.

### Why this layout is production-grade

| Property | How the layout delivers it |
|---|---|
| Deterministic engine | `engine/` can't import I/O; a test enforces it |
| Clear ownership for 3 people | A: engine + linter/verifier/replay · B: api, security, store, workers, deploy · C: packs, golden tests, docs (BUILD_GUIDE) |
| Security reviewable | All key handling in `security/`, all SQL in `store/`, app DB account can't rewrite audit |
| Same code everywhere | One image; services differ only by start command |
| Zero-engine-change proof | New domains touch only `packs/` and `tests/golden/` |
| Reproducible setup | `cp .env.example .env && docker compose up --build` |

---

## 7. Setup

**Prerequisites (all free):** Python 3.11+, Git, Docker (Docker Desktop, Podman or Rancher Desktop), VS Code + YAML extension.

```sh
cp .env.example .env                     # then change the two passwords
docker compose up --build                # full system at http://localhost:8080/docs

# engine work without Docker
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

Pin exact versions at hour 0 so all three laptops match: `pip freeze > requirements.lock` and commit it.
