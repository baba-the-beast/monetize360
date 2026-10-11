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

## Golden rule

Humans write `tests/golden/*.yaml` by hand first. Code (AI-assisted or not) is done when
`pytest tests/golden` is 100%. Never edit an expected value without recalculating it.
