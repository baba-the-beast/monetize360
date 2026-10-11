"""POST /v1/admin/kill, /v1/admin/keys, /v1/admin/keys/{id}/revoke.

API keys (create, list, revoke), the tenant's limits (GET/PUT /v1/admin/policy) and the kill switch
(POST /v1/admin/kill). All need an `admin` key and act only on the caller's own tenant."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from monetize360.api.deps import require_principal
from monetize360.api.errors import ApiError, jsonable, pack_not_found
from monetize360.api.schemas import Money
from monetize360.control import publisher
from monetize360.control.linter import lint
from monetize360.engine import Policy
from monetize360.security import keys as keylib
from monetize360.security.principal import ALL_DOMAINS, Principal
from monetize360.store import api_keys, controls
from monetize360.store.db import Database

router = APIRouter(prefix="/v1/admin", tags=["admin"])
admin_key = require_principal("admin")
_DOMAIN = re.compile(r"[a-z][a-z0-9_]{1,39}")
_KEY_ID = re.compile(r"[A-Z2-9]{10}")


class KeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [
        {"name": "checkout server", "role": "pricing", "domains": ["hotel"], "rate_limit_rps": 200}]})

    role: Literal["pricing", "editor", "approver", "admin"]
    domains: list[str] = Field(min_length=1, max_length=50, description='Domain names, or ["*"] for all')
    name: str | None = Field(default=None, min_length=1, max_length=80, description="What this key is for")
    rate_limit_rps: int = Field(default=200, ge=1, le=10_000)
    expires_at: datetime | None = Field(default=None, description="ISO-8601 with a timezone; default: never")
    env: Literal["live", "test"] = "live"

    @field_validator("domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        for d in v:
            if d != ALL_DOMAINS and not _DOMAIN.fullmatch(d):
                raise ValueError(f"'{d}' is not a domain name (lowercase letters, digits, _) or '*'")
        if len(set(v)) != len(v):
            raise ValueError("domains must not repeat")
        return v


def _db(request: Request) -> Database:
    db = request.app.state.db
    if db is None:
        raise ApiError(503, "keys_unavailable", "this server runs without a database, so it has no API keys")
    return db


def _status(row: dict, now: datetime) -> str:
    if row["revoked_at"] is not None:
        return "revoked"
    return "expired" if row["expires_at"] is not None and row["expires_at"] <= now else "active"


def _public(row: dict, now: datetime) -> dict[str, Any]:
    def iso(t: datetime | None) -> str | None:
        return t.astimezone(timezone.utc).isoformat() if t else None
    return {"key_id": row["key_id"], "name": row["name"], "role": row["role"], "domains": list(row["domains"]),
            "rate_limit_rps": row["rate_limit_rps"], "status": _status(row, now),
            "created_at": iso(row["created_at"]), "created_by": row["created_by"],
            "expires_at": iso(row["expires_at"]), "revoked_at": iso(row["revoked_at"]),
            "revoked_by": row["revoked_by"]}


@router.post("/keys", status_code=201)
def create_key(body: KeyCreate, request: Request, response: Response,
               principal: Principal = Depends(admin_key)) -> dict[str, Any]:   # noqa: B008
    """Create an API key for your tenant. The full key is in this response ONCE; only its hash is stored."""
    db = _db(request)
    # a key can't hand out more than it has itself: an admin key limited to `hotel` can't mint a key for `*`
    outside = [d for d in body.domains if not principal.allows(d)] if ALL_DOMAINS not in principal.domains else []
    if outside:
        raise ApiError(403, "forbidden", "your key doesn't cover these domains, so it can't create a key for them",
                       [{"path": "domains", "value": outside}])
    now = datetime.now(timezone.utc)
    if body.expires_at is not None:
        if body.expires_at.tzinfo is None:
            raise ApiError(400, "bad_request", "expires_at must include a timezone", [{"path": "expires_at"}])
        if body.expires_at <= now:
            raise ApiError(400, "bad_request", "expires_at must be in the future", [{"path": "expires_at"}])

    key_id, api_key, secret_hash = keylib.generate(body.env)
    api_keys.create(db, principal.tenant_id, key_id, secret_hash, body.role, body.domains,
                    rate_limit_rps=body.rate_limit_rps, expires_at=body.expires_at, name=body.name,
                    created_by=principal.key_id)
    response.headers["Cache-Control"] = "no-store"          # the secret must not sit in any cache
    return {"key_id": key_id, "api_key": api_key, "name": body.name, "role": body.role, "domains": body.domains,
            "rate_limit_rps": body.rate_limit_rps,
            "expires_at": body.expires_at.astimezone(timezone.utc).isoformat() if body.expires_at else None,
            "note": "Store this key now. It is shown once and cannot be recovered."}


@router.get("/keys")
def list_keys(request: Request, principal: Principal = Depends(admin_key)) -> dict[str, Any]:   # noqa: B008
    """Your tenant's keys with their status. Never the secret."""
    now = datetime.now(timezone.utc)
    return {"keys": [_public(r, now) for r in api_keys.list_keys(_db(request), principal.tenant_id)]}


