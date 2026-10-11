"""Editing an audit row makes /audit/verify report that row.

M6 against a real Postgres: every price is appended to a hash chain in the same request; changing, removing or
inserting a row is detected and the first bad row is named; concurrent writers keep one unbroken chain; and
when the database is unreachable the record is spooled and appended later, so no price goes unrecorded."""
import threading

import psycopg
import pytest
from fastapi.testclient import TestClient

from monetize360.api.app import create_app
from monetize360.store import audit
from monetize360.store.db import Database

pytestmark = pytest.mark.integration

HOTEL = {"entity": "deluxe-101@2026-11-08", "base_price": "3000", "signals": {"occupancy": 0.85},
         "as_of": "2026-11-08T09:00:00Z"}
RIDE = {"entity": "trip-1", "as_of": "2026-10-14T08:30:00Z",
        "signals": {"distance_km": 10, "duration_min": 30, "demand_ratio": 2, "vehicle": "sedan"}}
PARKING = {"entity": "slot-9", "as_of": "2026-10-14T08:30:00Z", "signals": {"hours": 2.5, "occupancy": "0.95",
                                                                           "vehicle": "car"}}


@pytest.fixture
def verify(client, auth):
    def run(tenant: str, domain: str) -> dict:
        r = client.post("/v1/audit/verify", json={"domain": domain}, headers=auth(tenant, "admin"))
        assert r.status_code == 200, r.text
        return r.json()
    return run


@pytest.fixture
def as_owner(database):
    """SQL as the database OWNER: the only account that can rewrite history."""
    def run(sql: str, params=()):
        with psycopg.connect(database.owner_url) as conn:
            return conn.execute(sql, params).rowcount
    return run


def _price(client, auth, tenant, domain, body, label="admin") -> dict:
    r = client.post(f"/v1/price/{domain}", json=body, headers=auth(tenant, label))
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- every price is recorded

def test_every_price_has_an_audit_record_with_everything_needed_to_explain_it(client, auth):
    priced = _price(client, auth, "hotel_001", "hotel", HOTEL, "pricing")
    r = client.get(f"/v1/audit/{priced['price_id']}", headers=auth("hotel_001", "admin"))
    rec = r.json()
    assert r.status_code == 200
    assert rec["output"]["price"] == priced["price"] == "4950.00"
    assert rec["output"]["waterfall"] == priced["waterfall"]
    assert rec["inputs"] == {"base_price": "3000", "signals": {"occupancy": "0.85"}, "redis_signals": {},
                             "allow_public_override": False, "policy": None}
    assert rec["signals_used"]["festival"] == {"value": "diwali", "source": "public"}
    assert (rec["domain"], rec["entity"], rec["pack_version"]) == ("hotel", HOTEL["entity"], 1)
    assert (rec["pack_owner"], rec["pack_approver"]) == ("seed", "seed-approver")
    assert rec["as_of"] == "2026-11-08T09:00:00+00:00" and len(rec["hash"]) == 64 and len(rec["key_id"]) == 10
    assert "flags" in priced and not any("audit" in f for f in priced["flags"])


def test_simulate_is_never_audited(client, auth, verify):
    before = verify("travel_003", "hotel")["rows_checked"]
    assert client.post("/v1/simulate/hotel", json=HOTEL, headers=auth("travel_003", "admin")).status_code == 200
    assert verify("travel_003", "hotel")["rows_checked"] == before


def test_a_failed_price_leaves_no_record(client, auth, verify):
    before = verify("travel_003", "hotel")["rows_checked"]
    r = client.post("/v1/price/hotel", json={**HOTEL, "base_price": "8000"}, headers=auth("travel_003", "admin"))
    assert r.status_code == 422
    assert verify("travel_003", "hotel")["rows_checked"] == before


def test_batch_items_are_each_recorded(client, auth, verify):
    before = verify("travel_003", "hotel")["rows_checked"]
    items = [HOTEL, {**HOTEL, "signals": {"occupancy": 7}}, {**HOTEL, "base_price": "2000"}]
    r = client.post("/v1/price/hotel/batch", json={"items": items}, headers=auth("travel_003", "admin"))
    assert [("price_id" in x) for x in r.json()["results"]] == [True, False, True]
    assert verify("travel_003", "hotel")["rows_checked"] == before + 2


# ---------------------------------------------------------------- only the tenant's own records

def test_another_tenant_cannot_read_verify_or_replay_your_records(client, auth):
    price_id = _price(client, auth, "hotel_001", "hotel", HOTEL)["price_id"]
    other = auth("airline_002", "admin")
    r = client.get(f"/v1/audit/{price_id}", headers=other)
    assert (r.status_code, r.json()["error"]) == (404, "price_not_found")
    missing = client.get("/v1/audit/01DOESNOTEXIST", headers=other).json()
    assert (r.json()["error"], r.json()["details"]) == (missing["error"], missing["details"])
    assert client.post("/v1/replay", json={"price_id": price_id}, headers=other).status_code == 404
    assert client.post("/v1/audit/verify", json={"domain": "hotel"}, headers=other).json() == \
        {"ok": True, "rows_checked": 0, "head_hash": None}         # their own (empty) hotel chain, not yours
    assert client.get("/v1/audit/export?domain=hotel", headers=other).json()["rows"] == []


