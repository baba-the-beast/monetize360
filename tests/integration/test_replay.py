"""Replay of audited price_ids is identical.

M6 against a real Postgres: any price ever issued can be re-run from its record and gives the same price and
the same waterfall, for every domain; replay never writes anything."""
import pytest

pytestmark = pytest.mark.integration

CASES = [
    ("hotel_001", "hotel", {"entity": "r1", "base_price": "3000", "signals": {"occupancy": 0.85},
                            "as_of": "2026-11-08T09:00:00Z"}, "4950.00"),
    ("hotel_001", "hotel", {"entity": "r2", "base_price": 3000.10, "signals": {"occupancy": 0.5},
                            "as_of": "2026-10-14T09:00:00Z"}, "3000.00"),
    ("hotel_001", "hotel", {"entity": "r3", "base_price": "1234.56", "as_of": "2026-03-04T23:59:59.999999+05:30"},
     "1550.00"),                                              # defaults, a festival day, microseconds, another zone
    ("airline_002", "airline", {"entity": "6E-201", "as_of": "2026-10-14T09:00:00Z",
                                "signals": {"seats_sold": "0.65", "days_to_departure": 3, "cabin": "business"}},
     "25499.00"),
    ("travel_003", "ride", {"entity": "t1", "as_of": "2026-10-14T08:30:00Z",
                            "signals": {"distance_km": 10, "duration_min": 30, "demand_ratio": 2,
                                        "vehicle": "sedan"}}, "323.00"),
    ("travel_003", "parking", {"entity": "s1", "as_of": "2026-10-17T08:30:00Z",
                               "signals": {"hours": 2.5, "occupancy": "0.95", "vehicle": "suv"}}, None),
]


@pytest.mark.parametrize("tenant, domain, body, price", CASES, ids=[f"{c[1]}-{c[2]['entity']}" for c in CASES])
def test_replay_gives_the_same_price_and_waterfall(client, auth, tenant, domain, body, price):
    admin = auth(tenant, "admin")
    priced = client.post(f"/v1/price/{domain}", json=body, headers=admin)
    assert priced.status_code == 200, priced.text
    if price is not None:
        assert priced.json()["price"] == price

    r = client.post("/v1/replay", json={"price_id": priced.json()["price_id"]}, headers=admin)
    out = r.json()
    assert r.status_code == 200
    assert out["identical"] is True and out["diff"] == []
    assert out["replayed"]["price"] == priced.json()["price"]
    assert out["replayed"]["waterfall"] == priced.json()["waterfall"]
    assert out["recorded_pack_version"] == out["replayed_pack_version"] == 1


def test_replay_writes_nothing_and_can_be_repeated(client, auth):
    admin = auth("hotel_001", "admin")
    price_id = client.post("/v1/price/hotel", json=CASES[0][2], headers=admin).json()["price_id"]
    before = client.post("/v1/audit/verify", json={"domain": "hotel"}, headers=admin).json()
    outs = [client.post("/v1/replay", json={"price_id": price_id}, headers=admin).json() for _ in range(3)]
    assert all(o["identical"] for o in outs) and outs[0] == outs[1] == outs[2]
    assert client.post("/v1/audit/verify", json={"domain": "hotel"}, headers=admin).json() == before


def test_replay_against_a_version_that_does_not_exist_is_404(client, auth):
    admin = auth("hotel_001", "admin")
    price_id = client.post("/v1/price/hotel", json=CASES[0][2], headers=admin).json()["price_id"]
    r = client.post("/v1/replay", json={"price_id": price_id, "pack_version": 99}, headers=admin)
    assert (r.status_code, r.json()["error"]) == (404, "pack_not_found")
    r = client.post("/v1/replay", json={"price_id": "01NOPE"}, headers=admin)
    assert (r.status_code, r.json()["error"]) == (404, "price_not_found")
