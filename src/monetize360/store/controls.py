"""Tenant limits and kill switches: the two things an operator can change that act on live prices at once.

Each pricing replica keeps them in memory (`Controls`), so a price request never queries for them and they keep
working if Postgres is down. Memory is refreshed from Postgres at startup, when a `controls_changed` broadcast
arrives, and by the same poll that picks up published packs.

  limits      tenant-wide min price, max price, max surge (x base): the engine's second guard layer
  kill switch per domain: normal | freeze (repeat each entity's last price) | base_only (rules off, guards on)"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from monetize360.engine import Policy
from monetize360.store.db import Database

MODES = ("normal", "freeze", "base_only")


def _policy(row: dict[str, Any]) -> Policy | None:
    limits = {k: Decimal(str(row[k])) for k in ("min_price", "max_price", "max_surge_x") if row.get(k) is not None}
    return Policy(**limits) if limits else None


class Controls:
    def __init__(self) -> None:
        self._limits: dict[str, Policy] = {}
        self._kills: dict[tuple[str, str], str] = {}

    def policy(self, tenant_id: str) -> Policy | None:
        return self._limits.get(tenant_id)

    def kill_mode(self, tenant_id: str, domain: str) -> str:
        return self._kills.get((tenant_id, domain), "normal")

    def apply(self, limit_rows: list[dict], kill_rows: list[dict]) -> None:
        """Replace everything with what Postgres says now. Each dict is swapped in one assignment."""
        self._limits = {r["tenant_id"]: p for r in limit_rows if (p := _policy(r)) is not None}
        self._kills = {(r["tenant_id"], r["domain"]): r["mode"] for r in kill_rows if r["mode"] in MODES[1:]}

    def refresh(self, db: Database) -> None:
        with db.service() as conn:
            limits = conn.execute("SELECT tenant_id, min_price, max_price, max_surge_x FROM list_tenant_limits()"
                                  ).fetchall()
            kills = conn.execute("SELECT tenant_id, domain, mode FROM list_kill_switches()").fetchall()
        self.apply(limits, kills)


def get_policy(db: Database, tenant_id: str) -> dict[str, Any]:
    with db.tenant(tenant_id) as conn:
        row = conn.execute("SELECT min_price, max_price, max_surge_x, banned_signals, require_two_person, "
                           "updated_by, updated_at FROM tenant_policy").fetchone()
        kills = conn.execute("SELECT domain, mode, set_by, set_at FROM kill_switch WHERE mode <> 'normal' "
                             "ORDER BY domain").fetchall()
    return {"policy": row, "kill_switches": kills}


def set_policy(db: Database, tenant_id: str, *, min_price: Decimal | None, max_price: Decimal | None,
               max_surge_x: Decimal | None, banned_signals: list[str], updated_by: str) -> None:
    """Replace the tenant's policy (a field left out is cleared)."""
    with db.tenant(tenant_id) as conn:
        conn.execute(
            "INSERT INTO tenant_policy (tenant_id, min_price, max_price, max_surge_x, banned_signals, updated_by) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id) DO UPDATE SET min_price = EXCLUDED.min_price, "
            "max_price = EXCLUDED.max_price, max_surge_x = EXCLUDED.max_surge_x, "
            "banned_signals = EXCLUDED.banned_signals, updated_by = EXCLUDED.updated_by, updated_at = now()",
            (tenant_id, min_price, max_price, max_surge_x, banned_signals, updated_by))


def set_kill(db: Database, tenant_id: str, domain: str, mode: str, set_by: str) -> str:
    """Set one domain's kill switch. Returns the mode it had before."""
    if mode not in MODES:
        raise ValueError(mode)
    with db.tenant(tenant_id) as conn:
        before = conn.execute("SELECT mode FROM kill_switch WHERE domain = %s FOR UPDATE", (domain,)).fetchone()
        conn.execute(
            "INSERT INTO kill_switch (tenant_id, domain, mode, set_by) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, domain) DO UPDATE SET mode = EXCLUDED.mode, set_by = EXCLUDED.set_by, "
            "set_at = now()", (tenant_id, domain, mode, set_by))
    return before["mode"] if before else "normal"


def live_pack_texts(db: Database, tenant_id: str) -> list[tuple[str, str]]:
    """(domain, YAML) of each of this tenant's live packs: what a new policy must not contradict."""
    with db.tenant(tenant_id) as conn:
        rows = conn.execute(
            "SELECT p.domain, p.yaml FROM pack_pointer pp JOIN packs p ON p.tenant_id = pp.tenant_id "
            "AND p.domain = pp.domain AND p.version = pp.live_version ORDER BY p.domain").fetchall()
    return [(r["domain"], r["yaml"]) for r in rows]
