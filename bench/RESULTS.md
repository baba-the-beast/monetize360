# Measured results

Measured on **2026-10-11** on one Windows 11 laptop (12 logical CPUs) running the API, PostgreSQL 16 and the
load generator together. The same numbers are served by `GET /v1/scoreboard` under `measured`
(from `bench/measured.json`).

**Read these as the shape of the system, not as production capacity.** Run-to-run variance on this laptop is
large: five identical 4-worker runs gave 213, 391, 486, 604 and 667 requests per second.

## Latency at light load

`/v1/price` with a real audit row, 4 clients, 4 workers:

| req/s | p50 | p95 | p99 | failures |
|---|---|---|---|---|
| 481 | 8 ms | 11 ms | 14 ms | 0 |

Target in ARCHITECTURE: API p95 < 150 ms. Met with a wide margin at this load.

## Throughput at saturation

Locust, closed loop, 32 clients, 20 s per run, every request a new entity. 0 failures in every run.

| Workers | Real prices, 5 audit chains (median; runs) | Real prices, 1 audit chain | `/simulate` (no writes) |
|---|---|---|---|
| 1 | 197 (262, 206, 188, 172) | 252 | 596 |
| 2 | 454 (one run) | — | — |
| 4 | 486 (667, 213, 391, 486, 604) | 396 | 1,952 |
| 8 | 710 (834, 553, 707, 714) | 416 | 2,399 |

What this shows:

- **It scales with workers** when prices spread over several (tenant, domain) chains: roughly 200 → 490 → 710.
  Less than linear: the database and the load generator share the same 12 CPUs.
- **One audit chain tops out near 400 req/s whatever the worker count.** That is the documented ceiling
  (ARCHITECTURE 11.1): appends to one chain are serialised so the hash chain stays valid. A single domain that
  needs more would batch appends in a worker; not built.
- **The engine and API alone are much faster** (about 2,400 req/s) — writing the audit row is the cost, and it is
  the price of "every price is recorded before it is returned". At 8 workers the single-process load generator
  is probably what limits this column.
- The ARCHITECTURE target "1,000+ req/s per node" is **not met for audited prices on this laptop** (710 at 8
  workers). It is met for `/simulate`.

## Postgres unreachable

Postgres was made to refuse every connection to the app's database while the server was running.

| | |
|---|---|
| Prices requested / returned | 40 / 40 |
| First request after it vanished | 2,013 ms (the connection timeout) |
| The rest | p50 7.2 ms |
| Flags on each | `audit deferred: database unreachable, record spooled`, `max_change not enforced: database unreachable` |
| An API key this server had never seen | `503 auth_unavailable` |
| `/readyz` | still ready (`database: down`) |
| After Postgres returned | all 40 records appended to the chain within 7 s; chain valid; no spool files left |

During a longer outage one request every 5 s retries the database and pays the 2 s timeout again.

## Everything else

| What | Result |
|---|---|
| Tests | 543 unit / golden / property + 144 integration (real Postgres and Redis), 0 failures |
| Golden cases (hand-calculated) | 16 of 16, across 10 domains |
| Fuzz verification | 10 packs × 10,000 cases = 100,000 cases, 0 violations, 37.7 s |
| Lint | 10 of 10 packs clean |
| Engine lines changed since `origin/main` | 0 (`python scripts/check_engine_frozen.py`) |
| Tampering detected | 9 of 9 kinds (edit of any field, removed row, inserted row). Not detected by design: deleting the newest rows |
| Key revoked on one server → refused by another | ~5 ms after the revoke call returns (Redis); up to 60 s without Redis |
| Pack published on one server → priced by another | 0.03 s (Redis); up to 10 s without |
| Kill switch | set in 222 ms; the next price obeys |

## Reproduce

```sh
python scripts/setup_local_db.py --reset && python scripts/seed_demo.py
# PowerShell: $env:DATABASE_URL = "<from .env.local>"
uvicorn --factory monetize360.api.app:create_app --port 8010 --workers 4

# in another terminal; M360_SCENARIO = many_chains (default) | one_chain | simulate
locust -f bench/locustfile.py --headless -u 32 -r 32 -t 20s --host http://127.0.0.1:8010 --csv bench/results/run
```

The load test creates its own API keys with a 10,000 req/s limit (the default 200 req/s per key would cap it)
and fills `audit_log` and `price_state`; reset the database afterwards.
