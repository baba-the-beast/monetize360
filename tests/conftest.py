"""Shared fixtures: loaded template packs, test tenant/keys, API test client."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from monetize360.engine import Context, compile_pack

ROOT = Path(__file__).resolve().parents[1]
WEDNESDAY = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)   # no festival


def load_pack(name: str) -> dict:
    return yaml.safe_load((ROOT / "packs/templates" / name).read_text(encoding="utf-8"))


def make_pack(groups: list, signals: dict | None = None, **overrides) -> dict:
    """A small valid pack for unit tests: base from request, wide guards, no rounding effect."""
    pack = {
        "domain": "test", "currency": "INR",
        "signals": signals or {"x": {"type": "number", "source": "request", "default": 0}},
        "base": {"op": "from_request"},
        "groups": groups,
        "guards": {"floor": {"op": "abs", "value": 1}, "ceiling": {"op": "abs", "value": 1000000}},
        "rounding": {"step": 0.01, "mode": "half_up"},
    }
    pack.update(overrides)
    return pack


def ctx(base="1000", as_of=WEDNESDAY, reference=None, override=True, **signals) -> Context:
    return Context(base_price=Decimal(base) if base is not None else None, as_of=as_of, signals=signals,
                   reference_price=Decimal(reference) if reference is not None else None,
                   allow_public_override=override)


@pytest.fixture(scope="session")
def hotel():
    return compile_pack(load_pack("hotel.yaml"))


@pytest.fixture(scope="session")
def airline():
    return compile_pack(load_pack("airline.yaml"))
