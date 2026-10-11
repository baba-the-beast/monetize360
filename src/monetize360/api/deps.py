"""FastAPI dependencies: require_principal(role), rate limit, domain scope check (404 if not allowed).

Who is calling comes ONLY from the API key (`Authorization: Bearer m360_...`): the key gives the tenant, the
role and the domains. Nothing in the URL or body can change the tenant. When the app runs without a database
(DEV_TENANT, local development only) every caller is that one dev tenant instead."""
from __future__ import annotations

import math
from typing import Callable

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from monetize360.api.errors import ApiError, pack_not_found, unauthorized
from monetize360.security.keys import KeyStoreUnavailable, KeyVerifier
from monetize360.security.principal import ALL_DOMAINS, Principal
from monetize360.store.packcache import LivePack, PackCache

_RANK = {"pricing": 0, "editor": 1, "approver": 2, "admin": 3}
DEFAULT_RPS = 200
# Declares the scheme in OpenAPI, which gives Swagger its "Authorize" button. auto_error=False: we answer in our
# own error format, and a missing header is fine in dev-tenant mode.
bearer = HTTPBearer(auto_error=False, bearerFormat="m360 API key",
                    description="Paste an API key: m360_live_<key_id>_<secret>")


def current_principal(request: Request,
                      credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:   # noqa: B008
    verifier: KeyVerifier | None = request.app.state.keys
    if verifier is None:                                   # no database: dev tenant, or closed
        dev_tenant = request.app.state.dev_tenant
        if not dev_tenant:
            raise unauthorized()
        return Principal(tenant_id=dev_tenant, key_id="dev", role="admin", domains=frozenset({ALL_DOMAINS}))

    if credentials is None:                                # no header, or not "Bearer ..."
        raise unauthorized()
    try:
        principal = verifier.verify(credentials.credentials.strip())
    except KeyStoreUnavailable:
        raise ApiError(503, "auth_unavailable", "can't check API keys right now; retry shortly",
                       headers={"Retry-After": "5"}) from None
    if principal is None:
        raise unauthorized()                               # unknown, wrong, expired and revoked all look the same

    wait = request.app.state.limiter.allow(principal.key_id, verifier.rate_limit_rps(principal.key_id) or DEFAULT_RPS)
    if wait > 0:
        raise ApiError(429, "rate_limited", "too many requests for this key",
                       headers={"Retry-After": str(max(1, math.ceil(wait)))})
    return principal


def require_principal(role: str) -> Callable[..., Principal]:
    """Dependency: the caller's principal, which must hold `role` or a higher one."""
    def dependency(principal: Principal = Depends(current_principal)) -> Principal:   # noqa: B008
        if _RANK[principal.role] < _RANK[role]:
            raise ApiError(403, "forbidden", f"this key's role '{principal.role}' can't call this endpoint")
        return principal
    return dependency


def live_pack(request: Request, principal: Principal, domain: str) -> LivePack:
    """The tenant's live pack for `domain`. Tenant comes from the key only. A domain outside the key's scope,
    one that isn't live and one that belongs to another tenant all give the same 404."""
    cache: PackCache = request.app.state.packs
    pack = cache.get(principal.tenant_id, domain) if principal.allows(domain) else None
    if pack is None:
        raise pack_not_found(domain)
    return pack
