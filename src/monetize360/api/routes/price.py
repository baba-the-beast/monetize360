"""POST /v1/price/{domain}, /v1/price/{domain}/batch, /v1/simulate/{domain}."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from prometheus_client import Counter, Histogram
from pydantic import ValidationError
from ulid import ULID

from monetize360.api.deps import live_pack, require_principal
from monetize360.api.errors import ApiError, engine_error_body, error_body, jsonable, pack_not_found
from monetize360.api.schemas import (
    BatchRequest,
    BatchResponse,
    PriceRequest,
    PriceResponse,
    SimulateRequest,
    SimulateResponse,
)
from monetize360.control.replay import output_of
from monetize360.engine import Context, EngineError, evaluate, explain
from monetize360.security.principal import Principal
from monetize360.store.audit import AuditEvent, AuditLog
from monetize360.store.packcache import LivePack

router = APIRouter(prefix="/v1", tags=["pricing"])

VALID_FOR = timedelta(minutes=15)
PRICES = Counter("m360_prices_total", "Price evaluations", ["domain", "kind", "outcome"])
SECONDS = Histogram("m360_price_seconds", "Time to evaluate and explain one price", ["kind"],
                    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1))
AUDIT_SECONDS = Histogram("m360_audit_seconds", "Time to append one audit record",
                          buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1))
AUDIT = Counter("m360_audit_records_total", "Audit records by where they went", ["where"])
pricing_key = require_principal("pricing")
Domain = Annotated[str, Path(description="Pack domain, e.g. hotel, airline, ride", examples=["hotel"],
                             json_schema_extra={"example": "hotel"})]


def _money(d) -> str:
    return f"{d:.2f}"


def _utc(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _price(pack: LivePack, req: PriceRequest, *, simulate: bool, principal: Principal | None = None,
           audit: AuditLog | None = None) -> dict[str, Any]:
    """Evaluate one request. Raises EngineError / ApiError; the caller decides how to report them."""
    kind = "simulate" if simulate else "price"
    domain = pack.compiled.domain
    as_of = req.as_of or datetime.now(timezone.utc)
    flags: list[str] = []
    reference = None
    if simulate:
        reference = req.reference_price                      # explicit what-if anchor
    elif pack.compiled.guards.max_change_pct is not None:
        # ponytail: no anchor store until Redis (M5), so say so instead of pretending the guard ran
        flags.append("max_change not enforced: no price-anchor store configured")
    if req.idempotency_key is not None and not simulate:
        # ponytail: needs the shared store too (M5); a per-process memory would be wrong across replicas
        flags.append("idempotency_key not honoured: no shared store configured")

    ctx = Context(as_of=as_of, base_price=req.base_price, signals=req.signals, reference_price=reference,
                  allow_public_override=simulate)
    started = time.perf_counter()
    try:
        result = evaluate(pack.compiled, ctx)
        counterfactuals = explain(pack.compiled, ctx, result)
    except EngineError as e:
        PRICES.labels(domain, kind, e.code).inc()
        raise
    SECONDS.labels(kind).observe(time.perf_counter() - started)
    PRICES.labels(domain, kind, "ok").inc()

    out = output_of(result)                    # exactly what is audited and compared on replay
    price_id = None
    if not simulate:
        price_id = str(ULID())
        if audit is None:
            flags.append("not audited: no database configured")
        else:
            event = AuditEvent(
                price_id=price_id, tenant_id=principal.tenant_id, domain=domain, pack_version=pack.version,
                entity=req.entity, key_id=principal.key_id, as_of=as_of,
                inputs={"base_price": req.base_price, "signals": req.signals, "redis_signals": {},
                        "allow_public_override": False, "policy": None},
                signal_sources=result.signals_used, reference_price=reference, output=out)
            started = time.perf_counter()
            stored = audit.record(event)
            AUDIT_SECONDS.observe(time.perf_counter() - started)
            AUDIT.labels("database" if stored else "spool").inc()
            if not stored:
                flags.append("audit deferred: database unreachable, record spooled")

    body: dict[str, Any] = {
        "price": out["price"],
        "currency": out["currency"],
        "domain": domain,
        "pack_version": pack.version,
        "valid_until": _utc(as_of + VALID_FOR),
        "waterfall": out["waterfall"],
        "counterfactuals": [{"if": c["if"], "price": _money(c["price"])} if "price" in c else jsonable(c)
                            for c in counterfactuals],
        "signals_used": jsonable(result.signals_used),
        "bounds": out["bounds"],
        "flags": [*out["flags"], *flags],
    }
    if price_id is not None:
        body = {"price_id": price_id, **body}
    return body


@router.post("/price/{domain}", response_model=PriceResponse)
def price(domain: Domain, body: PriceRequest, request: Request,
          principal: Principal = Depends(pricing_key)) -> dict[str, Any]:   # noqa: B008
    return _price(live_pack(request, principal, domain), body, simulate=False, principal=principal,
                  audit=request.app.state.audit)


@router.post("/price/{domain}/batch", response_model=BatchResponse)
def price_batch(domain: Domain, body: BatchRequest, request: Request,
                principal: Principal = Depends(pricing_key)) -> dict[str, Any]:   # noqa: B008
    pack = live_pack(request, principal, domain)
    results: list[dict[str, Any]] = []
    for item in body.items:
        try:
            results.append(jsonable(_price(pack, PriceRequest.model_validate(item), simulate=False,
                                           principal=principal, audit=request.app.state.audit)))
        except ValidationError as e:
            details = [{"path": ".".join(str(p) for p in err["loc"]), "message": err["msg"]} for err in e.errors()]
            results.append(error_body("bad_request", "the item is not a valid price request", details))
        except EngineError as e:
            results.append(engine_error_body(e))
        except ApiError as e:
            results.append(error_body(e.code, e.message, e.details))
    return {"results": results}


@router.post("/simulate/{domain}", response_model=SimulateResponse)
def simulate(domain: Domain, body: SimulateRequest, request: Request,
             principal: Principal = Depends(pricing_key)) -> dict[str, Any]:   # noqa: B008
    pack = live_pack(request, principal, domain)
    if body.pack_version is not None and body.pack_version != pack.version:
        raise pack_not_found(domain)          # drafts and old versions need the pack store (M5)
    return _price(pack, body, simulate=True)
