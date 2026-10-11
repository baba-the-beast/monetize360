"""Integration fixtures: a REAL Postgres. Each test session drops and recreates `monetize360_test` from
deploy/postgres/init/*.sql, seeds the demo tenants and starts the pricing app against it.

Needs a local Postgres and .env.local (scripts/setup_local_db.py). Run with:  pytest -m integration"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

TEST_DB = "monetize360_test"


@pytest.fixture(scope="session")
def database():
    if not (ROOT / ".env.local").exists():
        pytest.skip("no .env.local: integration tests need a local Postgres (scripts/setup_local_db.py)")
    import seed_demo
    import setup_local_db

    env = setup_local_db.read_env()
    setup_local_db.setup(TEST_DB, reset=True, env=env)
    urls = setup_local_db.urls(TEST_DB, env)
    return SimpleNamespace(app_url=urls["app"], owner_url=urls["owner"], keys=seed_demo.seed(urls["owner"]))


@pytest.fixture(scope="session")
def app(database, tmp_path_factory):
    from monetize360.api.app import create_app
    return create_app(database_url=database.app_url, audit_spool_dir=tmp_path_factory.mktemp("spool"))


@pytest.fixture(scope="session")
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def auth(database):
    """auth("hotel_001", "pricing") -> headers for that seeded key."""
    def headers(tenant: str, label: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {database.keys[tenant][label]}"}
    return headers