def test_audit_endpoints_need_an_admin_key(client, auth):
    for call in (lambda h: client.get("/v1/audit/01X", headers=h),
                 lambda h: client.post("/v1/audit/verify", json={"domain": "hotel"}, headers=h),
                 lambda h: client.post("/v1/replay", json={"price_id": "01X"}, headers=h),
                 lambda h: client.get("/v1/audit/export?domain=hotel", headers=h)):
        assert call(auth("hotel_001", "pricing")).status_code == 403
        assert call({}).status_code == 401


# ---------------------------------------------------------------- tamper detection

def test_an_edited_row_is_named(client, auth, verify, as_owner):
    ids = [_price(client, auth, "travel_003", "ride", {**RIDE, "entity": f"trip-{i}"})["price_id"] for i in range(5)]
    clean = verify("travel_003", "ride")
    assert clean["ok"] is True and clean["rows_checked"] >= 5 and len(clean["head_hash"]) == 64

    # the app's own account can't do this (see test_isolation); only the owner can rewrite a row
    assert as_owner("UPDATE audit_log SET output = jsonb_set(output, '{price}', '\"1.00\"') WHERE price_id = %s",
                    (ids[2],)) == 1
    broken = verify("travel_003", "ride")
    assert broken["ok"] is False
    assert broken["price_id"] == ids[2]
    assert "changed after it was written" in broken["reason"]
    assert broken["rows_checked"] == broken["first_broken_seq"] - 1          # everything before it is still good

    replayed = client.post("/v1/replay", json={"price_id": ids[2]}, headers=auth("travel_003", "admin")).json()
    assert replayed["identical"] is False                                    # replay disagrees with the forged price
    assert (replayed["original"]["price"], replayed["replayed"]["price"]) == ("1.00", "323.00")


@pytest.mark.parametrize("field, sql", [
    ("an input", "UPDATE audit_log SET inputs = jsonb_set(inputs, '{signals,hours}', '\"9\"') WHERE price_id = %s"),
    ("the entity", "UPDATE audit_log SET entity = 'someone-else' WHERE price_id = %s"),
    ("the time", "UPDATE audit_log SET as_of = as_of + interval '1 second' WHERE price_id = %s"),
    ("the pack version", "UPDATE audit_log SET pack_version = 9 WHERE price_id = %s"),
    ("the key", "UPDATE audit_log SET key_id = 'SOMEONEELS' WHERE price_id = %s"),
    ("the stored hash", "UPDATE audit_log SET hash = decode(repeat('ab', 32), 'hex') WHERE price_id = %s"),
])
def test_any_changed_field_is_detected(client, auth, verify, as_owner, database, field, sql):
    with psycopg.connect(database.owner_url) as conn:        # a fresh chain for each case
        conn.execute("DELETE FROM audit_log WHERE tenant_id = 'travel_003' AND domain = 'parking'")
    ids = [_price(client, auth, "travel_003", "parking", {**PARKING, "entity": f"slot-{i}"})["price_id"]
           for i in range(3)]
    assert verify("travel_003", "parking") == {"ok": True, "rows_checked": 3, "head_hash": verify(
        "travel_003", "parking")["head_hash"]}
    assert as_owner(sql, (ids[1],)) == 1
    broken = verify("travel_003", "parking")
    assert (broken["ok"], broken["price_id"], broken["first_broken_seq"]) == (False, ids[1], 2), field


def test_a_removed_row_and_an_inserted_row_are_detected(client, auth, verify, as_owner, database):
    with psycopg.connect(database.owner_url) as conn:
        conn.execute("DELETE FROM audit_log WHERE tenant_id = 'travel_003' AND domain = 'parking'")
    ids = [_price(client, auth, "travel_003", "parking", {**PARKING, "entity": f"slot-{i}"})["price_id"]
           for i in range(4)]

    assert as_owner("DELETE FROM audit_log WHERE price_id = %s", (ids[1],)) == 1
    gone = verify("travel_003", "parking")
    assert gone["ok"] is False and gone["price_id"] == ids[2] and "a row was removed" in gone["reason"]

    # put a forged row into the gap: right position, but it can't link to the row before it
    as_owner("INSERT INTO audit_log (price_id, tenant_id, domain, seq, pack_version, as_of, inputs, signal_sources, "
             "output, prev_hash, hash) VALUES ('FORGED', 'travel_003', 'parking', 2, 1, now(), '{}', '{}', "
             "'{\"price\": \"1.00\"}', decode(repeat('00', 32), 'hex'), decode(repeat('cd', 32), 'hex'))")
    forged = verify("travel_003", "parking")
    assert (forged["ok"], forged["price_id"], forged["first_broken_seq"]) == (False, "FORGED", 2)


