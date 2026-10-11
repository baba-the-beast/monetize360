"""POST /v1/price/{domain}, /v1/price/{domain}/batch, /v1/simulate/{domain}.

A real price (`/price` with a database) is ONE Postgres transaction: find the idempotency key, read the
max_change anchor, evaluate, append the audit row, update the anchor, store the idempotent response. They
commit together or not at all. If Postgres can't be reached the price is still returned, in degraded mode,
and the response says exactly what could not be enforced.

The tenant's limits and the domain's kill switch come from memory (store/controls.py), are applied to every
price, and are written into the audit row, so a replay uses the limits that were in force at the time."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any

import psycopg
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
from monetize360.control import publisher
from monetize360.control.replay import output_of, policy_json
from monetize360.engine import Context, EngineError, Policy, Result, SignalInvalid, compile_pack, evaluate, explain
from monetize360.security.principal import Principal
from monetize360.store import idempotency, packs, pricestate
from monetize360.store.audit import AuditEvent, AuditLog, append_in, plain
from monetize360.store.packcache import LivePack

router = APIRouter(prefix="/v1", tags=["pricing"])

VALID_FOR = timedelta(minutes=15)
PRICES = Counter("m360_prices_total", "Price evaluations", ["domain", "kind", "outcome"])
SECONDS = Histogram("m360_price_seconds", "Time to evaluate and explain one price", ["kind"],
                    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1))
AUDIT = Counter("m360_audit_records_total", "Audit records by where they went", ["where"])
IDEMPOTENT = Counter("m360_idempotent_replays_total", "Requests answered from a stored idempotent response")
KILLED = Counter("m360_kill_switch_prices_total", "Prices answered under a kill switch", ["mode"])
pricing_key = require_principal("pricing")
Domain = Annotated[str, Path(description="Pack domain, e.g. hotel, airline, ride", examples=["hotel"],
                             json_schema_extra={"example": "hotel"})]
KILL_FLAGS = {"base_only": "kill switch: base_only (rules are off; base price, guards and rounding only)",
              "freeze": "kill switch: freeze (the last issued price is repeated)"}


@dataclass(frozen=True)
class Rules:
    """What an operator has set for this tenant and domain, as this replica knows it right now."""
    policy: Policy | None = None
    kill: str = "normal"


def _rules(request: Request, principal: Principal, domain: str) -> Rules:
    controls = request.app.state.controls
    return Rules(controls.policy(principal.tenant_id), controls.kill_mode(principal.tenant_id, domain))


def _money(d) -> str:
    return f"{d:.2f}"


def _utc(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _evaluate(pack: LivePack, ctx: Context, kind: str, policy: Policy | None = None,
              rules_off: bool = False) -> tuple[Result, list[dict]]:
    disabled = pack.compiled.rule_ids if rules_off else frozenset()
    started = time.perf_counter()
    try:
        result = evaluate(pack.compiled, ctx, policy, disabled)
        counterfactuals = [] if rules_off else explain(pack.compiled, ctx, result, policy)
    except EngineError as e:
        PRICES.labels(pack.compiled.domain, kind, e.code).inc()
        raise
    SECONDS.labels(kind).observe(time.perf_counter() - started)
    PRICES.labels(pack.compiled.domain, kind, "ok").inc()
    return result, counterfactuals


def _body(pack: LivePack, as_of: datetime, out: dict[str, Any], counterfactuals: list[dict],
          signals_used: dict[str, Any], flags: list[str], price_id: str | None) -> dict[str, Any]:
    """`out` is exactly what is audited and compared on replay (control/replay.output_of)."""
    body: dict[str, Any] = {
        "price": out["price"],
        "currency": out["currency"],
        "domain": pack.compiled.domain,
        "pack_version": pack.version,
        "valid_until": _utc(as_of + VALID_FOR),
        "waterfall": out["waterfall"],
        "counterfactuals": [{"if": c["if"], "price": _money(c["price"])} if "price" in c else jsonable(c)
                            for c in counterfactuals],
        "signals_used": jsonable(signals_used),
        "bounds": out["bounds"],
        "flags": [*out["flags"], *flags],
    }
    return {"price_id": price_id, **body} if price_id is not None else body


def _event(pack: LivePack, req: PriceRequest, principal: Principal, as_of: datetime, price_id: str,
           out: dict[str, Any], signals_used: dict[str, Any], reference, rules: Rules,
           frozen_from: str | None = None) -> AuditEvent:
    inputs: dict[str, Any] = {"base_price": req.base_price, "signals": req.signals, "redis_signals": {},
                              "allow_public_override": False, "policy": policy_json(rules.policy)}
    if rules.kill != "normal":
        inputs["kill_mode"] = rules.kill
    if frozen_from is not None:
        inputs["frozen_from"] = frozen_from
    return AuditEvent(
        price_id=price_id, tenant_id=principal.tenant_id, domain=pack.compiled.domain, pack_version=pack.version,
        entity=req.entity, key_id=principal.key_id, as_of=as_of, inputs=inputs,
        signal_sources=signals_used, reference_price=reference, output=out)


def _frozen_output(pack: LivePack, price: Decimal) -> dict[str, Any]:
    p = _money(price)
    return {"price": p, "currency": pack.compiled.currency,
            "waterfall": [{"step": "frozen", "kind": "base", "delta": p, "after": p,
                           "reason": "Kill switch: last issued price repeated"}],
            "bounds": {"lower": p, "upper": p}, "flags": []}


def _killed(domain: str, why: str) -> ApiError:
    return ApiError(503, "killed", f"'{domain}' is frozen by its kill switch and {why}",
                    headers={"Retry-After": "30"})


def _simulate(pack: LivePack, req: SimulateRequest, rules: Rules) -> dict[str, Any]:
    """A what-if: the tenant's limits apply (they would in real life), the kill switch does not."""
    as_of = req.as_of or datetime.now(timezone.utc)
    ctx = Context(as_of=as_of, base_price=req.base_price, signals=req.signals,
                  reference_price=req.reference_price, allow_public_override=True)
    result, counterfactuals = _evaluate(pack, ctx, "simulate", rules.policy)
    return _body(pack, as_of, output_of(result), counterfactuals, result.signals_used, [], None)


