"""The max_change guard and idempotency keys against a real Postgres.

max_change: the price may move at most 15% from the price at the START of the hour-long window, so asking
again and again can't walk it up 15% each time. Idempotency: a retried request gets the first answer, not a
second price. Both commit in the same transaction as the audit row."""
import threading
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from monetize360.api.app import create_app

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)          # a Wednesday, no festival


def at(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def body(entity: str, minutes: int, base="3000", occupancy=0.5, **extra) -> dict:
    return {"entity": entity, "base_price": base, "signals": {"occupancy": occupancy}, "as_of": at(minutes), **extra}


@pytest.fixture
def price(client, auth):
    def run(b: dict, tenant="hotel_001", domain="hotel", expect=200) -> dict:
        r = client.post(f"/v1/price/{domain}", json=b, headers=auth(tenant, "admin"))
        assert r.status_code == expect, r.text
        return r.json()
    return run


@pytest.fixture
def rows(client, auth):
    def count(tenant="hotel_001", domain="hotel") -> int:
        r = client.post("/v1/audit/verify", json={"domain": domain}, headers=auth(tenant, "admin")).json()
        assert r["ok"] is True
        return r["rows_checked"]
    return count


def clamp_rows(p: dict) -> list[tuple[str, str]]:
    return [(r["step"], r["delta"]) for r in p["waterfall"] if r["kind"] == "clamp"]


# ---------------------------------------------------------------- max_change

def test_the_guard_is_enforced_and_no_longer_flagged(price):
    first = price(body("guard-1", 0))
    assert first["price"] == "3000.00" and first["flags"] == []
    assert first["bounds"] == {"lower": "2100.00", "upper": "5000.00"}        # first price: nothing to compare with

    second = price(body("guard-1", 10, occupancy=0.9))                        # rules say 3600 (+20%)
    assert second["price"] == "3450.00"                                       # ... the guard allows 3000 + 15%
    assert clamp_rows(second) == [("max_change", "-150.00")]
    assert second["bounds"] == {"lower": "2550.00", "upper": "3450.00"}
    assert second["flags"] == []


def test_asking_again_and_again_cannot_walk_the_price_up(price):
    price(body("creep", 0))                                                   # 3000 starts the window
    seen = [price(body("creep", m, occupancy=0.9))["price"] for m in (5, 10, 20, 40, 59, 60)]
    assert seen == ["3450.00"] * 6          # compared with the last price instead, the 2nd call would give 3600


def test_when_the_window_ends_the_last_price_starts_the_next_one(price, client, auth):
    price(body("window", 0))
    assert price(body("window", 30, occupancy=0.9))["price"] == "3450.00"
    after = price(body("window", 61, occupancy=0.9))                          # > 1 h after the anchor
    assert after["price"] == "3600.00"                                        # 3450 + 15% = 3967.50 allows 3600
    assert after["bounds"] == {"lower": "2932.50", "upper": "3967.50"}
    assert price(body("window", 70, occupancy=0.9))["bounds"]["upper"] == "3967.50"     # same new window

    rec = client.get(f"/v1/audit/{after['price_id']}", headers=auth("hotel_001", "admin")).json()
    assert rec["reference_price"] == "3450.00"                                # recorded, so replay is exact
    replay = client.post("/v1/replay", json={"price_id": after["price_id"]}, headers=auth("hotel_001", "admin"))
    assert replay.json()["identical"] is True


def test_the_guard_also_stops_a_sudden_drop(price):
    price(body("drop", 0, base="4000"))                                       # 4000
    down = price(body("drop", 10, base="2000"))                               # rules say 2000
    assert down["price"] == "3400.00" and clamp_rows(down) == [("max_change", "1400.00")]      # 4000 - 15%


def test_each_entity_and_each_tenant_has_its_own_anchor(price):
    price(body("room-a", 0))
    assert price(body("room-a", 10, occupancy=0.9))["price"] == "3450.00"
    assert price(body("room-b", 10, occupancy=0.9))["price"] == "3600.00"     # another room: no history
    assert price(body("room-a", 10, occupancy=0.9), tenant="travel_003")["price"] == "3600.00"   # another company


def test_a_failed_price_does_not_move_the_anchor(price):
    price(body("failed", 0))
    price(body("failed", 5, base="8000"), expect=422)                          # floor above ceiling
    price(body("failed", 6, occupancy=7), expect=422)
    assert price(body("failed", 10, occupancy=0.9))["price"] == "3450.00"      # still anchored at 3000


def test_a_pack_without_max_change_has_no_guard_but_still_prices(price):
    ride = {"entity": "trip-9", "as_of": at(0),
            "signals": {"distance_km": 10, "duration_min": 30, "demand_ratio": 1, "vehicle": "sedan"}}
    low = price(ride, tenant="travel_003", domain="ride")
    high = price({**ride, "signals": {**ride["signals"], "demand_ratio": 4}}, tenant="travel_003", domain="ride")
    assert (low["price"], high["price"]) == ("215.00", "538.00")              # 2.5x in a minute: this pack allows it
    assert low["flags"] == high["flags"] == []


def test_many_requests_for_one_room_from_two_servers_all_respect_one_anchor(client, auth, price, database, tmp_path):
    price(body("busy", 0))
    second = create_app(database_url=database.app_url, audit_spool_dir=tmp_path)
    headers, got, lock = auth("hotel_001", "pricing"), [], threading.Lock()

    with TestClient(second) as other:
        def hammer(c):
            for _ in range(10):
                r = c.post("/v1/price/hotel", json=body("busy", 5, occupancy=0.9), headers=headers)
                with lock:
                    got.append((r.status_code, r.json().get("price")))
        threads = [threading.Thread(target=hammer, args=(c,)) for c in (client, other) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert got == [(200, "3450.00")] * 60


# ---------------------------------------------------------------- idempotency

def test_a_retry_with_the_same_key_gets_the_first_answer_and_no_second_record(price, rows):
    before = rows()
    b = body("idem-1", 0, occupancy=0.9, idempotency_key="booking-1")
    first, again, third = price(b), price(b), price(b)
    assert first == again == third and len(first["price_id"]) == 26
    assert "idempotency" not in " ".join(first["flags"])
    assert rows() == before + 1


def test_the_retry_does_not_move_the_anchor_either(price):
    price(body("idem-anchor", 0))
    b = body("idem-anchor", 10, occupancy=0.9, idempotency_key="booking-anchor")
    assert price(b)["price"] == price(b)["price"] == "3450.00"
    assert price(body("idem-anchor", 20, occupancy=0.9))["price"] == "3450.00"       # anchor is still 3000


def test_the_same_key_with_a_different_request_is_a_conflict(price, rows, client, auth):
    price(body("idem-2", 0, idempotency_key="booking-2"))
    before = rows()
    r = client.post("/v1/price/hotel", json=body("idem-2", 0, base="3500", idempotency_key="booking-2"),
                    headers=auth("hotel_001", "admin"))
    assert (r.status_code, r.json()["error"]) == (409, "idempotency_conflict")
    assert r.json()["details"] == [{"path": "idempotency_key", "value": "booking-2"}]
    assert rows() == before


def test_keys_belong_to_one_tenant_and_one_domain(price):
    b = body("idem-3", 0, idempotency_key="shared-key")
    mine = price(b)
    theirs = price(b, tenant="travel_003")                                    # another company, same key text
    assert mine["price_id"] != theirs["price_id"]
    ride = {"entity": "idem-3", "as_of": at(0), "idempotency_key": "shared-key",
            "signals": {"distance_km": 10, "duration_min": 30, "demand_ratio": 2, "vehicle": "sedan"}}
    assert price(ride, tenant="travel_003", domain="ride")["price"] == "323.00"     # same tenant, another domain


def test_a_request_without_as_of_is_still_recognised_as_the_same(price):
    b = {"entity": "idem-now", "base_price": "3000", "idempotency_key": "booking-now"}
    assert price(b) == price(b)


def test_a_failed_request_does_not_use_up_the_key(price):
    price(body("idem-fail", 0, occupancy=7, idempotency_key="booking-fail"), expect=422)
    assert price(body("idem-fail", 0, idempotency_key="booking-fail"))["price"] == "3000.00"


def test_ten_identical_requests_at_once_make_one_price(client, auth, rows):
    before = rows()
    headers, got, lock = auth("hotel_001", "pricing"), [], threading.Lock()
    b = body("idem-race", 0, idempotency_key="booking-race")

    def call():
        r = client.post("/v1/price/hotel", json=b, headers=headers)
        with lock:
            got.append((r.status_code, r.json().get("price_id")))
    threads = [threading.Thread(target=call) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(got) == 10 and len(set(got)) == 1 and got[0][0] == 200
    assert rows() == before + 1


def test_a_key_can_be_used_again_after_24_hours(price, database):
    first = price(body("idem-old", 0, idempotency_key="booking-old"))
    with psycopg.connect(database.owner_url) as conn:
        assert conn.execute("UPDATE idempotency_keys SET created_at = now() - interval '25 hours' "
                            "WHERE key = 'booking-old'").rowcount == 1
    again = price(body("idem-old", 0, base="3200", idempotency_key="booking-old"))      # a different body is fine now
    assert again["price_id"] != first["price_id"] and again["price"] == "3200.00"
    with psycopg.connect(database.owner_url) as conn:
        assert conn.execute("SELECT count(*) FROM idempotency_keys WHERE key = 'booking-old'").fetchone()[0] == 1


def test_batch_items_honour_their_keys(client, auth, rows):
    before = rows()
    item = body("idem-batch", 0, idempotency_key="booking-batch")
    r = client.post("/v1/price/hotel/batch", json={"items": [item, item, {**item, "base_price": "3100"}]},
                    headers=auth("hotel_001", "admin")).json()["results"]
    assert r[0]["price_id"] == r[1]["price_id"] and r[2]["error"] == "idempotency_conflict"
    assert rows() == before + 1


def test_simulate_ignores_keys_and_anchors(client, auth, price):
    price(body("sim", 0))
    r = client.post("/v1/simulate/hotel", json=body("sim", 10, occupancy=0.9), headers=auth("hotel_001", "admin"))
    assert r.json()["price"] == "3600.00"                                     # a what-if: no stored anchor applied
    assert price(body("sim", 10, occupancy=0.9))["price"] == "3450.00"


# ---------------------------------------------------------------- isolation and outage

def test_the_database_keeps_state_and_keys_per_tenant(database, price):
    price(body("rls-room", 0, idempotency_key="rls-key"))
    price(body("rls-room", 0, idempotency_key="rls-key"), tenant="travel_003")
    seen = {}
    for tenant in ("hotel_001", "travel_003", "airline_002"):
        with psycopg.connect(database.app_url) as conn, conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
            for table in ("price_state", "idempotency_keys"):
                owners = {r[0] for r in conn.execute(f"SELECT DISTINCT tenant_id FROM {table}")}
                assert owners <= {tenant}, (tenant, table, owners)          # only ever its own rows
                seen[tenant, table] = bool(owners)
    assert seen["hotel_001", "price_state"] and seen["travel_003", "idempotency_keys"]
    with psycopg.connect(database.app_url) as conn:                           # no tenant set: nothing at all
        assert conn.execute("SELECT count(*) FROM price_state").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM idempotency_keys").fetchone()[0] == 0


def test_when_the_database_is_down_the_response_says_what_was_not_enforced(database, auth, tmp_path):
    app = create_app(database_url=database.app_url, audit_spool_dir=tmp_path)
    headers = auth("hotel_001", "pricing")
    with TestClient(app) as c:
        assert c.post("/v1/price/hotel", json=body("outage", 0), headers=headers).status_code == 200
        app.state.db.close()
        r = c.post("/v1/price/hotel", json=body("outage", 10, occupancy=0.9, idempotency_key="k"), headers=headers)
        assert r.status_code == 200 and r.json()["price"] == "3600.00"         # no anchor available: rules only
        assert r.json()["flags"] == ["max_change not enforced: database unreachable",
                                     "idempotency_key not honoured: database unreachable",
                                     "audit deferred: database unreachable, record spooled"]
        assert app.state.audit.pending() == 1