# ---------------------------------------------------------------- many writers, one chain

def test_concurrent_prices_from_two_servers_keep_one_unbroken_chain(client, auth, verify, database, tmp_path):
    with psycopg.connect(database.owner_url) as conn:
        conn.execute("DELETE FROM audit_log WHERE tenant_id = 'airline_002'")
    second = create_app(database_url=database.app_url, audit_spool_dir=tmp_path)      # another replica
    body = {"entity": "6E-201", "as_of": "2026-10-14T09:00:00Z",
            "signals": {"seats_sold": "0.65", "days_to_departure": 3, "cabin": "business"}}
    headers, errors = auth("airline_002", "pricing"), []

    with TestClient(second) as other:
        def hammer(c):
            for _ in range(25):
                if c.post("/v1/price/airline", json=body, headers=headers).status_code != 200:
                    errors.append(1)
        threads = [threading.Thread(target=hammer, args=(c,)) for c in (client, other) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert not errors
    result = verify("airline_002", "airline")
    assert result["ok"] is True and result["rows_checked"] == 200
    with psycopg.connect(database.owner_url) as conn:
        seqs = [r[0] for r in conn.execute(
            "SELECT seq FROM audit_log WHERE tenant_id = 'airline_002' AND domain = 'airline' ORDER BY seq")]
    assert seqs == list(range(1, 201))                              # no gaps, no duplicates


# ---------------------------------------------------------------- the database goes away

def test_when_the_database_is_down_the_price_is_returned_and_the_record_is_kept(database, auth, tmp_path):
    with psycopg.connect(database.owner_url) as conn:
        conn.execute("DELETE FROM audit_log WHERE tenant_id = 'hotel_001'")
    app = create_app(database_url=database.app_url, audit_spool_dir=tmp_path)
    headers = auth("hotel_001", "pricing")
    with TestClient(app) as c:
        first = c.post("/v1/price/hotel", json=HOTEL, headers=headers).json()
        assert not any("audit" in f for f in first["flags"])

        app.state.db.close()                                        # Postgres is gone
        down = [c.post("/v1/price/hotel", json={**HOTEL, "entity": f"room-{i}"}, headers=headers) for i in range(3)]
        assert [r.status_code for r in down] == [200, 200, 200]
        assert all(r.json()["price"] == "4950.00" for r in down)
        assert all("audit deferred: database unreachable, record spooled" in r.json()["flags"] for r in down)
        assert app.state.audit.pending() == 3

    # Postgres is back: another process drains the spool into the chain, in order, exactly once
    db = Database(database.app_url)
    db.open()
    try:
        other = Database(database.owner_url.rsplit("/", 1)[0] + "/postgres")      # a DIFFERENT database
        assert audit.AuditLog(other, tmp_path).pending() == 0                    # ... sees none of these records
        log = audit.AuditLog(db, tmp_path)
        assert log.drain() == 3 and log.pending() == 0
        assert log.drain() == 0
        chain = audit.verify(db, "hotel_001", "hotel")
        assert chain["ok"] is True and chain["rows_checked"] == 4
        recorded = [r["price_id"] for r in audit.rows(db, "hotel_001", "hotel")]
        assert recorded == [first["price_id"], *(r.json()["price_id"] for r in down)]
    finally:
        db.close()


# ---------------------------------------------------------------- export

def test_export_json_and_csv_and_formula_cells_are_neutralised(client, auth, database):
    with psycopg.connect(database.owner_url) as conn:
        conn.execute("DELETE FROM audit_log WHERE tenant_id = 'travel_003' AND domain = 'parking'")
    admin = auth("travel_003", "admin")
    _price(client, auth, "travel_003", "parking", {**PARKING, "entity": "=HYPERLINK(\"http://evil\")"})
    _price(client, auth, "travel_003", "parking", {**PARKING, "entity": "slot-2", "as_of": "2026-10-20T08:30:00Z"})

    data = client.get("/v1/audit/export?domain=parking", headers=admin).json()
    assert [r["seq"] for r in data["rows"]] == [1, 2] and data["truncated"] is False
    assert data["rows"][0]["price"] == "100.00" and data["rows"][0]["currency"] == "INR"

    csv_text = client.get("/v1/audit/export?domain=parking&format=csv", headers=admin)
    assert csv_text.headers["content-type"].startswith("text/csv")
    lines = csv_text.text.splitlines()
    assert lines[0].startswith("seq,price_id,as_of,entity") and len(lines) == 3
    assert "'=HYPERLINK" in lines[1]                                # would run as a formula in a spreadsheet

    late = client.get("/v1/audit/export?domain=parking&from=2026-10-15T00:00:00Z", headers=admin).json()
    assert [r["entity"] for r in late["rows"]] == ["slot-2"]
    assert client.get("/v1/audit/export?domain=airline", headers=admin).status_code == 200      # theirs: empty
    assert client.get("/v1/audit/export?domain=parking&format=xml", headers=admin).status_code == 400
