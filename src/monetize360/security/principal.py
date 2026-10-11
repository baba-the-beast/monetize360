"""Principal = (tenant_id, key_id, role, allowed domains). Built only from the API key, never from the request body."""
from __future__ import annotations

from dataclasses import dataclass

ROLES = ("pricing", "editor", "approver", "admin")
ALL_DOMAINS = "*"


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    key_id: str
    role: str
    domains: frozenset[str]          # domain names, or {"*"} for every domain of the tenant

    def allows(self, domain: str) -> bool:
        return ALL_DOMAINS in self.domains or domain in self.domains
