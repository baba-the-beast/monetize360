"""The admin console (static files at /admin), the developer portal (/portal), and the two calls they need to
find their feet: GET /v1/me (whose key is this) and GET /v1/domains (what can it price, and what to send).

The console is a client of the same public API as everyone else: it holds an API key in the browser tab and
calls /v1/... with it. It has no session, no cookie and no route of its own that changes anything."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from monetize360.api.deps import current_principal
from monetize360.api.errors import jsonable
from monetize360.security.principal import Principal

router = APIRouter(tags=["console"])
FILES = Path(__file__).resolve().parents[1] / "console"

_HEADERS = {
    # only our own files may run or load: an injected <script> or an outside URL is refused by the browser
    "Content-Security-Policy": "default-src 'self'; object-src 'none'; base-uri 'none'; "
                               "frame-ancestors 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


class _Console(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.update(_HEADERS)
        return response


@router.get("/v1/me")
def me(principal: Principal = Depends(current_principal)) -> dict[str, Any]:   # noqa: B008
    """Who this key is: its tenant, role and domains. Any working key may ask."""
    return {"tenant_id": principal.tenant_id, "key_id": principal.key_id, "role": principal.role,
            "domains": sorted(principal.domains)}


@router.get("/v1/domains")
def domains(request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:   # noqa: B008
    """The domains this key can price right now, and the inputs each one takes. Any working key may ask.
    Only what a caller must send: no rules, no limits."""
    out = []
    for live in request.app.state.packs.for_tenant(principal.tenant_id):
        pack = live.compiled
        if not principal.allows(pack.domain):
            continue
        out.append({
            "domain": pack.domain, "currency": pack.currency, "pack_version": live.version,
            "needs_base_price": pack.base.op == "from_request",
            "signals": [{"name": s.name, "type": s.type, "required": not s.has_default,
                         "default": jsonable(s.default) if s.has_default else None,
                         "range": [jsonable(s.range[0]), jsonable(s.range[1])] if s.range else None,
                         "values": list(s.values) or None}
                        for s in pack.signals.values() if s.source == "request"]})
    return {"domains": out}


@router.get("/portal", include_in_schema=False)
def portal() -> FileResponse:
    return FileResponse(FILES / "portal.html", headers=_HEADERS)


def install(app: FastAPI) -> None:
    app.include_router(router)
    app.mount("/admin", _Console(directory=FILES, html=True), name="console")
