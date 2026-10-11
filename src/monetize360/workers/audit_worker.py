"""Append spooled audit records to the hash chain: `python -m monetize360.workers.audit_worker`.

Audit rows are written to Postgres by the request itself (store/audit.py, ARCHITECTURE 11.1). Only when
Postgres was unreachable are they spooled to AUDIT_SPOOL_DIR; every API process drains its own spool in a
background thread. This standalone loop does the same for a spool directory whose API process is gone
(a crashed or scaled-in replica), so it needs that directory mounted."""
from __future__ import annotations

import logging
import time

from monetize360.config import settings
from monetize360.store.audit import AuditLog
from monetize360.store.db import Database

log = logging.getLogger("monetize360.audit")


def main(interval: float = 5.0) -> None:
    cfg = settings()
    if not cfg.database_url:
        raise SystemExit("DATABASE_URL is not set")
    logging.basicConfig(level=cfg.log_level.upper())
    db = Database(cfg.database_url)
    db.open()
    audit = AuditLog(db, cfg.audit_spool_dir)
    log.info("draining %s every %.0f s", cfg.audit_spool_dir, interval)
    while True:
        audit.drain()
        time.sleep(interval)


if __name__ == "__main__":
    main()
