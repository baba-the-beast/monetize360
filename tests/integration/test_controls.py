"""M7 against a real Postgres: tenant limits applied to live prices, and the kill switch.

Own tenant, so limits and kill switches set here can't change what other test files expect.
Hotel v1 on a weekday with base 3000: quiet (occupancy 0.5) = 3000, busy (0.9) = 3600."""
import time
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from monetize360.api.app import create_app
from monetize360.security import keys as keylib

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
TENANT = "controls_011"
HOTEL = (ROOT / "packs/templates/hotel.yaml").read_text(encoding="utf-8")
_seq = iter(range(1, 10_000))


def body(occupancy=0.9, entity=None, **extra) -> dict:
    return {"entity": entity or f"ctl-room-{next(_seq)}", "base_price": "3000", "signals": {"occupancy": occupancy},
            "as_of": "2026-10-14T09:00:00Z", **extra}


def _code(r):
    return r.status_code, r.json().get("error")


def clamps(p: dict) -> list[tuple[str, str]]:
    return [(r["step"], r["delta"]) for r in p["waterfall"] if r["kind"] == "clamp"]


@pytest.fixture(scope="module")
def keys(database, client):
    out = {}
    with psycopg.connect(database.owner_url) as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s, 'Controls Co') ON CONFLICT DO NOTHING", (TENANT,))
        conn.execute("INSERT INTO packs (tenant_id, domain, version, status, yaml, owner, approver) VALUES "
                     "(%s, 'hotel', 1, 'published', %s, 'seed', 'seed-approver') ON CONFLICT DO NOTHING",
                     (TENANT, HOTEL))
        conn.execute("INSERT INTO pack_pointer (tenant_id, domain, live_version) VALUES (%s, 'hotel', 1) "
                     "ON CONFLICT DO NOTHING", (TENANT,))
        for role in ("pricing", "editor", "admin"):
            key_id, api_key, secret_hash = keylib.generate("test")
            conn.execute("INSERT INTO api_keys (key_id, tenant_id, secret_sha256, role, domains) "
                         "VALUES (%s, %s, %s, %s, '{*}')", (key_id, TENANT, secret_hash, role))
            out[role] = {"Authorization": f"Bearer {api_key}"}
    client.app.state.refresh_packs()
    return out


@pytest.fixture(autouse=True)
def back_to_normal(client, keys):
    """Every test starts and ends with no limits and no kill switch."""
    yield
    assert client.put("/v1/admin/policy", json={}, headers=keys["admin"]).status_code == 200
    assert client.post("/v1/admin/kill", json={"domain": "hotel", "mode": "normal"},
                       headers=keys["admin"]).status_code == 200


def price(client, keys, b=None, expect=200) -> dict:
    r = client.post("/v1/price/hotel", json=b or body(), headers=keys["pricing"])
    assert r.status_code == expect, r.text
    return r.json()


def policy(client, keys, **limits):
    return client.put("/v1/admin/policy", json=limits, headers=keys["admin"])


def kill(client, keys, mode, domain="hotel"):
    return client.post("/v1/admin/kill", json={"domain": domain, "mode": mode}, headers=keys["admin"])


# ---------------------------------------------------------------- tenant limits on live prices

def test_a_surge_cap_applies_to_live_prices_at_once_and_is_explained(client, keys):
    assert price(client, keys)["price"] == "3600.00"                           # rules: busy = +20%

    r = policy(client, keys, max_surge_x="1.1")
    assert r.status_code == 200 and r.json()["max_surge_x"] == "1.10" and len(r.json()["updated_by"]) == 10
    capped = price(client, keys)
    assert capped["price"] == "3300.00"                                        # never more than 1.1 x base
    assert clamps(capped) == [("tenant_surge", "-300.00")]                     # ... and the waterfall says why
    assert capped["bounds"]["upper"] == "3300.00"
    assert price(client, keys, body(0.5))["price"] == "3000.00"                # a quiet room isn't touched

    assert policy(client, keys).status_code == 200                             # cleared
    assert price(client, keys)["price"] == "3600.00"


def test_a_minimum_price_lifts_prices_that_would_be_lower(client, keys):
    assert policy(client, keys, min_price="3500").status_code == 200
    lifted = price(client, keys, body(0.5))
    assert lifted["price"] == "3500.00" and clamps(lifted) == [("tenant_min", "500.00")]
    assert price(client, keys)["price"] == "3600.00"                           # already above the minimum


def test_the_limits_in_force_are_recorded_so_an_old_price_replays_after_they_change(client, keys):
    policy(client, keys, max_surge_x="1.1")
    capped = price(client, keys)
    rec = client.get(f"/v1/audit/{capped['price_id']}", headers=keys["admin"]).json()
    assert rec["inputs"]["policy"] == {"max_surge_x": "1.10"}

    policy(client, keys, max_surge_x="5")                                      # the limits change afterwards
    replay = client.post("/v1/replay", json={"price_id": capped["price_id"]}, headers=keys["admin"]).json()
    assert replay["identical"] is True and replay["replayed"]["price"] == "3300.00"
    assert price(client, keys)["price"] == "3600.00"                           # today's limits for today's prices


