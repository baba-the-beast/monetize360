"""Pack versions (immutable), pack_pointer, tenant_policy queries.

A version's YAML never changes after it is saved (the application account has no UPDATE on it). What "live"
means is one row in pack_pointer, so publish and rollback are each one UPDATE and take effect atomically.
Every function that takes a tenant runs inside that tenant's Row-Level-Security context."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.types.json import Jsonb

from monetize360.engine import Policy
from monetize360.store.db import Database

_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"
_VERSION = ("version, status, yaml, owner, approver, lint_report, verify_report, created_at, approved_at, "
            "published_at")


def live_packs(db: Database) -> list[dict]:
    """Every tenant's live pack: [{tenant_id, domain, version, yaml}]. Runs before any tenant is known."""
    with db.service() as conn:
        return conn.execute("SELECT tenant_id, domain, version, yaml FROM list_live_packs()").fetchall()


def get_version(db: Database, tenant_id: str, domain: str, version: int) -> dict | None:
    """One stored version of this tenant's pack."""
    with db.tenant(tenant_id) as conn:
        return conn.execute(f"SELECT {_VERSION} FROM packs WHERE domain = %s AND version = %s",   # noqa: S608
                            (domain, version)).fetchone()


def versions(db: Database, tenant_id: str, domain: str) -> tuple[int | None, list[dict]]:
    """(live version or None, every version newest first — without the YAML)."""
    with db.tenant(tenant_id) as conn:
        live = conn.execute("SELECT live_version FROM pack_pointer WHERE domain = %s", (domain,)).fetchone()
        rows = conn.execute(
            "SELECT version, status, owner, approver, created_at, approved_at, published_at, "
            "(lint_report->>'ok')::boolean AS lint_ok, (verify_report->>'ok')::boolean AS verify_ok "
            "FROM packs WHERE domain = %s ORDER BY version DESC", (domain,)).fetchall()
    return (live["live_version"] if live else None), rows


def domains(db: Database, tenant_id: str) -> list[dict]:
    """This tenant's domains with their live version and newest version."""
    with db.tenant(tenant_id) as conn:
        return conn.execute(
            "SELECT p.domain, pp.live_version, max(p.version) AS latest_version, count(*) AS versions "
            "FROM packs p LEFT JOIN pack_pointer pp ON pp.tenant_id = p.tenant_id AND pp.domain = p.domain "
            "GROUP BY p.domain, pp.live_version ORDER BY p.domain").fetchall()


def policy(db: Database, tenant_id: str) -> tuple[Policy | None, list[str]]:
    """The tenant's limits (for the linter and verifier) and its banned signal names."""
    with db.tenant(tenant_id) as conn:
        row = conn.execute("SELECT min_price, max_price, max_surge_x, banned_signals FROM tenant_policy").fetchone()
    if row is None:
        return None, []
    limits = {k: row[k] for k in ("min_price", "max_price", "max_surge_x") if row[k] is not None}
    return (Policy(**limits) if limits else None), list(row["banned_signals"])


def save_draft(db: Database, tenant_id: str, domain: str, yaml_text: str, owner: str) -> int:
    """Store a new draft as the next version number. Returns it."""
    with db.tenant(tenant_id) as conn:
        conn.execute(_LOCK, (f"pack:{tenant_id}:{domain}",))           # two drafts at once get two numbers
        version = conn.execute("SELECT COALESCE(max(version), 0) + 1 AS v FROM packs WHERE domain = %s",
                               (domain,)).fetchone()["v"]
        conn.execute("INSERT INTO packs (tenant_id, domain, version, status, yaml, owner) "
                     "VALUES (%s, %s, %s, 'draft', %s, %s)", (tenant_id, domain, version, yaml_text, owner))
    return version


def save_report(db: Database, tenant_id: str, domain: str, version: int, column: str, report: dict[str, Any]) -> None:
    if column not in ("lint_report", "verify_report"):
        raise ValueError(column)
    with db.tenant(tenant_id) as conn:
        conn.execute(f"UPDATE packs SET {column} = %s WHERE domain = %s AND version = %s",   # noqa: S608 - whitelisted
                     (Jsonb(report), domain, version))


@contextmanager
def locked(db: Database, tenant_id: str, domain: str) -> Iterator[psycopg.Connection]:
    """A transaction holding this domain's lock: approve, publish and rollback read state and change it inside
    one, so two of them at once can't both act on what they saw before the other finished."""
    with db.tenant(tenant_id) as conn:
        conn.execute(_LOCK, (f"pack:{tenant_id}:{domain}",))
        yield conn


def row(conn: psycopg.Connection, domain: str, version: int) -> dict | None:
    return conn.execute(f"SELECT {_VERSION} FROM packs WHERE domain = %s AND version = %s",   # noqa: S608
                        (domain, version)).fetchone()


def live_version(conn: psycopg.Connection, domain: str) -> int | None:
    r = conn.execute("SELECT live_version FROM pack_pointer WHERE domain = %s", (domain,)).fetchone()
    return r["live_version"] if r else None


def mark_approved(conn: psycopg.Connection, domain: str, version: int, approver: str) -> None:
    conn.execute("UPDATE packs SET status = 'approved', approver = %s, approved_at = now() "
                 "WHERE domain = %s AND version = %s", (approver, domain, version))


def make_live(conn: psycopg.Connection, tenant_id: str, domain: str, version: int) -> int | None:
    """Point "live" at `version`; the version that was live becomes archived. Returns the previous live version."""
    previous = live_version(conn, domain)
    if previous is not None and previous != version:
        conn.execute("UPDATE packs SET status = 'archived' WHERE domain = %s AND version = %s", (domain, previous))
    conn.execute("UPDATE packs SET status = 'published', published_at = now() WHERE domain = %s AND version = %s",
                 (domain, version))
    conn.execute(
        "INSERT INTO pack_pointer (tenant_id, domain, live_version) VALUES (%s, %s, %s) "
        "ON CONFLICT (tenant_id, domain) DO UPDATE SET live_version = EXCLUDED.live_version, updated_at = now()",
        (tenant_id, domain, version))
    return previous
