"""GET/PUT /v1/packs/{domain}; POST lint, verify, impact, approve, publish, rollback.

The rule lifecycle. An `editor` key drafts and checks; an `approver` key (a different one from the author)
approves, publishes and rolls back. Nothing here can reach another tenant: the tenant is the key's."""
from __future__ import annotations

import re
from datetime import timezone
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Body, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from monetize360.api.deps import require_principal
from monetize360.api.errors import ApiError, jsonable, pack_not_found
from monetize360.control import publisher
from monetize360.control.impact import impact
from monetize360.control.publisher import LifecycleError
from monetize360.control.verifier import verify
from monetize360.security.principal import Principal
from monetize360.store import audit, packs
from monetize360.store.db import Database

router = APIRouter(prefix="/v1/packs", tags=["packs"])
editor_key = require_principal("editor")
approver_key = require_principal("approver")
_DOMAIN = re.compile(r"[a-z][a-z0-9_]{1,39}")
Domain = Annotated[str, Path(description="Pack domain, e.g. hotel", json_schema_extra={"example": "hotel"})]
Version = Annotated[int, Path(ge=1, description="Version number")]

_EXAMPLE_PACK = """domain: hotel
currency: INR
signals:
  occupancy: {type: ratio, source: request, default: 0.5}
base: {op: from_request}
groups:
  - name: demand
    stacking: multiply
    rules:
      - {id: busy, when: "occupancy > 0.8", then: {op: pct, value: 30}, reason: "Occupancy above 80%"}
guards:
  floor: {op: pct_of_base, value: 70}
  ceiling: {op: abs, value: 5000}
rounding: {step: 50, mode: half_up}
"""


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"cases": 10000}]})
    cases: int = Field(default=10_000, ge=100, le=20_000)


class ImpactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"last_n": 1000}]})
    last_n: int = Field(default=1000, ge=1, le=10_000)


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"to_version": 1}]})
    to_version: int = Field(ge=1)


def _db(request: Request) -> Database:
    db = request.app.state.db
    if db is None:
        raise ApiError(503, "packs_unavailable", "this server runs without a database; packs come from files")
    return db


def _scope(principal: Principal, domain: str) -> None:
    """A domain outside the key's scope answers exactly like one that doesn't exist."""
    if not _DOMAIN.fullmatch(domain) or not principal.allows(domain):
        raise pack_not_found(domain)


def _api(e: LifecycleError) -> ApiError:
    return ApiError(e.status, e.code, e.message, e.details)


def _version(db: Database, principal: Principal, domain: str, version: int) -> dict:
    _scope(principal, domain)
    row = packs.get_version(db, principal.tenant_id, domain, version)
    if row is None:
        raise ApiError(404, "pack_not_found", f"no version {version} of '{domain}'")
    return row


def _when(t) -> str | None:
    return t.astimezone(timezone.utc).isoformat() if t else None


def _went_live(request: Request, principal: Principal, domain: str, result: dict[str, Any]) -> dict[str, Any]:
    """After publish or rollback: this replica switches now; the others are told (or find out by their poll)."""
    request.app.state.refresh_packs()
    events = request.app.state.events
    told = events.publish("pack_published", tenant_id=principal.tenant_id, domain=domain,
                          version=result["live_version"]) if events is not None else False
    return {**result, "broadcast": told}


@router.get("")
def list_domains(request: Request, principal: Principal = Depends(editor_key)) -> dict[str, Any]:   # noqa: B008
    """Your domains, each with its live version and newest version."""
    rows = packs.domains(_db(request), principal.tenant_id)
    return {"domains": [dict(r) for r in rows if principal.allows(r["domain"])]}


@router.get("/{domain}")
def list_versions(domain: Domain, request: Request,
                  principal: Principal = Depends(editor_key)) -> dict[str, Any]:   # noqa: B008
    _scope(principal, domain)
    live, rows = packs.versions(_db(request), principal.tenant_id, domain)
    if not rows:
        raise pack_not_found(domain)
    return {"domain": domain, "live_version": live, "versions": [
        {"version": r["version"], "status": r["status"], "owner": r["owner"], "approver": r["approver"],
         "lint_ok": r["lint_ok"], "verify_ok": r["verify_ok"], "created_at": _when(r["created_at"]),
         "approved_at": _when(r["approved_at"]), "published_at": _when(r["published_at"])} for r in rows]}


@router.put("/{domain}", status_code=201)
def save_draft(domain: Domain, request: Request,
               body: Annotated[bytes, Body(media_type="application/yaml", openapi_examples={
                   "hotel": {"summary": "A small hotel pack", "value": _EXAMPLE_PACK}})],
               principal: Principal = Depends(editor_key)) -> dict[str, Any]:   # noqa: B008
    """Save pack YAML as a new DRAFT version. It must pass the schema and compile, or nothing is stored and every
    problem is listed. A draft prices nothing until it is linted, verified, approved and published."""
    _scope(principal, domain)
    try:
        _, text = publisher.parse(body, domain)
    except LifecycleError as e:
        raise _api(e) from None
    version = packs.save_draft(_db(request), principal.tenant_id, domain, text, principal.key_id)
    return {"domain": domain, "version": version, "status": "draft", "owner": principal.key_id}


