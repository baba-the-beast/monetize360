"""Tenant A key can never load, price or audit tenant B packs (always 404).

M4 against a real Postgres: API keys decide the tenant, keys are scoped to domains, revoked and expired keys
stop working, and the database itself (Row-Level Security + grants) refuses cross-tenant access even if the
application code were wrong."""
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from monetize360.api.app import create_pricing_app
from monetize360.security import keys as keylib
from monetize360.store import api_keys

pytestmark = pytest.mark.integration

HOTEL = {"entity": "deluxe-101@2026-11-08", "base_price": "3000", "signals": {"occupancy": 0.85},
         "as_of": "2026-11-08T09:00:00Z"}
AIRLINE = {"entity": "6E-201", "as_of": "2026-10-14T09:00:00Z",
           "signals": {"seats_sold": "0.65", "days_to_departure": 3, "cabin": "business"}}
RIDE = {"entity": "trip-1", "as_of": "2026-10-14T08:30:00Z",
        "signals": {"distance_km": 10, "duration_min": 30, "demand_ratio": 2, "vehicle": "sedan"}}


def _code(r):
    return r.status_code, r.json().get("error")


# ---------------------------------------------------------------- authentication

@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer"},
    {"Authorization": "Bearer not-a-key"},
    {"Authorization": "Basic dXNlcjpwYXNz"},
    {"Authorization": "Bearer m360_test_ZZZZZZZZZZ_" + "a" * 43},
    {"X-Api-Key": "m360_test_ZZZZZZZZZZ_" + "a" * 43},
])
def test_no_key_or_a_bad_key_is_401(client, headers):
    assert _code(client.post("/v1/price/hotel", json=HOTEL, headers=headers)) == (401, "unauthorized")


def test_right_key_id_with_a_wrong_secret_is_401(client, database):
    key_id, secret = keylib.parse(database.keys["hotel_001"]["pricing"])
    forged = f"m360_test_{key_id}_{secret[:-1]}{'A' if secret[-1] != 'A' else 'B'}"
    assert _code(client.post("/v1/price/hotel", json=HOTEL, headers={"Authorization": f"Bearer {forged}"})) == \
        (401, "unauthorized")


def test_ops_endpoints_need_no_key_and_reveal_no_tenant(client):
    assert client.get("/healthz").status_code == 200
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"ready": True, "packs_loaded": 5, "pack_load_error_count": 0, "self_test": True,
                            "database": "ok", "redis": "not_configured"}
    assert "hotel_001" not in ready.text


# ---------------------------------------------------------------- the tenant comes from the key

def test_each_tenant_prices_its_own_domain(client, auth):
    r = client.post("/v1/price/hotel", json=HOTEL, headers=auth("hotel_001", "pricing"))
    assert r.status_code == 200 and r.json()["price"] == "4950.00"
    r = client.post("/v1/price/airline", json=AIRLINE, headers=auth("airline_002", "pricing"))
    assert r.status_code == 200 and r.json()["price"] == "25499.00"


def test_a_key_cannot_reach_another_tenants_domain(client, auth):
    assert _code(client.post("/v1/price/airline", json=AIRLINE, headers=auth("hotel_001", "pricing"))) == \
        (404, "pack_not_found")
    assert _code(client.post("/v1/price/hotel", json=HOTEL, headers=auth("airline_002", "pricing"))) == \
        (404, "pack_not_found")
    # not even an admin key, and not through simulate or batch
    assert _code(client.post("/v1/price/airline", json=AIRLINE, headers=auth("hotel_001", "admin"))) == \
        (404, "pack_not_found")
    assert _code(client.post("/v1/simulate/airline", json=AIRLINE, headers=auth("hotel_001", "admin"))) == \
        (404, "pack_not_found")
    assert _code(client.post("/v1/price/airline/batch", json={"items": [AIRLINE]},
                             headers=auth("hotel_001", "admin"))) == (404, "pack_not_found")


