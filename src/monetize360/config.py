"""Settings read from environment variables (DATABASE_URL, REDIS_URL, PACK_SCHEMA_PATH, ...). No other module
reads os.environ."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    redis_url: str | None
    log_level: str
    key_cache_ttl_seconds: int
    pack_poll_seconds: int
    pack_schema_path: Path
    packs_dir: Path
    audit_spool_dir: Path
    measured_path: Path
    dev_tenant: str | None


@lru_cache(maxsize=1)
def settings() -> Settings:
    env = os.environ
    return Settings(
        database_url=env.get("DATABASE_URL"),
        redis_url=env.get("REDIS_URL"),
        log_level=env.get("LOG_LEVEL", "info"),
        key_cache_ttl_seconds=int(env.get("KEY_CACHE_TTL_SECONDS", "60")),
        pack_poll_seconds=int(env.get("PACK_POLL_SECONDS", "10")),
        # default works for a dev checkout; Docker sets PACK_SCHEMA_PATH (.env.example)
        pack_schema_path=Path(env.get("PACK_SCHEMA_PATH", REPO_ROOT / "packs/schema/pack.schema.json")),
        packs_dir=Path(env.get("PACKS_DIR", REPO_ROOT / "packs/templates")),
        # where audit records wait when Postgres is unreachable (one file per process)
        audit_spool_dir=Path(env.get("AUDIT_SPOOL_DIR", REPO_ROOT / ".audit-spool")),
        # results of test and load runs, shown by /v1/scoreboard under "measured" (bench/RESULTS.md)
        measured_path=Path(env.get("MEASURED_PATH", REPO_ROOT / "bench/measured.json")),
        # Until API keys exist (M4) the API is CLOSED unless a dev tenant is named here: every caller is then that
        # tenant and its packs come from PACKS_DIR. Never set in production.
        dev_tenant=env.get("DEV_TENANT") or None,
    )
