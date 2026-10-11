"""api_keys table queries (lookup by key_id, revoke)."""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from monetize360.security.keys import KeyRecord
from monetize360.store.db import Database

_COLUMNS = "key_id, tenant_id, secret_sha256, role, domains, rate_limit_rps, expires_at, revoked_at"


def _record(row: dict) -> KeyRecord:
    return KeyRecord(key_id=row["key_id"], tenant_id=row["tenant_id"], secret_sha256=bytes(row["secret_sha256"]),
                     role=row["role"], domains=tuple(row["domains"]), rate_limit_rps=row["rate_limit_rps"],
                     expires_at=row["expires_at"], revoked_at=row["revoked_at"])


def lookup(db: Database, key_id: str) -> KeyRecord | None:
    """Authentication: runs before the tenant is known, through the one function allowed to do that."""
    with db.service() as conn:
        row = conn.execute(f"SELECT {_COLUMNS} FROM lookup_api_key(%s)", (key_id,)).fetchone()   # noqa: S608
    return _record(row) if row else None


def create(db: Database, tenant_id: str, key_id: str, secret_sha256: bytes, role: str, domains: Sequence[str],
           *, rate_limit_rps: int = 200, expires_at: datetime | None = None, name: str | None = None,
           created_by: str | None = None) -> None:
    with db.tenant(tenant_id) as conn:
        conn.execute(
            "INSERT INTO api_keys (key_id, tenant_id, secret_sha256, role, domains, rate_limit_rps, expires_at, "
            "name, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (key_id, tenant_id, secret_sha256, role, list(domains), rate_limit_rps, expires_at, name, created_by))


class LastAdminKey(Exception):
    """Revoking this key would leave the tenant with no working admin key."""


def revoke(db: Database, tenant_id: str, key_id: str, *, revoked_by: str | None = None,
           keep_one_admin: bool = False) -> datetime | None:
    """Revoke one of THIS tenant's keys. Returns when it was revoked, or None if the tenant has no such key.
    With keep_one_admin, refuses (LastAdminKey) to revoke the tenant's only working admin key."""
    with db.tenant(tenant_id) as conn:
        if keep_one_admin:
            # lock the tenant's admin keys so two revokes at once can't each think the other key remains
            admins = conn.execute(
                "SELECT key_id FROM api_keys WHERE role = 'admin' AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > now()) FOR UPDATE").fetchall()
            if [a["key_id"] for a in admins] == [key_id]:
                raise LastAdminKey(key_id)
        row = conn.execute(
            "UPDATE api_keys SET revoked_by = COALESCE(revoked_by, %s), revoked_at = COALESCE(revoked_at, now()) "
            "WHERE key_id = %s RETURNING revoked_at", (revoked_by, key_id)).fetchone()
    return row["revoked_at"] if row else None


def list_keys(db: Database, tenant_id: str) -> list[dict]:
    """This tenant's keys, oldest first. Never the secret or its hash."""
    with db.tenant(tenant_id) as conn:
        return conn.execute(
            "SELECT key_id, name, role, domains, rate_limit_rps, created_at, created_by, expires_at, revoked_at, "
            "revoked_by FROM api_keys ORDER BY created_at, key_id").fetchall()