def test_not_yours_and_does_not_exist_are_indistinguishable(client, auth):
    theirs = client.post("/v1/price/airline", json=AIRLINE, headers=auth("hotel_001", "pricing")).json()
    nothing = client.post("/v1/price/airlinx", json=AIRLINE, headers=auth("hotel_001", "pricing")).json()
    assert (theirs["error"], theirs["details"]) == (nothing["error"], nothing["details"])
    assert theirs["message"].replace("airline", "X") == nothing["message"].replace("airlinx", "X")


def test_the_body_cannot_name_a_tenant(client, auth):
    r = client.post("/v1/price/hotel", json={**HOTEL, "tenant_id": "airline_002"}, headers=auth("hotel_001", "pricing"))
    assert _code(r) == (400, "bad_request")


def test_a_key_scoped_to_one_domain_gets_404_on_its_own_tenants_other_domains(client, auth):
    scoped = auth("travel_003", "pricing_hotel_only")
    assert client.post("/v1/price/hotel", json=HOTEL, headers=scoped).status_code == 200
    assert _code(client.post("/v1/price/ride", json=RIDE, headers=scoped)) == (404, "pack_not_found")
    r = client.post("/v1/price/ride", json=RIDE, headers=auth("travel_003", "pricing_all"))
    assert r.status_code == 200 and r.json()["price"] == "323.00"


# ---------------------------------------------------------------- revoked, expired, rate limited

def _new_key(app, database, tenant="hotel_001", **extra) -> tuple[str, dict[str, str]]:
    key_id, api_key, secret_hash = keylib.generate("test")
    api_keys.create(app.state.db, tenant, key_id, secret_hash, "pricing", ["hotel"], **extra)
    return key_id, {"Authorization": f"Bearer {api_key}"}


def test_a_revoked_key_stops_working(client, app, database):
    key_id, headers = _new_key(app, database)
    assert client.post("/v1/price/hotel", json=HOTEL, headers=headers).status_code == 200

    assert api_keys.revoke(app.state.db, "airline_002", key_id) is None        # another tenant can't revoke it
    assert client.post("/v1/price/hotel", json=HOTEL, headers=headers).status_code == 200

    assert api_keys.revoke(app.state.db, "hotel_001", key_id) is not None
    app.state.keys.evict(key_id)                                               # what the revoke endpoint will do
    assert _code(client.post("/v1/price/hotel", json=HOTEL, headers=headers)) == (401, "unauthorized")


def test_an_expired_key_is_401(client, app, database):
    _, headers = _new_key(app, database, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    assert _code(client.post("/v1/price/hotel", json=HOTEL, headers=headers)) == (401, "unauthorized")


def test_rate_limit_is_per_key_and_says_when_to_retry(client, app, database, auth):
    _, slow = _new_key(app, database, rate_limit_rps=3)
    codes = [client.post("/v1/price/hotel", json=HOTEL, headers=slow).status_code for _ in range(6)]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:]
    limited = client.post("/v1/price/hotel", json=HOTEL, headers=slow)
    assert _code(limited) == (429, "rate_limited") and int(limited.headers["Retry-After"]) >= 1
    assert client.post("/v1/price/hotel", json=HOTEL, headers=auth("hotel_001", "pricing")).status_code == 200


def test_only_hashes_are_stored(database):
    secret = keylib.parse(database.keys["hotel_001"]["pricing"])[1]
    with psycopg.connect(database.owner_url) as conn:
        rows = conn.execute("SELECT key_id, secret_sha256 FROM api_keys").fetchall()
    assert rows and all(len(bytes(h)) == 32 for _, h in rows)
    assert all(secret.encode() not in bytes(h) for _, h in rows)
    assert keylib.hash_secret(secret) in {bytes(h) for _, h in rows}


# ---------------------------------------------------------------- the database enforces it too

