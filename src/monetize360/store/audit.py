"""Audit queries: get by price_id, export CSV/JSON, verify hash chain, recent requests for impact preview.

Every price is one row, appended in the SAME request that returned it (Postgres is durable and local; Redis is
not used). Rows of one (tenant, domain) form a chain:

    hash = SHA-256( prev_hash + canonical_json(row) )        first row: prev_hash = 32 zero bytes

so changing, removing or inserting a row breaks every hash after it. Appends to one chain are serialised by a
Postgres advisory lock, which also works across replicas. If Postgres can't be reached the row is spooled to a
local file and appended when it is back (`AuditLog`); the price is still returned.

What the chain does NOT catch: deleting the newest rows (the tail). Publishing the head hash somewhere else
from time to time would; that is not built."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable, Iterator

import psycopg
from psycopg.types.json import Jsonb

from monetize360.store.db import Database

log = logging.getLogger("monetize360.audit")

GENESIS = bytes(32)
# ponytail: one lock per (tenant, domain) chain means ~one append at a time per chain (about 1 ms each).
# If a single domain needs thousands of prices per second, batch appends in a worker instead.
_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"
_COLUMNS = ("price_id, tenant_id, domain, seq, pack_version, entity, key_id, as_of, inputs, signal_sources, "
            "reference_price, output, prev_hash, hash, created_at")


@dataclass(frozen=True)
class AuditEvent:
    """Everything needed to explain and exactly replay one price."""
    price_id: str
    tenant_id: str
    domain: str
    pack_version: int
    entity: str | None
    key_id: str | None
    as_of: datetime
    inputs: dict[str, Any]              # base_price, signals, redis_signals, allow_public_override, policy
    signal_sources: dict[str, Any]      # what the engine actually used, and where each value came from
    reference_price: Decimal | None
    output: dict[str, Any]              # price, currency, waterfall, bounds, flags


def plain(v: Any) -> Any:
    """Values safe to store in jsonb AND get back unchanged: money and floats become text (a float would come
    back with different digits and break the hash), dates become ISO text; ints, bools and text stay."""
    if isinstance(v, bool) or v is None or isinstance(v, (str, int)):
        return v
    if isinstance(v, (Decimal, float)):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [plain(x) for x in v]
    return str(v)


def _money(v: Any) -> str | None:
    return None if v is None else f"{Decimal(str(v)):.2f}"


def canonical(row: dict[str, Any]) -> bytes:
    """The exact bytes that are hashed: the same for a row about to be written and for one read back."""
    body = {
        "price_id": row["price_id"], "tenant_id": row["tenant_id"], "domain": row["domain"], "seq": row["seq"],
        "pack_version": row["pack_version"], "entity": row["entity"], "key_id": row["key_id"],
        "as_of": row["as_of"].astimezone(timezone.utc).isoformat(),
        "inputs": row["inputs"], "signal_sources": row["signal_sources"],
        "reference_price": _money(row["reference_price"]), "output": row["output"],
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def chain_hash(prev_hash: bytes, row: dict[str, Any]) -> bytes:
    return hashlib.sha256(bytes(prev_hash) + canonical(row)).digest()


def _row(event: AuditEvent, seq: int) -> dict[str, Any]:
    ref = event.reference_price
    if ref is not None:                 # two decimals BEFORE hashing, the same way the numeric(14,2) column keeps it
        ref = Decimal(str(ref)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {**asdict(event), "seq": seq, "reference_price": ref, "inputs": plain(event.inputs),
            "signal_sources": plain(event.signal_sources), "output": plain(event.output)}


def append_in(conn: psycopg.Connection, event: AuditEvent) -> tuple[int, bytes] | None:
    """Append to the event's chain inside the caller's tenant transaction (so the row commits together with
    whatever else that transaction writes). Returns (seq, hash), or None when this price_id is already recorded."""
    conn.execute(_LOCK, (f"{event.tenant_id}:{event.domain}",))
    if conn.execute("SELECT 1 FROM audit_log WHERE price_id = %s", (event.price_id,)).fetchone():
        return None
    tail = conn.execute(
        "SELECT seq, hash FROM audit_log WHERE tenant_id = %s AND domain = %s ORDER BY seq DESC LIMIT 1",
        (event.tenant_id, event.domain)).fetchone()
    seq, prev = (tail["seq"] + 1, bytes(tail["hash"])) if tail else (1, GENESIS)
    row = _row(event, seq)
    digest = chain_hash(prev, row)
    conn.execute(
        "INSERT INTO audit_log (price_id, tenant_id, domain, seq, pack_version, entity, key_id, as_of, inputs, "
        "signal_sources, reference_price, output, prev_hash, hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (row["price_id"], row["tenant_id"], row["domain"], seq, row["pack_version"], row["entity"],
         row["key_id"], row["as_of"], Jsonb(row["inputs"]), Jsonb(row["signal_sources"]),
         row["reference_price"], Jsonb(row["output"]), prev, digest))
    return seq, digest


def append(db: Database, event: AuditEvent) -> tuple[int, bytes] | None:
    """Append to the event's chain in its own transaction."""
    with db.tenant(event.tenant_id) as conn:
        return append_in(conn, event)


def get(db: Database, tenant_id: str, price_id: str) -> dict[str, Any] | None:
    with db.tenant(tenant_id) as conn:
        return conn.execute(f"SELECT {_COLUMNS} FROM audit_log WHERE price_id = %s", (price_id,)).fetchone()   # noqa: S608


