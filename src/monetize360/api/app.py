"""App factories: create_pricing_app() (hot path routes) and create_control_app() (admin routes). Same code, two
processes."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI

from monetize360.api import errors
from monetize360.api.routes import health, price
from monetize360.config import settings
from monetize360.store.packcache import PackCache

log = logging.getLogger("monetize360.api")


def _base_app(title: str, dev_tenant: str | None, packs_dir: Path | None) -> FastAPI:
    cfg = settings()
    app = FastAPI(title=title, version="0.1.0")
    app.state.dev_tenant = dev_tenant if dev_tenant is not None else cfg.dev_tenant
    app.state.packs = PackCache()
    if app.state.dev_tenant:
        # M3: the dev tenant's packs are the YAML files in PACKS_DIR. M5 loads every tenant's from Postgres.
        loaded = app.state.packs.load_directory(packs_dir or cfg.packs_dir, app.state.dev_tenant)
        log.warning("DEV_TENANT=%s: API keys are OFF, %d pack(s) loaded from files", app.state.dev_tenant, loaded)
        for (_, name), why in app.state.packs.load_errors.items():
            log.error("pack %s not loaded: %s", name, why)
    errors.install(app)
    app.include_router(health.router)
    return app


def create_pricing_app(*, dev_tenant: str | None = None, packs_dir: Path | None = None) -> FastAPI:
    """Decision plane: /v1/price, /v1/price/.../batch, /v1/simulate. Stateless; scale by adding replicas."""
    app = _base_app("Monetize360 pricing API", dev_tenant, packs_dir)
    app.include_router(price.router)
    return app


def create_control_app(*, dev_tenant: str | None = None, packs_dir: Path | None = None) -> FastAPI:
    """Control plane: packs, audit, replay, admin. Those routes arrive with M5-M7; until then it serves the ops
    endpoints so the process starts and reports health."""
    return _base_app("Monetize360 control API", dev_tenant, packs_dir)
