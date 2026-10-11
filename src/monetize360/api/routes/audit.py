"""GET /v1/audit/{price_id}, POST /v1/audit/verify, GET /v1/audit/export, POST /v1/replay."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import yaml
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from monetize360.api.deps import require_principal
from monetize360.api.errors import ApiError, jsonable, pack_not_found
from monetize360.control.replay import replay
from monetize360.security.principal import Principal
from monetize360.store import audit, packs
from monetize360.store.db import Database

router = APIRouter(prefix="/v1", tags=["audit"])
admin_key = require_principal("admin")
EXPORT_LIMIT = 10_000


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"domain": "hotel"}]})
    domain: str = Field(min_length=1, max_length=40)


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"price_id": "01JA7Z3K9X..."}]})
    price_id: str = Field(min_length=1, max_length=64)
    pack_version: int | None = Field(default=None, ge=1, description="default: the version that set the price")


def _db(request: Request) -> Database:
    db = request.app.state.db
    if db is None:
        raise ApiError(503, "audit_unavailable", "this server runs without a database, so nothing is audited")
    return db


def _own_domain(principal: Principal, domain: str) -> None:
    if not principal.allows(domain):
        raise pack_not_found(domain)


def _record(db: Database, principal: Principal, price_id: str) -> dict[str, Any]:
    """This tenant's record, or the same 404 whether it doesn't exist or belongs to someone else."""
    record = audit.get(db, principal.tenant_id, price_id)
    if record is None or not principal.allows(record["domain"]):
        raise ApiError(404, "price_not_found", f"no price '{price_id}'")
    return record


def _public(record: dict[str, Any], pack: dict | None) -> dict[str, Any]:
    return {
        "price_id": record["price_id"], "domain": record["domain"], "seq": record["seq"],
        "entity": record["entity"], "key_id": record["key_id"],
        "as_of": record["as_of"].astimezone(timezone.utc).isoformat(),
        "recorded_at": record["created_at"].astimezone(timezone.utc).isoformat(),
        "pack_version": record["pack_version"],
        "pack_owner": pack["owner"] if pack else None, "pack_approver": pack["approver"] if pack else None,
        "inputs": record["inputs"], "signals_used": record["signal_sources"],
        "reference_price": jsonable(record["reference_price"]),
        "output": record["output"],
        "prev_hash": bytes(record["prev_hash"]).hex(), "hash": bytes(record["hash"]).hex(),
    }


@router.get("/audit/export")
def export(request: Request, domain: Annotated[str, Query(min_length=1, max_length=40)],
           format: Literal["json", "csv"] = "json",
           start: Annotated[datetime | None, Query(alias="from")] = None,
           end: Annotated[datetime | None, Query(alias="to")] = None,
           principal: Principal = Depends(admin_key)) -> Response:   # noqa: B008
    """Take your data with you: every audited price of one domain, oldest first (up to 10,000 rows per call)."""
    _own_domain(principal, domain)
    records = audit.rows(_db(request), principal.tenant_id, domain, start=start, end=end, limit=EXPORT_LIMIT + 1)
    truncated = len(records) > EXPORT_LIMIT
    flat = list(audit.iter_export(records[:EXPORT_LIMIT]))
    headers = {"X-Truncated": str(truncated).lower()}
    if format == "json":
        return Response(json.dumps({"domain": domain, "rows": flat, "truncated": truncated}),
                        media_type="application/json", headers=headers)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(flat[0]) if flat else ["seq"])
    writer.writeheader()
    for row in flat:
        # a spreadsheet would run a cell that starts with = + - @ as a formula: neutralise caller-supplied text
        writer.writerow({k: ("'" + v if isinstance(v, str) and v[:1] in "=+-@\t\r" else v) for k, v in row.items()})
    headers["Content-Disposition"] = f'attachment; filename="audit-{domain}.csv"'
    return Response(out.getvalue(), media_type="text/csv", headers=headers)


@router.get("/audit/{price_id}")
def get_record(price_id: str, request: Request, principal: Principal = Depends(admin_key)) -> dict[str, Any]:   # noqa: B008
    """Everything about one price: inputs, where each signal came from, the rule version and who approved it."""
    db = _db(request)
    record = _record(db, principal, price_id)
    pack = packs.get_version(db, principal.tenant_id, record["domain"], record["pack_version"])
    return _public(record, pack)


@router.post("/audit/verify")
def verify_chain(body: VerifyRequest, request: Request,
                 principal: Principal = Depends(admin_key)) -> dict[str, Any]:   # noqa: B008
    """Recompute the whole hash chain of one domain. Any edited, removed or inserted row is reported."""
    _own_domain(principal, body.domain)
    return audit.verify(_db(request), principal.tenant_id, body.domain)


@router.post("/replay")
def replay_price(body: ReplayRequest, request: Request,
                 principal: Principal = Depends(admin_key)) -> dict[str, Any]:   # noqa: B008
    """Re-run a past price from its record. Same version -> must be identical. Another version -> what would
    have changed. Never writes anything."""
    db = _db(request)
    record = _record(db, principal, body.price_id)
    version = body.pack_version or record["pack_version"]
    pack = packs.get_version(db, principal.tenant_id, record["domain"], version)
    if pack is None:
        raise pack_not_found(record["domain"])
    result = replay(record, yaml.safe_load(pack["yaml"]))
    return {"price_id": record["price_id"], "recorded_pack_version": record["pack_version"],
            "replayed_pack_version": version, **result}