def test_limits_that_contradict_live_rules_are_refused(client, keys):
    r = policy(client, keys, max_price="3300")                                 # the live pack's ceiling is 5000
    assert _code(r) == (409, "policy_conflict") and r.json()["details"][0]["path"].startswith("hotel/")
    r = policy(client, keys, banned_signals=["occupancy"])                     # the live pack prices on occupancy
    assert _code(r) == (409, "policy_conflict")
    assert client.get("/v1/admin/policy", headers=keys["admin"]).json()["max_price"] is None   # nothing was saved
    assert price(client, keys)["price"] == "3600.00"

    assert policy(client, keys, max_price="6000", banned_signals=["customer_gender"]).status_code == 200
    shown = client.get("/v1/admin/policy", headers=keys["admin"]).json()
    assert (shown["max_price"], shown["banned_signals"]) == ("6000.00", ["customer_gender"])


@pytest.mark.parametrize("limits", [
    {"min_price": "5000", "max_price": "4000"},
    {"min_price": "-1"},
    {"max_price": "0"},
    {"max_surge_x": "0"},
    {"max_surge_x": "5000"},
    {"max_price": "1e30"},
    {"max_price": "abc"},
    {"banned_signals": ["Not A Signal"]},
    {"require_two_person": False},
    {"tenant_id": "hotel_001"},
])
def test_bad_limits_are_rejected(client, keys, limits):
    assert _code(policy(client, keys, **limits)) == (400, "bad_request"), limits


def test_banned_signals_block_a_draft_that_uses_them(client, keys):
    assert policy(client, keys, banned_signals=["loyalty_tier"]).status_code == 200
    text = HOTEL.replace("signals:\n", "signals:\n  loyalty_tier: { type: enum, values: [a, b], source: request }\n")
    v = client.put("/v1/packs/hotel", content=text, headers={**keys["editor"], "Content-Type": "application/yaml"})
    report = client.post(f"/v1/packs/hotel/{v.json()['version']}/lint", headers=keys["editor"]).json()
    assert report["ok"] is False and "banned_signal" in {e["code"] for e in report["errors"]}


def test_simulate_applies_the_limits_too(client, keys):
    policy(client, keys, max_surge_x="1.1")
    sim = client.post("/v1/simulate/hotel", json=body(), headers=keys["pricing"]).json()
    assert sim["price"] == "3300.00"


# ---------------------------------------------------------------- kill switch: base_only

def test_base_only_turns_the_rules_off_but_keeps_the_guards(client, keys):
    r = kill(client, keys, "base_only")
    assert r.status_code == 200 and (r.json()["mode"], r.json()["previous_mode"]) == ("base_only", "normal")
    p = price(client, keys)                                                    # busy: rules would say 3600
    assert p["price"] == "3000.00" and [x["step"] for x in p["waterfall"]] == ["base"]
    assert p["counterfactuals"] == [] and p["flags"] == [
        "kill switch: base_only (rules are off; base price, guards and rounding only)"]
    assert price(client, keys, body(base_price="3020"))["price"] == "3000.00"  # rounding still applies
    assert _code(client.post("/v1/price/hotel", json=body(base_price="8000"), headers=keys["pricing"])) == \
        (422, "bounds_conflict")                                               # and so do the guards

    replay = client.post("/v1/replay", json={"price_id": p["price_id"]}, headers=keys["admin"]).json()
    assert replay["identical"] is True                                         # replayed with the rules off, too

    assert kill(client, keys, "normal").json()["previous_mode"] == "base_only"
    assert price(client, keys)["price"] == "3600.00"


# ---------------------------------------------------------------- kill switch: freeze

def test_freeze_repeats_each_things_last_price_whatever_is_asked(client, keys):
    before = price(client, keys, body(0.9, entity="frozen-room"))              # 3600
    assert kill(client, keys, "freeze").status_code == 200

    quiet = price(client, keys, body(0.5, entity="frozen-room"))               # rules would now say 3000
    assert quiet["price"] == "3600.00" and quiet["price_id"] != before["price_id"]
    assert quiet["waterfall"] == [{"step": "frozen", "kind": "base", "delta": "3600.00", "after": "3600.00",
                                   "reason": "Kill switch: last issued price repeated"}]
    assert quiet["flags"] == ["kill switch: freeze (the last issued price is repeated)"]
    wild = price(client, keys, body(0.99, entity="frozen-room", base_price="4900"))
    assert wild["price"] == "3600.00"

    r = client.post("/v1/price/hotel", json=body(entity="never-priced-room"), headers=keys["pricing"])
    assert _code(r) == (503, "killed") and r.headers["Retry-After"] == "30"    # nothing to repeat: no price

    rec = client.get(f"/v1/audit/{quiet['price_id']}", headers=keys["admin"]).json()
    assert (rec["inputs"]["kill_mode"], rec["inputs"]["frozen_from"]) == ("freeze", before["price_id"])
    replay = client.post("/v1/replay", json={"price_id": quiet["price_id"]}, headers=keys["admin"]).json()
    assert replay["identical"] is None and replay["frozen_from"] == before["price_id"]
    assert "nothing was calculated" in replay["reason"]
    assert client.post("/v1/audit/verify", json={"domain": "hotel"}, headers=keys["admin"]).json()["ok"] is True

    kill(client, keys, "normal")
    after = price(client, keys, body(0.5, entity="frozen-room"))
    # rules again, and the max_change guard picks up where it left off: at most 15% below 3600 = 3060;
    # the nearest 50 (3050) would be below that, so rounding goes to the next allowed step, 3100
    assert after["price"] == "3100.00" and after["bounds"]["lower"] == "3060.00"


