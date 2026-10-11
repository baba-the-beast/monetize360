"""GET /v1/scoreboard: measured KPIs as JSON.

Three kinds of number, kept apart so nothing reads as more than it is:
  capabilities  counted from the pack schema this server ships with (not typed in by hand)
  live          what THIS replica has done since it started (its own counters; other replicas have their own)
  measured      results of test and load runs, read from a file written after those runs (bench/measured.json);
                absent = "not measured", never a made-up value
No key needed, so it shows no tenant, domain name or price."""
from __future__ import annotations

import json
import time
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Request
from prometheus_client import REGISTRY

from monetize360.api.routes.health import self_test_ok
from monetize360.config import settings

router = APIRouter(tags=["ops"])
_STARTED = time.time()


@lru_cache(maxsize=1)
def _capabilities() -> dict[str, Any]:
    defs = json.loads(settings().pack_schema_path.read_text(encoding="utf-8"))["$defs"]
    base = defs["base"]["properties"]["op"]["enum"]
    adjust = defs["adjustment"]["properties"]["op"]["enum"]
    return {
        "operators": sorted(set(base) | set(adjust)),
        "operator_count": len(set(base) | set(adjust)),
        "stacking_policies": defs["group"]["properties"]["stacking"]["enum"],
        "guard_layers": ["pack", "tenant", "platform"],
        "limit_kinds": defs["limit"]["properties"]["op"]["enum"],
        "signal_types": defs["signal"]["properties"]["type"]["enum"],
        "signal_sources": defs["signal"]["properties"]["source"]["enum"],
    }


def _counters() -> dict[str, dict[tuple, float]]:
    """{sample name: {label values: value}} for this process's m360_* metrics."""
    out: dict[str, dict[tuple, float]] = {}
    for family in REGISTRY.collect():
        if family.name.startswith("m360_"):
            for s in family.samples:
                out.setdefault(s.name, {})[tuple(sorted(s.labels.items()))] = s.value
    return out


def _quantile_upper_bound(buckets: dict[tuple, float], kind: str, q: float) -> float | None:
    """The smallest histogram bucket that holds at least q of the observations: "q of requests took <= this"."""
    mine = sorted((float(dict(labels)["le"]), n) for labels, n in buckets.items()
                  if dict(labels).get("kind") == kind and dict(labels)["le"] != "+Inf")
    total = next((n for labels, n in buckets.items()
                  if dict(labels).get("kind") == kind and dict(labels)["le"] == "+Inf"), 0)
    if not total:
        return None
    return next((le * 1000 for le, n in mine if n >= q * total), None)


def _measured() -> dict[str, Any] | None:
    path = settings().measured_path
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@router.get("/v1/scoreboard")
def scoreboard(request: Request) -> dict[str, Any]:
    state, c = request.app.state, _counters()
    prices = c.get("m360_prices_total", {})

    def total(kind: str, ok: bool) -> int:
        return int(sum(v for labels, v in prices.items()
                       if dict(labels)["kind"] == kind and (dict(labels)["outcome"] == "ok") == ok))

    audit = {dict(labels)["where"]: int(v) for labels, v in c.get("m360_audit_records_total", {}).items()}
    kills = {dict(labels)["mode"]: int(v) for labels, v in c.get("m360_kill_switch_prices_total", {}).items()}
    buckets = c.get("m360_price_seconds_bucket", {})
    live_packs = len(state.packs)
    return {
        "capabilities": _capabilities(),
        "live": {
            "scope": "this replica, since it started",
            "uptime_seconds": int(time.time() - _STARTED),
            "live_packs": live_packs,
            "self_test": self_test_ok(),
            "database": "not_configured" if state.db is None else ("ok" if state.db.ping() else "down"),
            "redis": "not_configured" if state.events is None else ("ok" if state.events.connected else "down"),
            "prices": {"ok": total("price", True), "rejected": total("price", False)},
            "simulations": {"ok": total("simulate", True), "rejected": total("simulate", False)},
            "audit_records": {"database": audit.get("database", 0), "spooled": audit.get("spool", 0)},
            "idempotent_replays": int(sum(c.get("m360_idempotent_replays_total", {}).values())),
            "kill_switch_prices": kills,
            "engine_ms_at_most": {"note": "evaluate + explain only, from histogram buckets (upper bounds)",
                                  "p50": _quantile_upper_bound(buckets, "price", 0.50),
                                  "p95": _quantile_upper_bound(buckets, "price", 0.95),
                                  "p99": _quantile_upper_bound(buckets, "price", 0.99)},
        },
        "measured": _measured(),
    }