def _price(pack: LivePack, req: PriceRequest, principal: Principal, audit: AuditLog | None,
           raw: dict[str, Any], rules: Rules) -> dict[str, Any]:
    """One real price. `raw` is the request body as the caller sent it (what "the same request" means)."""
    as_of = req.as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        raise SignalInvalid("as_of must include a timezone", [{"path": "as_of"}])
    guards = pack.compiled.guards

    if audit is not None and audit.available():
        try:
            return _price_recorded(pack, req, principal, audit, as_of, raw, rules)
        except (psycopg.OperationalError, psycopg.InterfaceError) as e:
            # the database can't be reached (not a bug in a query: those still fail loudly as a 500).
            # Nothing was committed and nothing was returned, so price it again in degraded mode.
            audit.mark_down(e)

    # ---- no database (dev tenant), or it is unreachable: still a price, and the response says what was skipped
    why = "no database configured" if audit is None else "database unreachable"
    if rules.kill == "freeze":
        raise _killed(pack.compiled.domain, f"the last price can't be read ({why})")
    flags = [KILL_FLAGS[rules.kill]] if rules.kill != "normal" else []
    if guards.max_change_pct is not None:
        flags.append(f"max_change not enforced: {why}")
    if req.idempotency_key is not None:
        flags.append(f"idempotency_key not honoured: {why}")
    ctx = Context(as_of=as_of, base_price=req.base_price, signals=req.signals)
    result, counterfactuals = _evaluate(pack, ctx, "price", rules.policy, rules.kill == "base_only")
    price_id, out = str(ULID()), output_of(result)
    if audit is None:
        flags.append("not audited: no database configured")
    else:
        audit.spool(_event(pack, req, principal, as_of, price_id, out, result.signals_used, None, rules))
        AUDIT.labels("spool").inc()
        flags.append("audit deferred: database unreachable, record spooled")
    return _body(pack, as_of, out, counterfactuals, result.signals_used, flags, price_id)