def test_the_switch_is_listed_and_the_policy_endpoint_shows_it(client, keys):
    kill(client, keys, "freeze")
    shown = client.get("/v1/admin/policy", headers=keys["admin"]).json()["kill_switches"]
    assert [(k["domain"], k["mode"]) for k in shown] == [("hotel", "freeze")] and len(shown[0]["set_by"]) == 10
    kill(client, keys, "normal")
    assert client.get("/v1/admin/policy", headers=keys["admin"]).json()["kill_switches"] == []


def test_only_an_admin_of_this_tenant_can_use_the_controls(client, keys, auth):
    for role in ("pricing", "editor"):
        assert kill(client, {"admin": keys[role]}, "freeze").status_code == 403
        assert policy(client, {"admin": keys[role]}, max_surge_x="2").status_code == 403
        assert client.get("/v1/admin/policy", headers=keys[role]).status_code == 403
    assert client.post("/v1/admin/kill", json={"domain": "hotel", "mode": "freeze"}).status_code == 401
    assert _code(kill(client, keys, "freeze", domain="airline")) == (404, "pack_not_found")     # not a domain here
    assert _code(kill(client, keys, "sideways")) == (400, "bad_request")

    # another company's switch and limits never touch this one, and the other way round
    assert kill(client, {"admin": auth("travel_003", "admin")}, "base_only").status_code == 200
    try:
        assert price(client, keys)["price"] == "3600.00"
        mine = kill(client, keys, "base_only")
        assert mine.json()["previous_mode"] == "normal"                        # theirs was never ours
    finally:
        kill(client, {"admin": auth("travel_003", "admin")}, "normal")


# ---------------------------------------------------------------- every server obeys, and it survives a restart

def _wait(condition, seconds: float) -> float | None:
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        if condition():
            return time.monotonic() - start
        time.sleep(0.05)
    return None


def test_another_server_obeys_the_switch_and_the_limits(client, keys, database, tmp_path):
    other = create_app(database_url=database.app_url, audit_spool_dir=tmp_path, pack_poll_seconds=0.2)
    with TestClient(other) as c:
        def p():
            return c.post("/v1/price/hotel", json=body(), headers=keys["pricing"]).json().get("price")
        assert p() == "3600.00"
        kill(client, keys, "base_only")
        assert _wait(lambda: p() == "3000.00", 5) is not None, "the second server never obeyed the kill switch"
        kill(client, keys, "normal")
        policy(client, keys, max_surge_x="1.1")
        assert _wait(lambda: p() == "3300.00", 5) is not None, "the second server never applied the limits"


def test_a_server_started_later_already_knows_the_switch_is_on(client, keys, database, tmp_path):
    kill(client, keys, "base_only")
    fresh = create_app(database_url=database.app_url, audit_spool_dir=tmp_path)
    with TestClient(fresh) as c:
        p = c.post("/v1/price/hotel", json=body(), headers=keys["pricing"]).json()
        assert p["price"] == "3000.00" and "kill switch: base_only" in p["flags"][0]


def test_with_the_database_down_the_switch_and_limits_still_hold(client, keys, database, tmp_path):
    app = create_app(database_url=database.app_url, audit_spool_dir=tmp_path)
    with TestClient(app) as c:
        def call(b):
            return c.post("/v1/price/hotel", json=b, headers=keys["pricing"])
        assert call(body()).status_code == 200                                 # this server now knows the key
        policy(client, keys, max_surge_x="1.1")
        app.state.refresh_controls()
        app.state.db.close()                                                   # Postgres is gone

        capped = call(body()).json()
        assert capped["price"] == "3300.00" and clamps(capped) == [("tenant_surge", "-300.00")]
        assert "audit deferred: database unreachable, record spooled" in capped["flags"]

        app.state.controls.apply([], [{"tenant_id": TENANT, "domain": "hotel", "mode": "base_only"}])
        assert call(body()).json()["price"] == "3000.00"
        app.state.controls.apply([], [{"tenant_id": TENANT, "domain": "hotel", "mode": "freeze"}])
        assert _code(call(body())) == (503, "killed")                          # the last price can't be read