def rows(db: Database, tenant_id: str, domain: str, *, start: datetime | None = None,
         end: datetime | None = None, limit: int = 10_000) -> list[dict[str, Any]]:
    """One chain in order, optionally limited to prices with start <= as_of < end."""
    with db.tenant(tenant_id) as conn:
        return conn.execute(
            f"SELECT {_COLUMNS} FROM audit_log WHERE domain = %s "   # noqa: S608
            "AND (%s::timestamptz IS NULL OR as_of >= %s) AND (%s::timestamptz IS NULL OR as_of < %s) "
            "ORDER BY seq LIMIT %s", (domain, start, start, end, end, limit)).fetchall()


def verify(db: Database, tenant_id: str, domain: str) -> dict[str, Any]:
    """Walk the whole chain. The first row that doesn't fit is reported with the reason."""
    prev, expected_seq, checked = GENESIS, 1, 0
    with db.tenant(tenant_id) as conn, conn.cursor(name="audit_verify") as cur:      # server-side: any chain size
        cur.execute(f"SELECT {_COLUMNS} FROM audit_log WHERE domain = %s ORDER BY seq, created_at", (domain,))   # noqa: S608
        for row in cur:
            problem = None
            if row["seq"] != expected_seq:
                problem = (f"expected row {expected_seq} but found row {row['seq']}: "
                           + ("a row was removed" if row["seq"] > expected_seq else "a row was added or duplicated"))
            elif bytes(row["prev_hash"]) != prev:
                problem = "this row doesn't link to the row before it (an earlier row was replaced)"
            elif bytes(row["hash"]) != chain_hash(prev, row):
                problem = "this row's contents were changed after it was written"
            if problem:
                return {"ok": False, "rows_checked": checked, "first_broken_seq": row["seq"],
                        "price_id": row["price_id"], "reason": problem}
            prev, expected_seq, checked = bytes(row["hash"]), expected_seq + 1, checked + 1
    return {"ok": True, "rows_checked": checked, "head_hash": prev.hex() if checked else None}


# ---------------------------------------------------------------- never lose a record: spool when Postgres is down

class AuditLog:
    """`record(event)` appends to Postgres, or to a local spool file when Postgres can't be reached.
    `drain()` moves spooled records into the chain; a background thread calls it every few seconds."""

    RETRY_AFTER = 5.0        # after a failure, don't make every request wait on the database again for this long

    def __init__(self, db: Database, spool_dir: Path, *, monotonic: Callable[[], float] = time.monotonic):
        self._db, self._monotonic = db, monotonic
        # one folder per database: a record spooled for one database is never drained into another
        self._dir = Path(spool_dir) / db.fingerprint
        self._spool = self._dir / f"audit-{os.getpid()}.jsonl"
        self._file_lock = threading.Lock()
        self._down_until = 0.0
        self._stop = threading.Event()

    @property
    def db(self) -> Database:
        return self._db

    def available(self) -> bool:
        """False for a few seconds after a failure, so every request doesn't wait on a database that is down."""
        return self._monotonic() >= self._down_until

    def mark_down(self, error: Exception) -> None:
        self._down_until = self._monotonic() + self.RETRY_AFTER
        log.error("database unreachable, degraded for %.0f s (%s)", self.RETRY_AFTER, type(error).__name__)

    def spool(self, event: AuditEvent) -> None:
        """Keep a record on local disk until Postgres is back."""
        self._write_spool([event])

    def record(self, event: AuditEvent) -> bool:
        """True when the row is in Postgres now; False when it was spooled and will be appended later."""
        if self.available():
            try:
                append(self._db, event)
                return True
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:      # unreachable, not a query bug
                self.mark_down(e)
        self.spool(event)
        return False

    def pending(self) -> int:
        return sum(sum(1 for _ in f.open(encoding="utf-8")) for f in self._dir.glob("audit-*.jsonl"))

    def _write_spool(self, events: list[AuditEvent]) -> None:
        with self._file_lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            with self._spool.open("a", encoding="utf-8") as f:
                for e in events:
                    f.write(json.dumps(plain(asdict(e)), sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())           # on disk before the price is returned

    def drain(self) -> int:
        """Append every spooled record (from this and earlier processes), oldest first. Returns how many."""
        done = 0
        for path in sorted(self._dir.glob("audit-*.jsonl")):
            with self._file_lock:
                lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
                left = list(lines)
                try:
                    for line in lines:
                        append(self._db, _event(json.loads(line)))      # a price_id already recorded is skipped
                        left.pop(0)
                        done += 1
                except psycopg.Error:
                    pass                                                # still down: keep the rest for next time
                if left:
                    path.write_text("\n".join(left) + "\n", encoding="utf-8")
                else:
                    path.unlink(missing_ok=True)
        if done:
            self._down_until = 0.0
            log.info("audit: %d spooled record(s) appended", done)
        return done

    def start_draining(self, interval: float = 5.0) -> None:
        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    if any(self._dir.glob("audit-*.jsonl")):
                        self.drain()
                except Exception:      # noqa: BLE001 - the drainer must survive anything and try again
                    log.exception("audit: drain failed")
        threading.Thread(target=loop, name="audit-drain", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def _event(d: dict[str, Any]) -> AuditEvent:
    ref = d.get("reference_price")
    return AuditEvent(**{**d, "as_of": datetime.fromisoformat(d["as_of"]),
                         "reference_price": Decimal(ref) if ref is not None else None})


def iter_export(records: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Flat rows for CSV/JSON export."""
    for r in records:
        yield {"seq": r["seq"], "price_id": r["price_id"], "as_of": r["as_of"].astimezone(timezone.utc).isoformat(),
               "entity": r["entity"], "pack_version": r["pack_version"], "price": r["output"].get("price"),
               "currency": r["output"].get("currency"), "base_price": r["inputs"].get("base_price"),
               "signals": json.dumps(r["inputs"].get("signals"), sort_keys=True), "key_id": r["key_id"],
               "hash": bytes(r["hash"]).hex()}