@router.post("/keys/{key_id}/revoke")
def revoke_key(key_id: str, request: Request, principal: Principal = Depends(admin_key)) -> dict[str, Any]:   # noqa: B008
    """Revoke a key. This replica refuses it at once; the others are told by broadcast (or, if the broadcast is
    lost, within the 60 s key cache)."""
    db = _db(request)
    not_found = ApiError(404, "key_not_found", f"no key '{key_id}'")       # not yours looks the same
    if not _KEY_ID.fullmatch(key_id):
        raise not_found
    try:
        revoked_at = api_keys.revoke(db, principal.tenant_id, key_id, revoked_by=principal.key_id,
                                     keep_one_admin=True)
    except api_keys.LastAdminKey:
        raise ApiError(409, "last_admin_key", "this is your only working admin key; create another one first, "
                                              "or you would lock yourself out") from None
    if revoked_at is None:
        raise not_found
    request.app.state.keys.evict(key_id)
    events = request.app.state.events
    broadcast = events.publish("key_revoked", key_id=key_id) if events is not None else False
    return {"key_id": key_id, "revoked_at": revoked_at.astimezone(timezone.utc).isoformat(),
            "broadcast": broadcast}


# ---------------------------------------------------------------- tenant limits

_SIGNAL = re.compile(r"[a-z][a-z0-9_]{0,39}")
MAX_MONEY = Decimal(10) ** 12


class PolicyUpdate(BaseModel):
    """The whole policy: a field left out is cleared (PUT replaces)."""
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [
        {"max_surge_x": "2.0", "banned_signals": ["customer_gender"]}]})

    min_price: Money | None = Field(default=None, description="No price below this, in any domain")
    max_price: Money | None = Field(default=None, description="No price above this, in any domain")
    max_surge_x: Money | None = Field(default=None, description="No price above this many times its base price")
    banned_signals: list[str] = Field(default_factory=list, max_length=100)
    require_two_person: bool = True

    @field_validator("banned_signals")
    @classmethod
    def _signals(cls, v: list[str]) -> list[str]:
        for name in v:
            if not _SIGNAL.fullmatch(name):
                raise ValueError(f"'{name}' is not a signal name")
        return sorted(set(v))


def _check_policy(body: PolicyUpdate) -> Policy | None:
    def bad(path: str, why: str) -> ApiError:
        return ApiError(400, "bad_request", why, [{"path": path}])

    if not body.require_two_person:
        raise bad("require_two_person", "two-person approval cannot be switched off: the database enforces it")
    for name in ("min_price", "max_price"):
        value = getattr(body, name)
        if value is not None and not Decimal(0) < value <= MAX_MONEY:
            raise bad(name, f"{name} must be greater than 0 and at most {MAX_MONEY}")
    if body.max_surge_x is not None and not Decimal(0) < body.max_surge_x <= 1000:
        raise bad("max_surge_x", "max_surge_x must be greater than 0 and at most 1000")
    if body.min_price is not None and body.max_price is not None and body.min_price > body.max_price:
        raise bad("min_price", "min_price is above max_price")
    limits = {k: v for k in ("min_price", "max_price", "max_surge_x") if (v := getattr(body, k)) is not None}
    return Policy(**limits) if limits else None


