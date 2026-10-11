# Monetize360

Universal dynamic pricing engine — one engine, any industry. Every price is explainable,
bounded by three guardrail layers, replayable and tamper-evident. 100% free and open source.

- Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Tech stack & repo layout: [docs/TECH_STACK.md](docs/TECH_STACK.md)
- Domain Pack schema: [docs/PACK_SCHEMA.md](docs/PACK_SCHEMA.md)

## Run everything (needs Docker)

```sh
cp .env.example .env
docker compose up --build
# API docs:  http://localhost:8080/docs
```

## Develop without Docker (engine + tests only)

```sh
python -m venv .venv
.venv\Scripts\activate          # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -e ".[dev]"
pytest                           # unit + golden + property tests
```

## Run the pricing API with no database (dev tenant)

```sh
# Windows PowerShell:  $env:DEV_TENANT = "demo"      macOS/Linux:  export DEV_TENANT=demo
uvicorn --factory monetize360.api.app:create_pricing_app --port 8000
# Swagger:  http://localhost:8000/docs      Ready check:  http://localhost:8000/readyz
```

```sh
curl -X POST http://localhost:8000/v1/simulate/hotel -H "Content-Type: application/json" \
  -d '{"base_price":"3000","signals":{"occupancy":0.85},"as_of":"2026-11-08T09:00:00Z","reference_price":"4500"}'
```

`DEV_TENANT` is for running without a database. With neither it nor `DATABASE_URL`, every `/v1` call
returns 401 — the API is closed by default. With it, every caller is that tenant and its packs are the files in
`packs/templates/`. Never set it in production.

Not enforced until the shared store exists (the response says so in `flags`): the `max_change` anchor and
`idempotency_key`. Not built yet: audit rows, tenant policy, Redis-fed signals.

## Run with API keys and Postgres, no Docker (M4)

Needs a local PostgreSQL 16 and a git-ignored `.env.local` holding `PG_SUPERUSER`, `PG_SUPERUSER_PASSWORD`,
`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `APP_DB_PASSWORD`, `DATABASE_URL`.

```sh
python scripts/setup_local_db.py          # creates the database from deploy/postgres/init/*.sql
python scripts/seed_demo.py               # 3 demo tenants, their packs and keys -> .demo-keys.json (git-ignored)
# PowerShell:  $env:DATABASE_URL = "<DATABASE_URL from .env.local>"
uvicorn --factory monetize360.api.app:create_pricing_app --port 8000
# http://localhost:8000/docs  ->  Authorize  ->  paste a key from .demo-keys.json
pytest -m integration                     # 32 tests against the real database (recreates monetize360_test)
```

| Demo tenant | Domains | Try |
|---|---|---|
| `hotel_001` | hotel | its key prices hotel; on `/v1/price/airline` it gets 404 |
| `airline_002` | airline | the reverse |
| `travel_003` | hotel, ride, parking | `pricing_hotel_only` gets 404 on its own tenant's `ride` |

With `DATABASE_URL` set, the tenant comes only from the API key and `DEV_TENANT` is ignored. If Postgres goes
down, keys this replica has already seen keep pricing; unseen keys get 503 `auth_unavailable`.
Not yet: shared rate limits and instant cross-replica revocation (Redis, M5) — until then a revoked key can
live up to 60 s on other replicas and the rate limit is per replica.

## Golden rule

Humans write `tests/golden/*.yaml` by hand first. Code (AI-assisted or not) is done when
`pytest tests/golden` is 100%. Never edit an expected value without recalculating it.
