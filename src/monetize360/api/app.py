"""App factories: create_pricing_app() (hot path routes) and create_control_app() (admin routes). Same code, two
processes.

Two ways to run:
  DATABASE_URL set  -> real mode: API keys from Postgres decide the tenant; live packs are read from Postgres.
  DEV_TENANT set    -> local development without a database: every caller is that tenant, packs come from
                       PACKS_DIR. Ignored when DATABASE_URL is set.
  neither           -> closed: every /v1 call is 401."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from fastapi import FastAPI

from monetize360.api import errors
from monetize360.api.routes import health, price
from monetize360.config import settings
from monetize360.security.keys import KeyVerifier
from monetize360.security.ratelimit import RateLimiter
from monetize360.store import api_keys, packs
from monetize360.store.db import Database
from monetize360.store.packcache import PackCache

log = logging.getLogger("monetize360.api")


def _base_app(title: str, dev_tenant: str | None, packs_dir: Path | None, database_url: str | None) -> FastAPI:
    cfg = settings()
    # an explicit argument wins over the environment (tests and scripts pass arguments)
    if database_url is None and dev_tenant is None:
        database_url, dev_tenant = cfg.database_url, cfg.dev_tenant

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if app.state.db is not None:
            app.state.db.close()

    app = FastAPI(title=title, version="0.1.0", lifespan=lifespan)
    app.state.packs = PackCache()
    app.state.limiter = RateLimiter()
    app.state.db = app.state.keys = app.state.dev_tenant = None

    if database_url:
        if dev_tenant:
            log.warning("DEV_TENANT is ignored because DATABASE_URL is set")
        app.state.db = Database(database_url)
        app.state.db.open()
        app.state.keys = KeyVerifier(partial(api_keys.lookup, app.state.db), ttl=cfg.key_cache_ttl_seconds)
        loaded = app.state.packs.load_rows(packs.live_packs(app.state.db))
        log.info("API keys ON, %d live pack(s) loaded from the database", loaded)
    elif dev_tenant:
        app.state.dev_tenant = dev_tenant
        loaded = app.state.packs.load_directory(packs_dir or cfg.packs_dir, dev_tenant)
        log.warning("DEV_TENANT=%s: API keys are OFF, %d pack(s) loaded from files", dev_tenant, loaded)
    for (_, name), why in app.state.packs.load_errors.items():
        log.error("pack %s not loaded: %s", name, why)

    errors.install(app)
    app.include_router(health.router)
    return app


def create_pricing_app(*, dev_tenant: str | None = None, packs_dir: Path | None = None,
                       database_url: str | None = None) -> FastAPI:
    """Decision plane: /v1/price, /v1/price/.../batch, /v1/simulate. Stateless; scale by adding replicas."""
    app = _base_app("Monetize360 pricing API", dev_tenant, packs_dir, database_url)
    app.include_router(price.router)
    return app


def create_control_app(*, dev_tenant: str | None = None, packs_dir: Path | None = None,
                       database_url: str | None = None) -> FastAPI:
    """Control plane: packs, audit, replay, admin. Those routes arrive with M5-M7; until then it serves the ops
    endpoints so the process starts and reports health."""
    return _base_app("Monetize360 control API", dev_tenant, packs_dir, database_url)
