"""POST /v1/admin/kill, /v1/admin/keys, /v1/admin/keys/{id}/revoke.

Built so far: the API-key endpoints (create, list, revoke). Kill switch and tenant policy arrive with M7.
All need an `admin` key and act only on the caller's own tenant."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from monetize360.api.deps import require_principal
from monetize360.api.errors import ApiError
from monetize360.security import keys as keylib
from monetize360.security.principal import ALL_DOMAINS, Principal
from monetize360.store import api_keys
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