@router.get("/{domain}/{version}")
def get_version(domain: Domain, version: Version, request: Request,
                principal: Principal = Depends(editor_key)) -> dict[str, Any]:   # noqa: B008
    """One version: its YAML, its check reports, and how it differs from what is live now."""
    db = _db(request)
    row = _version(db, principal, domain, version)
    live, _ = packs.versions(db, principal.tenant_id, domain)
    live_row = packs.get_version(db, principal.tenant_id, domain, live) if live and live != version else None
    return {"domain": domain, "version": row["version"], "status": row["status"], "owner": row["owner"],
            "approver": row["approver"], "is_live": live == version, "live_version": live,
            "created_at": _when(row["created_at"]), "approved_at": _when(row["approved_at"]),
            "published_at": _when(row["published_at"]), "yaml": row["yaml"],
            "pack": jsonable(yaml.safe_load(row["yaml"])),          # the same rules as data, for a UI
            "lint_report": row["lint_report"], "verify_report": row["verify_report"],
            "diff_from_live": publisher.diff(live_row["yaml"], row["yaml"], f"v{live} (live)", f"v{version}")
            if live_row else ""}


@router.post("/{domain}/{version}/lint")
def lint_version(domain: Domain, version: Version, request: Request,
                 principal: Principal = Depends(editor_key)) -> dict[str, Any]:   # noqa: B008
    """Static checks, including fairness and your tenant's limits. Any error blocks approval."""
    db = _db(request)
    row = _version(db, principal, domain, version)
    report = jsonable(publisher.run_lint(db, principal.tenant_id, yaml.safe_load(row["yaml"])))
    packs.save_report(db, principal.tenant_id, domain, version, "lint_report", report)
    return report


@router.post("/{domain}/{version}/verify")
def verify_version(domain: Domain, version: Version, request: Request, body: VerifyRequest | None = None,
                   principal: Principal = Depends(editor_key)) -> dict[str, Any]:   # noqa: B008
    """Price thousands of random valid requests with this version and check every guarantee. Takes a few seconds."""
    db = _db(request)
    row = _version(db, principal, domain, version)
    report = jsonable(verify(yaml.safe_load(row["yaml"]), cases=(body or VerifyRequest()).cases,
                             policy=publisher.tenant_policy(db, principal.tenant_id)))
    packs.save_report(db, principal.tenant_id, domain, version, "verify_report", report)
    return report


@router.post("/{domain}/{version}/impact")
def impact_version(domain: Domain, version: Version, request: Request, body: ImpactRequest | None = None,
                   principal: Principal = Depends(editor_key)) -> dict[str, Any]:   # noqa: B008
    """Re-price your most recent real requests with this version: what would have changed, before anything ships."""
    db = _db(request)
    row = _version(db, principal, domain, version)
    live, _ = packs.versions(db, principal.tenant_id, domain)
    live_row = packs.get_version(db, principal.tenant_id, domain, live) if live else None
    records = audit.recent_requests(db, principal.tenant_id, domain, (body or ImpactRequest()).last_n)
    report = impact(yaml.safe_load(row["yaml"]), records,
                    live_pack=yaml.safe_load(live_row["yaml"]) if live_row else None,
                    policy=publisher.tenant_policy(db, principal.tenant_id))
    return {"domain": domain, "version": version, "compared_with_live_version": live, **jsonable(report)}


@router.post("/{domain}/{version}/approve")
def approve_version(domain: Domain, version: Version, request: Request,
                    principal: Principal = Depends(approver_key)) -> dict[str, Any]:   # noqa: B008
    """Sign off a draft. Needs a passing lint and verify, and an approver key that did not write it."""
    _scope(principal, domain)
    try:
        return publisher.approve(_db(request), principal.tenant_id, domain, version, principal.key_id)
    except LifecycleError as e:
        raise _api(e) from None


@router.post("/{domain}/{version}/publish")
def publish_version(domain: Domain, version: Version, request: Request,
                    principal: Principal = Depends(approver_key)) -> dict[str, Any]:   # noqa: B008
    """Make an approved version live. Every pricing replica switches to it."""
    _scope(principal, domain)
    try:
        result = publisher.publish(_db(request), principal.tenant_id, domain, version)
    except LifecycleError as e:
        raise _api(e) from None
    return _went_live(request, principal, domain, result)


@router.post("/{domain}/rollback")
def rollback_domain(domain: Domain, body: RollbackRequest, request: Request,
                    principal: Principal = Depends(approver_key)) -> dict[str, Any]:   # noqa: B008
    """Make a version that was live before live again. One call; the newer version is kept, just not live."""
    _scope(principal, domain)
    try:
        result = publisher.rollback(_db(request), principal.tenant_id, domain, body.to_version)
    except LifecycleError as e:
        raise _api(e) from None
    return _went_live(request, principal, domain, result)
