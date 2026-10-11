"""/healthz, /readyz (packs loaded + redis ok + self-test price matches), /metrics."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from monetize360.engine import Context, EngineError, compile_pack, evaluate

router = APIRouter(tags=["ops"])

# A tiny built-in pack with a known answer: 1000 +10% = 1100. If this is ever wrong, the replica must not serve.
_SELF_TEST = compile_pack({
    "domain": "self_test", "currency": "INR",
    "signals": {"x": {"type": "number", "source": "request", "default": 0}},
    "base": {"op": "from_request"},
    "groups": [{"name": "g", "stacking": "multiply", "rules": [
        {"id": "plus-ten", "when": "x >= 0", "then": {"op": "pct", "value": 10}, "reason": "self-test"}]}],
    "guards": {"floor": {"op": "abs", "value": 1}, "ceiling": {"op": "abs", "value": 100000}},
    "rounding": {"step": 1, "mode": "half_up"},
})
_SELF_TEST_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def self_test_ok() -> bool:
    try:
        result = evaluate(_SELF_TEST, Context(as_of=_SELF_TEST_AT, base_price=Decimal(1000)))
    except EngineError:
        return False
    return result.price == Decimal("1100.00")


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
def readyz(request: Request) -> JSONResponse:
    packs, db, events = request.app.state.packs, request.app.state.db, request.app.state.events
    checks = {
        "packs_loaded": len(packs),
        "pack_load_error_count": len(packs.load_errors),
        "self_test": self_test_ok(),
        "database": "not_configured" if db is None else ("ok" if db.ping() else "down"),
        # the listener's own state, not a ping: this endpoint must stay fast even when Redis is far away
        "redis": "not_configured" if events is None else ("ok" if events.connected else "down"),
    }
    # neither the database nor Redis is required to be ready: packs are in memory, so pricing continues
    ready = checks["packs_loaded"] > 0 and checks["self_test"]
    if request.app.state.dev_tenant:
        # this endpoint needs no key, so tenant names and pack errors are shown in local dev mode only;
        # with real tenants they go to the log
        checks["pack_load_errors"] = {f"{t}/{d}": why for (t, d), why in packs.load_errors.items()}
    return JSONResponse({"ready": ready, **checks}, status_code=200 if ready else 503)


@router.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
