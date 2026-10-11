"""psycopg3 connection pool; sets app.tenant_id per transaction for Row-Level Security.

The app connects as `m360_app`, which owns nothing: every table is behind an RLS policy keyed on the
`app.tenant_id` setting. `tenant()` opens a transaction with that setting, so a query can only ever see one
tenant's rows; `service()` opens one WITHOUT it, where the tables look empty and only the two narrow
SECURITY DEFINER functions (lookup_api_key, list_live_packs) return anything."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class Database:
    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 10):
        self._pool = ConnectionPool(url, min_size=min_size, max_size=max_size, open=False,
                                    kwargs={"row_factory": dict_row})

    def open(self) -> None:
        self._pool.open(wait=True, timeout=10)

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def tenant(self, tenant_id: str) -> Iterator[psycopg.Connection]:
        """A transaction that can only see `tenant_id`'s rows. The setting ends with the transaction."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            yield conn

    @contextmanager
    def service(self) -> Iterator[psycopg.Connection]:
        """A transaction with no tenant: for the SECURITY DEFINER functions only."""
        with self._pool.connection() as conn, conn.transaction():
            yield conn

    def ping(self) -> bool:
        try:
            with self.service() as conn:
                return conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
        except psycopg.Error:
            return False