def _policy_view(db: Database, tenant_id: str) -> dict[str, Any]:
    state = controls.get_policy(db, tenant_id)
    row = state["policy"] or {}
    return {
        "min_price": jsonable(row.get("min_price")), "max_price": jsonable(row.get("max_price")),
        "max_surge_x": jsonable(row.get("max_surge_x")), "banned_signals": list(row.get("banned_signals") or []),
        "require_two_person": True,
        "updated_by": row.get("updated_by"),
        "updated_at": row["updated_at"].astimezone(timezone.utc).isoformat() if row.get("updated_at") else None,
        "kill_switches": [{"domain": k["domain"], "mode": k["mode"], "set_by": k["set_by"],
                           "set_at": k["set_at"].astimezone(timezone.utc).isoformat()}
                          for k in state["kill_switches"]],
    }


def _changed(request: Request) -> bool:
    """This replica applies the change now; the others are told, or find out by their poll."""
    request.app.state.refresh_controls()
    events = request.app.state.events
    return events.publish("controls_changed") if events is not None else False


@router.get("/policy")
def get_policy(request: Request, principal: Principal = Depends(admin_key)) -> dict[str, Any]:   # noqa: B008
    """Your tenant's limits and any kill switch that is on."""
    return _policy_view(_db(request), principal.tenant_id)


@router.put("/policy")
def put_policy(body: PolicyUpdate, request: Request,
               principal: Principal = Depends(admin_key)) -> dict[str, Any]:   # noqa: B008
    """Replace your tenant's limits. They apply to every price in every domain at once, on top of each pack's own
    guards (the strictest wins). Refused if they contradict a pack that is live."""
    db = _db(request)
    policy = _check_policy(body)
    conflicts = []
    for domain, text in controls.live_pack_texts(db, principal.tenant_id):
        pack, _ = publisher.parse(text, domain)
        report = lint(pack, policy=policy, banned_signals=body.banned_signals, as_of=datetime.now(timezone.utc))
        conflicts += [{"path": f"{domain}/{e['path']}", "message": e["message"]} for e in report["errors"]
                      if e["code"] in ("tenant_limit", "banned_signal")]
    if conflicts:
        raise ApiError(409, "policy_conflict", "these limits contradict rules that are live; "
                                               "change the pack first, or choose other limits", conflicts)
    controls.set_policy(db, principal.tenant_id, min_price=body.min_price, max_price=body.max_price,
                        max_surge_x=body.max_surge_x, banned_signals=body.banned_signals,
                        updated_by=principal.key_id)
    broadcast = _changed(request)
    return {**_policy_view(db, principal.tenant_id), "broadcast": broadcast}


# ---------------------------------------------------------------- kill switch

class KillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"domain": "hotel", "mode": "freeze"}]})
    domain: str = Field(min_length=1, max_length=40)
    mode: Literal["normal", "freeze", "base_only"]


@router.post("/kill")
def kill_switch(body: KillRequest, request: Request,
                principal: Principal = Depends(admin_key)) -> dict[str, Any]:   # noqa: B008
    """Stop dynamic pricing for one domain, at once, on every server.
    `freeze`: each thing keeps the last price it was given (503 if it never had one).
    `base_only`: rules are off; base price, guards and rounding still apply. `normal`: back to the live rules."""
    db = _db(request)
    if not principal.allows(body.domain) or request.app.state.packs.get(principal.tenant_id, body.domain) is None:
        raise pack_not_found(body.domain)
    previous = controls.set_kill(db, principal.tenant_id, body.domain, body.mode, principal.key_id)
    return {"domain": body.domain, "mode": body.mode, "previous_mode": previous, "broadcast": _changed(request)}
