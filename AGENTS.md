# Rules for AI coding assistants (and humans) working on Monetize360

Read before writing code: `docs/ENGINE_SPEC.md` (exact math), `docs/API.md` (exact JSON),
`docs/ARCHITECTURE.md` (why). Each `.py` file starts with a docstring stating its single job — stay inside it.

## Never
- Never use `float` for money or signals, never use built-in `round()`. Use `Decimal` with the local
  context from ENGINE_SPEC §2.
- Never edit an expected value in `tests/golden/*.yaml` to make a test pass. If a golden test fails,
  the code is wrong — or stop and ask the human to recalculate by hand.
- Never import `api`, `store`, `security`, `control`, `time`, `random`, `os`, `datetime.now`, or any
  network library inside `src/monetize360/engine/`. The engine gets everything through `Context`.
- Never use `eval`/`exec`. Rule expressions use CEL syntax, evaluated by `engine/expr.py` (exact Decimal,
  whitelisted `ast` nodes). Don't switch it to cel-python — it computes in floats (ENGINE_SPEC §5.4).
- Never write SQL or Redis commands outside `src/monetize360/store/`. SQL is always parameterized.
- Never read the tenant from the request body — only from the API key (`security/principal.py`).
- Never log or return a full API key secret.
- Never add a dependency without asking. Everything must be free and open source. No AI/LLM features.

## Always
- Run `pytest` before saying something works. Golden tests must be 100%.
- Return errors in the format from `docs/API.md` (`error`, `message`, `details`, `request_id`).
- "Not yours" and "doesn't exist" both return 404.
- Money in JSON is a string with 2 decimals: `"4950.00"`.
- Keep functions small and boring. Simplest code that meets the spec.

## Commands
```sh
.venv\Scripts\activate
pip install -e ".[dev]"
pytest                       # unit + golden + property
pytest tests/golden -q       # the acceptance test for the engine
ruff check .
docker compose up --build    # full system, http://localhost:8080/docs
```
