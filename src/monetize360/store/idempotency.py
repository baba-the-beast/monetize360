"""Idempotency keys in Postgres: a retried request gets the stored response, not a second price.

Kept 24 hours per (tenant, domain, key). The same key with a different body is a conflict (the caller reused a
key by mistake). The lookup takes an advisory lock held until the transaction ends, so two identical requests
arriving together produce one price: the second waits, then finds the first one's stored response."""
from __future__ import annotations

import hashlib
import json
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

KEEP = "24 hours"
_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"


def request_hash(domain: str, body: dict[str, Any]) -> bytes:
    """What "the same request" means: the domain and the body as the caller sent it."""
    text = json.dumps({"domain": domain, "body": body}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).digest()


def lookup(conn: psycopg.Connection, tenant_id: str, domain: str, key: str) -> dict | None:
    """Lock this key (until commit) and return {request_hash, response} if it was used in the last 24 hours."""
    conn.execute(_LOCK, (f"idem:{tenant_id}:{domain}:{key}",))
    row = conn.execute(
        "SELECT request_hash, response FROM idempotency_keys "
        "WHERE domain = %s AND key = %s AND created_at > now() - %s::interval", (domain, key, KEEP)).fetchone()
    return {"request_hash": bytes(row["request_hash"]), "response": row["response"]} if row else None


def save(conn: psycopg.Connection, tenant_id: str, domain: str, key: str, req_hash: bytes,
         response: dict[str, Any]) -> None:
    conn.execute("DELETE FROM idempotency_keys WHERE created_at <= now() - %s::interval", (KEEP,))
    conn.execute(
        "INSERT INTO idempotency_keys (tenant_id, domain, key, request_hash, response) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (tenant_id, domain, key) DO UPDATE SET request_hash = EXCLUDED.request_hash, "
        "response = EXCLUDED.response, created_at = now()",
        (tenant_id, domain, key, req_hash, Jsonb(response)))
