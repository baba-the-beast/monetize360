"""Create a Postgres database for Monetize360 WITHOUT Docker, from the same files Docker uses
(deploy/postgres/init/*.sql). Reads credentials from .env.local (git-ignored).

    python scripts/setup_local_db.py                 # create the dev database if it doesn't exist
    python scripts/setup_local_db.py --reset         # drop and recreate it
    python scripts/setup_local_db.py --db monetize360_test --reset     # what the integration tests do

Docker Compose does the same through the image's init scripts; this is only for a machine with a native Postgres."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
INIT_DIR = ROOT / "deploy/postgres/init"
APP_ROLE = "m360_app"


def read_env(path: Path = ROOT / ".env.local") -> dict[str, str]:
    if not path.exists():
        sys.exit(f"{path} not found: it holds the local Postgres passwords (see README)")
    pairs = (line.split("=", 1) for line in path.read_text(encoding="utf-8-sig").splitlines()
             if "=" in line and not line.lstrip().startswith("#"))
    return {k.strip(): v.strip() for k, v in pairs}


def _connect(env: dict[str, str], *, user: str, password: str, dbname: str) -> psycopg.Connection:
    return psycopg.connect(host=env.get("PGHOST", "127.0.0.1"), port=env.get("PGPORT", "5432"), user=user,
                           password=password, dbname=dbname, autocommit=True)


def _role(conn: psycopg.Connection, name: str, password: str) -> None:
    """Create the login role, or reset its password so it always matches .env.local."""
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone()
    verb = sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}") if exists else sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}")
    conn.execute(verb.format(sql.Identifier(name), sql.Literal(password)))


def setup(dbname: str, *, reset: bool = False, env: dict[str, str] | None = None) -> bool:
    """Returns True when the schema was created, False when the database already had it."""
    env = env or read_env()
    owner = env["POSTGRES_USER"]
    with _connect(env, user=env["PG_SUPERUSER"], password=env["PG_SUPERUSER_PASSWORD"], dbname="postgres") as su:
        _role(su, owner, env["POSTGRES_PASSWORD"])
        _role(su, APP_ROLE, env["APP_DB_PASSWORD"])           # what 001_roles.sh does in Docker
        if reset:
            su.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))
        if not su.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone():
            su.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(dbname), sql.Identifier(owner)))

    with _connect(env, user=owner, password=env["POSTGRES_PASSWORD"], dbname=dbname) as conn:
        if conn.execute("SELECT to_regclass('public.tenants')").fetchone()[0] is not None:
            return False
        for path in sorted(INIT_DIR.glob("*.sql")):
            conn.execute(path.read_text(encoding="utf-8"))    # no parameters: the whole file runs as one script
    return True


def urls(dbname: str, env: dict[str, str] | None = None) -> dict[str, str]:
    """Connection URLs for the app role (subject to RLS) and the owner (seed scripts only)."""
    env = env or read_env()
    host, port = env.get("PGHOST", "127.0.0.1"), env.get("PGPORT", "5432")
    return {
        "app": f"postgresql://{APP_ROLE}:{env['APP_DB_PASSWORD']}@{host}:{port}/{dbname}",
        "owner": f"postgresql://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}@{host}:{port}/{dbname}",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help="database name (default: POSTGRES_DB from .env.local)")
    parser.add_argument("--reset", action="store_true", help="drop the database first")
    args = parser.parse_args()
    settings = read_env()
    name = args.db or settings["POSTGRES_DB"]
    created = setup(name, reset=args.reset, env=settings)
    print(f"database '{name}': " + ("schema created" if created else "already set up, nothing changed"))
