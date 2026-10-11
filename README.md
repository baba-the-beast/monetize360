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

In this no-database mode nothing is recorded: no audit row, no `max_change` anchor, no idempotency. Every response
says so in `flags`.

## Run with API keys and Postgres, no Docker (M4)

Needs a local PostgreSQL 16 and a git-ignored `.env.local` holding `PG_SUPERUSER`, `PG_SUPERUSER_PASSWORD`,
`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `APP_DB_PASSWORD`, `DATABASE_URL`.

```sh
python scripts/setup_local_db.py          # creates the database from deploy/postgres/init/*.sql
python scripts/seed_demo.py               # 3 demo tenants, their packs and keys -> .demo-keys.json (git-ignored)
# PowerShell:  $env:DATABASE_URL = "<DATABASE_URL from .env.local>"
uvicorn --factory monetize360.api.app:create_app --port 8000      # pricing + audit in one process
# http://localhost:8000/docs  ->  Authorize  ->  paste a key from .demo-keys.json
pytest -m integration                     # against the real database (recreates monetize360_test)
```

| Demo tenant | Domains | Try |
|---|---|---|
| `hotel_001` | hotel | its key prices hotel; on `/v1/price/airline` it gets 404 |
| `airline_002` | airline | the reverse |
| `travel_003` | hotel, ride, parking | `pricing_hotel_only` gets 404 on its own tenant's `ride` |

With `DATABASE_URL` set, the tenant comes only from the API key and `DEV_TENANT` is ignored. If Postgres goes
down, keys this replica has already seen keep pricing; unseen keys get 503 `auth_unavailable`.
The rate limit is per replica (a key gets its limit on each server).

### Audit, replay and tamper detection (M6)

Every `/v1/price` appends a row to a hash chain in Postgres before it answers (about 3 ms). With an `admin` key:

| Call | Shows |
|---|---|
| `GET /v1/audit/{price_id}` | the inputs, where each signal came from, the rule version and who approved it |
| `POST /v1/replay {"price_id": "..."}` | the same price again, from the record alone (`identical: true`) |
| `POST /v1/audit/verify {"domain": "hotel"}` | the whole chain checked; an edited, removed or inserted row is named |
| `GET /v1/audit/export?domain=hotel&format=csv` | your data, to take anywhere |

Tamper demo: as the database owner run
`UPDATE audit_log SET output = jsonb_set(output, '{price}', '"1.00"') WHERE price_id = '...'`, then call
`/v1/audit/verify` — it reports that row. (The application's own account gets `permission denied`.)
If Postgres is unreachable prices are still returned; their records wait in `.audit-spool/` and are appended
when it is back.

### The max_change guard and idempotency

Both are enforced in the same Postgres transaction as the audit row.

- **max_change** — hotel allows 15% per hour. Price a room at 3000, then again with `occupancy: 0.9`
  (rules say 3600): you get **3450**, and you keep getting 3450 for the rest of the hour however often you ask.
  An hour later the last price starts the next window.
- **Idempotency** — send `"idempotency_key": "booking-77"`. A retry returns the first response (same
  `price_id`, no second audit row); the same key with a different body is `409 idempotency_conflict`.
  Keys last 24 hours, per tenant and domain.

### API keys and instant revocation

With an `admin` key: `POST /v1/admin/keys` (the new key is shown once), `GET /v1/admin/keys`,
`POST /v1/admin/keys/{key_id}/revoke`. Add `REDIS_URL` to the environment and a key revoked on one server is
refused by every other server within milliseconds; without Redis they find out within 60 seconds. Redis only
carries these announcements: nothing is stored in it and no price request waits for it.

### Changing rules: draft → lint → verify → approve → publish → rollback

```sh
curl -X PUT  localhost:8000/v1/packs/hotel -H "Authorization: Bearer <editor key>" \
     -H "Content-Type: application/yaml" --data-binary @packs/templates/hotel.yaml       # -> {"version": 2, "status": "draft"}
curl -X POST localhost:8000/v1/packs/hotel/2/lint     -H "Authorization: Bearer <editor key>"
curl -X POST localhost:8000/v1/packs/hotel/2/verify   -H "Authorization: Bearer <editor key>"      # 10,000 cases, ~2.5 s
curl -X POST localhost:8000/v1/packs/hotel/2/impact   -H "Authorization: Bearer <editor key>"      # what real prices would change
curl -X POST localhost:8000/v1/packs/hotel/2/approve  -H "Authorization: Bearer <approver key>"    # not the author's key
curl -X POST localhost:8000/v1/packs/hotel/2/publish  -H "Authorization: Bearer <approver key>"    # live on every server
curl -X POST localhost:8000/v1/packs/hotel/rollback   -H "Authorization: Bearer <approver key>" \
     -H "Content-Type: application/json" -d '{"to_version": 1}'
```

A draft prices nothing, but `POST /v1/simulate/hotel` with `"pack_version": 2` tries it. A new industry is the
same steps on a new domain name: `PUT /v1/packs/ev_charging` ... publish, and `/v1/price/ev_charging` works with
no restart and no engine change. Prices issued under an old version still replay under that version.

### Tenant limits and the kill switch

With an `admin` key:

```sh
curl -X PUT  localhost:8000/v1/admin/policy -H "Authorization: Bearer <admin key>" \
     -H "Content-Type: application/json" -d '{"max_surge_x": "1.1"}'     # busy room 3600 -> 3300, at once
curl -X POST localhost:8000/v1/admin/kill   -H "Authorization: Bearer <admin key>" \
     -H "Content-Type: application/json" -d '{"domain": "hotel", "mode": "freeze"}'
```

`freeze` repeats each room's last price, `base_only` turns the rules off but keeps the guards, `normal` goes
back. Every server obeys: at once through Redis, within 10 s without it, and a server started later already
knows.

### Admin console

Open <http://localhost:8000/admin/> and sign in with an `admin` key from `.demo-keys.json`. Switch the engine per
domain, upload and publish rules, set limits, manage API keys and inspect every recorded price. It is a plain
client of the API above: static files, no build step. Screens, the API each one calls and a demo script:
[docs/ADMIN_CONSOLE.md](docs/ADMIN_CONSOLE.md). `python scripts/demo_traffic.py` records some sample prices first.

### Load test, scoreboard and the engine-frozen check

```sh
locust -f bench/locustfile.py --headless -u 32 -r 32 -t 20s --host http://127.0.0.1:8010 --csv bench/results/run
curl localhost:8000/v1/scoreboard          # capabilities, this replica's live counters, measured results
python scripts/check_engine_frozen.py      # engine diff since origin/main; "no changes" = frozen
```

Measured numbers and what they mean: [bench/RESULTS.md](bench/RESULTS.md).

## Golden rule

Humans write `tests/golden/*.yaml` by hand first. Code (AI-assisted or not) is done when
`pytest tests/golden` is 100%. Never edit an expected value without recalculating it.
