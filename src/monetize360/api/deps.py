"""FastAPI dependencies: require_principal(role), rate limit, domain scope check (404 if not allowed).

M3: there are no API keys yet, so the only principal is the dev tenant from settings (DEV_TENANT). Without it
every call is 401 — the API is closed by default. M4 replaces the body of `current_principal` with key
verification; routes don't change. Rate limiting arrives with Redis (M4)."""
from __future__ import annotations

from typing import Callable

from fastapi import Depends, Request

from monetize360.api.errors import ApiError, pack_not_found, unauthorized
from monetize360.security.principal import ALL_DOMAINS, Principal
from monetize360.store.packcache import LivePack, PackCache

_RANK = {"pricing": 0, "editor": 1, "approver": 2, "admin": 3}


def current_principal(request: Request) -> Principal:
    dev_tenant = request.app.state.dev_tenant
    if not dev_tenant:
        raise unauthorized()
    return Principal(tenant_id=dev_tenant, key_id="dev", role="admin", domains=frozenset({ALL_DOMAINS}))


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
