"""The admin console: static files at /admin, and GET /v1/me so it can ask whose key it was given.

The console is a client of the same public API as everyone else: it holds an API key in the browser tab and
calls /v1/... with it. It has no session, no cookie and no route of its own that changes anything."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from monetize360.api.deps import current_principal
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


def install(app: FastAPI) -> None:
    app.include_router(router)
    app.mount("/admin", _Console(directory=FILES, html=True), name="console")
