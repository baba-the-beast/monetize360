"""Pack versions (immutable), pack_pointer, tenant_policy queries.

M4: only the read the pricing replicas need at startup. Drafts, approval, publish and rollback are M5."""
from __future__ import annotations

from monetize360.store.db import Database


def live_packs(db: Database) -> list[dict]:
    """Every tenant's live pack: [{tenant_id, domain, version, yaml}]. Runs before any tenant is known."""
    with db.service() as conn:
        return conn.execute("SELECT tenant_id, domain, version, yaml FROM list_live_packs()").fetchall()
