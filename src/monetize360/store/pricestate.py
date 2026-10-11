"""The max_change anchor and the last price of every priced thing (ENGINE_SPEC 6.2), in Postgres.

`max_change: {pct: 15, per: 1h}` = the price may move at most 15% from the price at the START of the current
window. So per (tenant, domain, entity) we keep the anchor (price + when its window began) and the last price.
When a window is over, the next anchor is the last price. Comparing with the last price every time instead
would let the price creep 15% on every request.

Requests for the same entity are serialised by an advisory lock held until the transaction ends, so two
servers pricing the same room at once still see one consistent anchor."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import NamedTuple

import psycopg

_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"


class Anchor(NamedTuple):
    reference: Decimal | None        # what to pass to the engine as reference_price (None = nothing to compare)
    price: Decimal | None            # the anchor to store afterwards (None = "use the price being issued now")
    time: datetime


def lock_and_read(conn: psycopg.Connection, tenant_id: str, domain: str, entity: str) -> dict | None:
    """Take this entity's lock (until commit) and return its state row, if any."""
    conn.execute(_LOCK, (f"state:{tenant_id}:{domain}:{entity}",))
    return conn.execute(
        "SELECT anchor_price, anchor_time, last_price, last_time FROM price_state "
        "WHERE domain = %s AND entity = %s", (domain, entity)).fetchone()


def anchor_for(state: dict | None, as_of: datetime, per: timedelta | None) -> Anchor:
    """Pure: which reference price applies at `as_of`, and what the anchor becomes."""
    if state is None or per is None:
        return Anchor(None, None, as_of)                       # first price, or the pack has no max_change
    if as_of - state["anchor_time"] > per:
        return Anchor(state["last_price"], state["last_price"], as_of)     # window over: restart from the last price
    return Anchor(state["anchor_price"], state["anchor_price"], state["anchor_time"])


def save(conn: psycopg.Connection, tenant_id: str, domain: str, entity: str, price: Decimal, as_of: datetime,
         anchor: Anchor) -> None:
    conn.execute(
        "INSERT INTO price_state (tenant_id, domain, entity, anchor_price, anchor_time, last_price, last_time) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (tenant_id, domain, entity) DO UPDATE SET anchor_price = EXCLUDED.anchor_price, "
        "anchor_time = EXCLUDED.anchor_time, last_price = EXCLUDED.last_price, last_time = EXCLUDED.last_time",
        (tenant_id, domain, entity, anchor.price if anchor.price is not None else price, anchor.time, price, as_of))
