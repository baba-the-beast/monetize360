"""App factories: create_pricing_app() (hot path routes) and create_control_app() (admin routes). Same code, two
processes.

Two ways to run:
  DATABASE_URL set  -> real mode: API keys from Postgres decide the tenant; live packs are read from Postgres.
  DEV_TENANT set    -> local development without a database: every caller is that tenant, packs come from
                       PACKS_DIR. Ignored when DATABASE_URL is set.
  neither           -> closed: every /v1 call is 401."""
from __future__ import annotations

import logging
import re
import threading
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from fastapi import FastAPI

from monetize360.api import errors
from monetize360.api.routes import admin as admin_routes
from monetize360.api.routes import audit as audit_routes
from monetize360.api.routes import health, price
from monetize360.api.routes import packs as pack_routes
from monetize360.config import settings
from monetize360.security.keys import KeyVerifier
from monetize360.security.ratelimit import RateLimiter
from monetize360.store import api_keys, packs
from monetize360.store.audit import AuditLog
from monetize360.store.cache import Events
from monetize360.store.controls import Controls
from monetize360.store.db import Database
from monetize360.store.packcache import PackCache

log = logging.getLogger("monetize360.api")


_KEY_ID = re.compile(r"[A-Z2-9]{10}")


def _base_app(title: str, dev_tenant: str | None, packs_dir: Path | None, database_url: str | None,
              audit_spool_dir: Path | None = None, redis_url: str | None = None,
              pack_poll_seconds: float | None = None) -> FastAPI:
    cfg = settings()
    # an explicit argument wins over the environment (tests and scripts pass arguments)
    if database_url is None and dev_tenant is None:
        database_url, dev_tenant = cfg.database_url, cfg.dev_tenant
        redis_url = cfg.redis_url if redis_url is None else redis_url

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        app.state.stopping.set()
        if app.state.events is not None:
            app.state.events.stop()
        if app.state.audit is not None:
            app.state.audit.stop()
        if app.state.db is not None:
            app.state.db.close()

    app = FastAPI(title=title, version="0.1.0", lifespan=lifespan)
    app.state.packs = PackCache()
    app.state.limiter = RateLimiter()
    app.state.db = app.state.keys = app.state.dev_tenant = app.state.audit = app.state.events = None
    app.state.stopping = threading.Event()
    app.state.controls = Controls()        # limits + kill switches; empty without a database
    app.state.refresh_packs = lambda: 0
    app.state.refresh_controls = lambda: None

    if database_url:
        if dev_tenant:
            log.warning("DEV_TENANT is ignored because DATABASE_URL is set")
        app.state.db = Database(database_url)
        app.state.db.open()
        app.state.keys = KeyVerifier(partial(api_keys.lookup, app.state.db), ttl=cfg.key_cache_ttl_seconds)
        # every price is appended to the audit chain in Postgres; a local spool covers database outages
        app.state.audit = AuditLog(app.state.db, audit_spool_dir or cfg.audit_spool_dir)
        app.state.audit.start_draining()
        db, cache = app.state.db, app.state.packs

        def refresh_packs() -> int:
            """Load whatever is live in Postgres that this replica doesn't have yet."""
            return cache.sync_rows(packs.live_packs(db))
        app.state.refresh_packs = refresh_packs

        def refresh_controls() -> None:
            app.state.controls.refresh(db)
        app.state.refresh_controls = refresh_controls

        if redis_url:
            # broadcasts only (store/cache.py): a revoked key is dropped by every replica at once
            keys, events = app.state.keys, Events(redis_url, app.state.db.fingerprint)
            events.on("key_revoked", lambda m: keys.evict(m["key_id"])
                      if isinstance(m.get("key_id"), str) and _KEY_ID.fullmatch(m["key_id"]) else None)
            events.on_connect(keys.clear)          # anything revoked while we weren't listening
            # a publish or rollback somewhere: the message carries no pack, it only says "look again"
            events.on("pack_published", lambda m: refresh_packs())
            events.on_connect(refresh_packs)
            # a tenant's limits or a kill switch changed somewhere: same rule, re-read Postgres
            events.on("controls_changed", lambda m: refresh_controls())
            events.on_connect(refresh_controls)
            events.start()
            app.state.events = events
        refresh_controls()
        loaded = refresh_packs()
        log.info("API keys ON, %d live pack(s) loaded from the database", loaded)

        def poll() -> None:
            # backstop for a lost broadcast (or no Redis at all): look at Postgres every few seconds
            while not app.state.stopping.wait(pack_poll_seconds or cfg.pack_poll_seconds):
                try:
                    refresh_packs()
                    refresh_controls()
                except Exception:      # noqa: BLE001 - database down: keep serving what we have, try again
                    log.warning("pack poll failed; keeping the packs already loaded")
        threading.Thread(target=poll, name="pack-poll", daemon=True).start()
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
                       database_url: str | None = None, audit_spool_dir: Path | None = None,
                       redis_url: str | None = None, pack_poll_seconds: float | None = None) -> FastAPI:
    """Decision plane: /v1/price, /v1/price/.../batch, /v1/simulate. Stateless; scale by adding replicas."""
    app = _base_app("Monetize360 pricing API", dev_tenant, packs_dir, database_url, audit_spool_dir, redis_url,
                    pack_poll_seconds)
    app.include_router(price.router)
    return app


def create_control_app(*, dev_tenant: str | None = None, packs_dir: Path | None = None,
                       database_url: str | None = None, audit_spool_dir: Path | None = None,
                       redis_url: str | None = None, pack_poll_seconds: float | None = None) -> FastAPI:
    """Control plane: packs (draft -> lint -> verify -> approve -> publish -> rollback), audit, replay, API keys."""
    app = _base_app("Monetize360 control API", dev_tenant, packs_dir, database_url, audit_spool_dir, redis_url,
                    pack_poll_seconds)
    app.include_router(pack_routes.router)
    app.include_router(audit_routes.router)
    app.include_router(admin_routes.router)
    return app


def create_app(*, dev_tenant: str | None = None, packs_dir: Path | None = None,
               database_url: str | None = None, audit_spool_dir: Path | None = None,
               redis_url: str | None = None, pack_poll_seconds: float | None = None) -> FastAPI:
    """Both planes in one process: local development and demos without nginx (one Swagger for everything)."""
    app = _base_app("Monetize360 API", dev_tenant, packs_dir, database_url, audit_spool_dir, redis_url,
                    pack_poll_seconds)
    app.include_router(price.router)
    app.include_router(pack_routes.router)
    app.include_router(audit_routes.router)
    app.include_router(admin_routes.router)
    return app
