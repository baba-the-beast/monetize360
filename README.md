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

## Run the pricing API without Docker (M3)

```sh
# Windows PowerShell:  $env:DEV_TENANT = "demo"      macOS/Linux:  export DEV_TENANT=demo
uvicorn --factory monetize360.api.app:create_pricing_app --port 8000
# Swagger:  http://localhost:8000/docs      Ready check:  http://localhost:8000/readyz
```

```sh
curl -X POST http://localhost:8000/v1/simulate/hotel -H "Content-Type: application/json" \
  -d '{"base_price":"3000","signals":{"occupancy":0.85},"as_of":"2026-11-08T09:00:00Z","reference_price":"4500"}'
```

`DEV_TENANT` exists because API keys are not built yet (M4). Without it every `/v1` call returns 401 — the
API is closed by default. With it, every caller is that tenant and its packs are the files in
`packs/templates/`. Never set it in production.

Not enforced until the shared store exists (the response says so in `flags`): the `max_change` anchor and
`idempotency_key`. Not built yet: audit rows, tenant policy, rate limits, Redis-fed signals.

## Golden rule

Humans write `tests/golden/*.yaml` by hand first. Code (AI-assisted or not) is done when
`pytest tests/golden` is 100%. Never edit an expected value without recalculating it.
