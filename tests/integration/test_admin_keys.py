"""API-key endpoints against a real Postgres, and (when REDIS_URL is in .env.local) the revocation broadcast
between two servers through a real Redis."""
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from monetize360.api.app import create_app
from monetize360.security import keys as keylib

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
HOTEL = {"entity": "admin-keys-room", "base_price": "3000", "signals": {"occupancy": 0.5},
         "as_of": "2026-10-14T09:00:00Z"}


def bearer(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _code(r):
    return r.status_code, r.json().get("error")


@pytest.fixture
def new_key(client, auth):
    def create(tenant="hotel_001", **body) -> dict:
        r = client.post("/v1/admin/keys", json={"role": "pricing", "domains": ["hotel"], **body},
                        headers=auth(tenant, "admin"))
        assert r.status_code == 201, r.text
        return r.json()
    return create


# ---------------------------------------------------------------- create

def test_a_created_key_works_at_once_and_its_secret_is_shown_exactly_once(client, auth, new_key, database):
    made = new_key(name="checkout server", rate_limit_rps=50)
    assert keylib.parse(made["api_key"])[0] == made["key_id"] and made["api_key"].startswith("m360_live_")
    assert (made["role"], made["domains"], made["name"], made["rate_limit_rps"]) == \
        ("pricing", ["hotel"], "checkout server", 50)
    assert client.post("/v1/price/hotel", json=HOTEL, headers=bearer(made["api_key"])).status_code == 200

    listing = client.get("/v1/admin/keys", headers=auth("hotel_001", "admin"))
    mine = next(k for k in listing.json()["keys"] if k["key_id"] == made["key_id"])
    assert mine["status"] == "active" and mine["name"] == "checkout server" and len(mine["created_by"]) == 10
    secret = keylib.parse(made["api_key"])[1]
    assert secret not in listing.text and "secret" not in listing.text and "sha256" not in listing.text
    with psycopg.connect(database.owner_url) as conn:
        stored = conn.execute("SELECT secret_sha256 FROM api_keys WHERE key_id = %s", (made["key_id"],)).fetchone()[0]
    assert bytes(stored) == keylib.hash_secret(secret)                       # only the hash is in the database


def test_the_response_carrying_the_secret_must_not_be_cached(client, auth):
    r = client.post("/v1/admin/keys", json={"role": "pricing", "domains": ["hotel"]},
                    headers=auth("hotel_001", "admin"))
    assert r.headers["Cache-Control"] == "no-store"


def test_only_an_admin_key_can_manage_keys(client, auth):
    body = {"role": "admin", "domains": ["*"]}
    for label in ("pricing", "editor", "approver"):
        assert _code(client.post("/v1/admin/keys", json=body, headers=auth("hotel_001", label))) == (403, "forbidden")
        assert client.get("/v1/admin/keys", headers=auth("hotel_001", label)).status_code == 403
        assert client.post("/v1/admin/keys/ABCDEFGHJK/revoke", headers=auth("hotel_001", label)).status_code == 403
    assert client.post("/v1/admin/keys", json=body).status_code == 401


def test_a_key_cannot_create_a_key_with_more_scope_than_it_has(client, new_key):
    limited = new_key(role="admin", domains=["hotel"], name="hotel-only admin")
    h = bearer(limited["api_key"])
    for domains in (["*"], ["hotel", "airline"], ["ride"]):
        r = client.post("/v1/admin/keys", json={"role": "pricing", "domains": domains}, headers=h)
        assert _code(r) == (403, "forbidden"), domains
    assert client.post("/v1/admin/keys", json={"role": "pricing", "domains": ["hotel"]}, headers=h).status_code == 201


@pytest.mark.parametrize("body", [
    {"role": "superuser", "domains": ["hotel"]},
    {"role": "pricing", "domains": []},
    {"role": "pricing", "domains": ["Hotel"]},
    {"role": "pricing", "domains": ["hotel", "hotel"]},
    {"role": "pricing", "domains": ["hotel; DROP TABLE api_keys"]},
    {"role": "pricing", "domains": ["hotel"], "rate_limit_rps": 0},
    {"role": "pricing", "domains": ["hotel"], "expires_at": "2020-01-01T00:00:00Z"},
    {"role": "pricing", "domains": ["hotel"], "expires_at": "2030-01-01T00:00:00"},
    {"role": "pricing", "domains": ["hotel"], "tenant_id": "airline_002"},
    {"role": "pricing", "domains": ["hotel"], "name": ""},
    {"role": "pricing", "domains": ["hotel"], "env": "prod"},
])
def test_bad_key_requests_are_rejected(client, auth, body):
    r = client.post("/v1/admin/keys", json=body, headers=auth("hotel_001", "admin"))
    assert _code(r) == (400, "bad_request"), body


def test_a_key_with_an_expiry_stops_working_when_it_passes(client, new_key, database):
    made = new_key(expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    h = bearer(made["api_key"])
    assert client.post("/v1/price/hotel", json=HOTEL, headers=h).status_code == 200
    with psycopg.connect(database.owner_url) as conn:
        conn.execute("UPDATE api_keys SET expires_at = now() - interval '1 second' WHERE key_id = %s",
                     (made["key_id"],))
    client.app.state.keys.evict(made["key_id"])
    assert _code(client.post("/v1/price/hotel", json=HOTEL, headers=h)) == (401, "unauthorized")


# ---------------------------------------------------------------- only your own tenant

def test_keys_are_created_listed_and_revoked_for_the_callers_tenant_only(client, auth, new_key):
    theirs = new_key(tenant="airline_002", domains=["airline"], name="airline key")
    mine = client.get("/v1/admin/keys", headers=auth("hotel_001", "admin")).json()["keys"]
    assert theirs["key_id"] not in {k["key_id"] for k in mine}

    r = client.post(f"/v1/admin/keys/{theirs['key_id']}/revoke", headers=auth("hotel_001", "admin"))
    unknown = client.post("/v1/admin/keys/ZZZZZZZZZZ/revoke", headers=auth("hotel_001", "admin"))
    assert _code(r) == _code(unknown) == (404, "key_not_found")              # not yours looks like not there
    assert client.post("/v1/admin/keys/not-a-key-id/revoke", headers=auth("hotel_001", "admin")).status_code == 404
    airline = {"entity": "6E", "as_of": "2026-10-14T09:00:00Z",
               "signals": {"seats_sold": "0.65", "days_to_departure": 3, "cabin": "business"}}
    assert client.post("/v1/price/airline", json=airline, headers=bearer(theirs["api_key"])).status_code == 200


# ---------------------------------------------------------------- revoke

def test_revoking_a_key_stops_it_at_once_on_this_server(client, auth, new_key):
    made = new_key()
    h = bearer(made["api_key"])
    assert client.post("/v1/price/hotel", json=HOTEL, headers=h).status_code == 200      # now cached

    r = client.post(f"/v1/admin/keys/{made['key_id']}/revoke", headers=auth("hotel_001", "admin"))
    assert r.status_code == 200 and r.json()["key_id"] == made["key_id"] and r.json()["revoked_at"]
    assert _code(client.post("/v1/price/hotel", json=HOTEL, headers=h)) == (401, "unauthorized")

    listed = next(k for k in client.get("/v1/admin/keys", headers=auth("hotel_001", "admin")).json()["keys"]
                  if k["key_id"] == made["key_id"])
    assert listed["status"] == "revoked" and len(listed["revoked_by"]) == 10
    again = client.post(f"/v1/admin/keys/{made['key_id']}/revoke", headers=auth("hotel_001", "admin"))
    assert again.status_code == 200 and again.json()["revoked_at"] == r.json()["revoked_at"]     # same answer


def test_you_cannot_revoke_your_only_working_admin_key(client, new_key, database):
    with psycopg.connect(database.owner_url) as conn:           # a tenant of its own, with one admin key
        conn.execute("INSERT INTO tenants (id, name) VALUES ('solo_009', 'Solo') ON CONFLICT DO NOTHING")
        key_id, api_key, secret_hash = keylib.generate("test")
        conn.execute("DELETE FROM api_keys WHERE tenant_id = 'solo_009'")
        conn.execute("INSERT INTO api_keys (key_id, tenant_id, secret_sha256, role, domains) "
                     "VALUES (%s, 'solo_009', %s, 'admin', '{*}')", (key_id, secret_hash))
    h = bearer(api_key)
    assert _code(client.post(f"/v1/admin/keys/{key_id}/revoke", headers=h)) == (409, "last_admin_key")
    assert client.get("/v1/admin/keys", headers=h).status_code == 200        # still works

    second = client.post("/v1/admin/keys", json={"role": "admin", "domains": ["*"]}, headers=h).json()
    assert client.post(f"/v1/admin/keys/{key_id}/revoke", headers=bearer(second["api_key"])).status_code == 200
    assert client.get("/v1/admin/keys", headers=h).status_code == 401        # the first one is gone
    assert _code(client.post(f"/v1/admin/keys/{second['key_id']}/revoke", headers=bearer(second["api_key"]))) == \
        (409, "last_admin_key")


# ---------------------------------------------------------------- broadcast through a real Redis

def _redis_url() -> str | None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import setup_local_db
    return setup_local_db.read_env().get("REDIS_URL") if (ROOT / ".env.local").exists() else None


def _wait(condition, seconds: float) -> float | None:
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        if condition():
            return time.monotonic() - start
        time.sleep(0.05)
    return None


def test_a_key_revoked_on_one_server_is_refused_by_another_within_seconds(database, auth, tmp_path):
    url = _redis_url()
    if not url:
        pytest.skip("no REDIS_URL in .env.local")
    a = create_app(database_url=database.app_url, audit_spool_dir=tmp_path / "a", redis_url=url)
    b = create_app(database_url=database.app_url, audit_spool_dir=tmp_path / "b", redis_url=url)
    quiet = create_app(database_url=database.app_url, audit_spool_dir=tmp_path / "c")      # a server with no Redis
    with TestClient(a) as ca, TestClient(b) as cb, TestClient(quiet) as cq:
        if _wait(lambda: a.state.events.connected and b.state.events.connected, 15) is None:
            pytest.skip("Redis is not reachable from here")
        assert ca.get("/readyz").json()["redis"] == "ok" and cq.get("/readyz").json()["redis"] == "not_configured"

        made = ca.post("/v1/admin/keys", json={"role": "pricing", "domains": ["hotel"]},
                       headers=auth("hotel_001", "admin")).json()
        h = bearer(made["api_key"])
        for c in (ca, cb, cq):                                    # every server has now cached the key
            assert c.post("/v1/price/hotel", json=HOTEL, headers=h).status_code == 200

        r = ca.post(f"/v1/admin/keys/{made['key_id']}/revoke", headers=auth("hotel_001", "admin"))
        assert r.status_code == 200 and r.json()["broadcast"] is True
        assert ca.post("/v1/price/hotel", json=HOTEL, headers=h).status_code == 401       # the server that did it

        took = _wait(lambda: cb.post("/v1/price/hotel", json=HOTEL, headers=h).status_code == 401, 10)
        assert took is not None, "server B never heard about the revocation"
        print(f"\n   server B refused the revoked key {took:.2f} s after server A revoked it")

        # without Redis a server only finds out when its cached copy expires (60 s): that is the backstop
        assert cq.post("/v1/price/hotel", json=HOTEL, headers=h).status_code == 200
        quiet.state.keys.clear()
        assert cq.post("/v1/price/hotel", json=HOTEL, headers=h).status_code == 401