@pytest.fixture
def as_hotel(database):
    """Run SQL as the app's own account inside hotel_001's tenant context, like the app does."""
    def run(sql: str):
        with psycopg.connect(database.app_url) as conn, conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', 'hotel_001', true)")
            cur = conn.execute(sql)
            return cur.fetchall() if cur.description else cur.rowcount
    return run


def test_rls_a_tenant_sees_only_its_own_rows(as_hotel):
    assert {r[0] for r in as_hotel("SELECT tenant_id FROM api_keys")} == {"hotel_001"}
    assert {r[0] for r in as_hotel("SELECT tenant_id FROM packs")} == {"hotel_001"}
    assert as_hotel("SELECT id FROM tenants") == [("hotel_001",)]
    assert as_hotel("SELECT 1 FROM packs WHERE tenant_id = 'airline_002'") == []


def test_rls_without_a_tenant_context_the_tables_look_empty(database):
    with psycopg.connect(database.app_url) as conn:
        for table in ("api_keys", "packs", "pack_pointer", "tenant_policy", "tenants", "audit_log"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


@pytest.mark.parametrize("sql, why", [
    ("INSERT INTO api_keys (key_id, tenant_id, secret_sha256, role, domains) "
     "VALUES ('EVILEVIL01', 'airline_002', decode('00','hex'), 'admin', '{*}')", "row-level security"),
    ("UPDATE api_keys SET role = 'admin'", "permission denied"),
    ("UPDATE api_keys SET secret_sha256 = decode('00','hex')", "permission denied"),
    ("DELETE FROM api_keys", "permission denied"),
    ("UPDATE tenants SET name = 'hacked'", "permission denied"),
    ("INSERT INTO tenants (id, name) VALUES ('evil_999', 'Evil')", "permission denied"),
    ("UPDATE packs SET yaml = 'evil'", "permission denied"),
    ("UPDATE audit_log SET entity = 'x'", "permission denied"),
    ("DELETE FROM audit_log", "permission denied"),
    ("DROP TABLE audit_log", "must be owner"),
    ("ALTER TABLE api_keys DISABLE ROW LEVEL SECURITY", "must be owner"),
])
def test_the_app_account_cannot_break_isolation_or_rewrite_history(as_hotel, sql, why):
    with pytest.raises(psycopg.Error, match=why):
        as_hotel(sql)


def test_key_lookup_function_returns_one_key_and_nothing_else(database):
    key_id = keylib.parse(database.keys["airline_002"]["pricing"])[0]
    with psycopg.connect(database.app_url) as conn:
        rows = conn.execute("SELECT key_id, tenant_id FROM lookup_api_key(%s)", (key_id,)).fetchall()
        assert rows == [(key_id, "airline_002")]
        assert conn.execute("SELECT * FROM lookup_api_key('%')").fetchall() == []      # no wildcards


# ---------------------------------------------------------------- Postgres goes down

def test_when_the_database_is_unreachable_known_keys_still_price(database, tmp_path):
    app = create_pricing_app(database_url=database.app_url, audit_spool_dir=tmp_path)
    with TestClient(app) as c:
        known = {"Authorization": f"Bearer {database.keys['hotel_001']['pricing']}"}
        unseen = {"Authorization": f"Bearer {database.keys['airline_002']['pricing']}"}
        assert c.post("/v1/price/hotel", json=HOTEL, headers=known).status_code == 200

        app.state.db.close()                  # the database is gone ...
        app.state.keys._ttl = 0               # ... and every cached key is past its TTL

        r = c.post("/v1/price/hotel", json=HOTEL, headers=known)
        assert r.status_code == 200 and r.json()["price"] == "4950.00"
        assert _code(c.post("/v1/price/airline", json=AIRLINE, headers=unseen)) == (503, "auth_unavailable")
        ready = c.get("/readyz").json()
        assert ready["ready"] is True and ready["database"] == "down"