def _price_recorded(pack: LivePack, req: PriceRequest, principal: Principal, audit: AuditLog, as_of: datetime,
                    raw: dict[str, Any], rules: Rules) -> dict[str, Any]:
    tenant, domain, key = principal.tenant_id, pack.compiled.domain, req.idempotency_key
    flags = [KILL_FLAGS[rules.kill]] if rules.kill != "normal" else []
    with audit.db.tenant(tenant) as conn:
        req_hash = None
        if key is not None:
            req_hash = idempotency.request_hash(domain, plain(raw))
            prior = idempotency.lookup(conn, tenant, domain, key)
            if prior is not None:
                if prior["request_hash"] != req_hash:
                    raise ApiError(409, "idempotency_conflict",
                                   "this idempotency_key was already used with a different request",
                                   [{"path": "idempotency_key", "value": key}])
                IDEMPOTENT.inc()
                return prior["response"]       # the first answer, same price_id; nothing new is written

        state = pricestate.lock_and_read(conn, tenant, domain, req.entity)
        price_id = str(ULID())

        if rules.kill == "freeze":
            # no calculation at all: repeat what this entity was last charged, and record that we did
            if state is None:
                raise _killed(domain, f"'{req.entity}' has no earlier price to repeat")
            out, signals_used, counterfactuals = _frozen_output(pack, state["last_price"]), {}, []
            append_in(conn, _event(pack, req, principal, as_of, price_id, out, signals_used, None, rules,
                                   frozen_from=state["last_price_id"]))
        else:
            anchor = pricestate.anchor_for(state, as_of, pack.compiled.guards.max_change_per)
            ctx = Context(as_of=as_of, base_price=req.base_price, signals=req.signals,
                          reference_price=anchor.reference)
            result, counterfactuals = _evaluate(pack, ctx, "price", rules.policy, rules.kill == "base_only")
            out, signals_used = output_of(result), result.signals_used
            append_in(conn, _event(pack, req, principal, as_of, price_id, out, signals_used, anchor.reference, rules))
            pricestate.save(conn, tenant, domain, req.entity, result.price, as_of, anchor, price_id)

        body = jsonable(_body(pack, as_of, out, counterfactuals, signals_used, flags, price_id))
        if key is not None:
            idempotency.save(conn, tenant, domain, key, req_hash, body)
    AUDIT.labels("database").inc()
    if rules.kill != "normal":
        KILLED.labels(rules.kill).inc()
    return body


@router.post("/price/{domain}", response_model=PriceResponse)
def price(domain: Domain, body: PriceRequest, request: Request,
          principal: Principal = Depends(pricing_key)) -> dict[str, Any]:   # noqa: B008
    return _price(live_pack(request, principal, domain), body, principal, request.app.state.audit,
                  body.model_dump(mode="json", exclude_unset=True), _rules(request, principal, domain))


@router.post("/price/{domain}/batch", response_model=BatchResponse)
def price_batch(domain: Domain, body: BatchRequest, request: Request,
                principal: Principal = Depends(pricing_key)) -> dict[str, Any]:   # noqa: B008
    # ponytail: one transaction per item (up to 500). Fine at this size; batch the appends if it shows in profiles.
    pack, rules = live_pack(request, principal, domain), _rules(request, principal, domain)
    results: list[dict[str, Any]] = []
    for item in body.items:
        try:
            results.append(jsonable(_price(pack, PriceRequest.model_validate(item), principal,
                                           request.app.state.audit, item, rules)))
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
    rules = _rules(request, principal, domain)
    if body.pack_version is None:
        return _simulate(live_pack(request, principal, domain), body, rules)
    return _simulate(_stored_pack(request, principal, domain, body.pack_version), body, rules)


def _stored_pack(request: Request, principal: Principal, domain: str, version: int) -> LivePack:
    """Any stored version of the caller's own pack - a draft, or an old one - for a what-if. Never audited."""
    cache = request.app.state.packs
    live = cache.get(principal.tenant_id, domain) if principal.allows(domain) else None
    if live is not None and live.version == version:
        return live
    db = request.app.state.db
    if db is None or not principal.allows(domain):
        raise pack_not_found(domain)
    row = packs.get_version(db, principal.tenant_id, domain, version)
    if row is None:
        raise pack_not_found(domain)
    try:
        pack, _ = publisher.parse(row["yaml"], domain)
    except publisher.LifecycleError as e:
        raise ApiError(e.status, e.code, e.message, e.details) from None
    return LivePack(compile_pack(pack), version)
