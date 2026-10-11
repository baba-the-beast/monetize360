"""M5 against a real Postgres: draft -> lint -> verify -> approve -> publish -> rollback, and every server
picking up what is live.

Uses its own tenant so that publishing new rules here can't change the prices other test files expect."""
import sys
import threading
import time
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from monetize360.api.app import create_app
from monetize360.security import keys as keylib

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
TENANT = "lifecycle_010"
HOTEL_V1 = (ROOT / "packs/templates/hotel.yaml").read_text(encoding="utf-8")
HOTEL_V2 = HOTEL_V1.replace("then: { op: pct, value: 20 }", "then: { op: pct, value: 30 }")     # busier = +30%
YAML = {"Content-Type": "application/yaml"}
_seq = iter(range(1, 10_000))


def body(occupancy=0.9, **extra) -> dict:
    """A weekday request for a room nobody has priced before (so no max_change anchor is involved)."""
    return {"entity": f"lc-room-{next(_seq)}", "base_price": "3000", "signals": {"occupancy": occupancy},
            "as_of": "2026-10-14T09:00:00Z", **extra}


def _code(r):
    return r.status_code, r.json().get("error")


@pytest.fixture(scope="module")
def keys(database, client):
    """A tenant with hotel v1 live and one key per role, created by the operator (database owner)."""
    out = {}
    with psycopg.connect(database.owner_url) as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s, 'Lifecycle Co') ON CONFLICT DO NOTHING", (TENANT,))
        conn.execute("INSERT INTO tenant_policy (tenant_id) VALUES (%s) ON CONFLICT DO NOTHING", (TENANT,))
        conn.execute("INSERT INTO packs (tenant_id, domain, version, status, yaml, owner, approver) VALUES "
                     "(%s, 'hotel', 1, 'published', %s, 'seed', 'seed-approver') ON CONFLICT DO NOTHING",
                     (TENANT, HOTEL_V1))
        conn.execute("INSERT INTO pack_pointer (tenant_id, domain, live_version) VALUES (%s, 'hotel', 1) "
                     "ON CONFLICT DO NOTHING", (TENANT,))
        for role in ("pricing", "editor", "approver", "admin"):
            key_id, api_key, secret_hash = keylib.generate("test")
            conn.execute("INSERT INTO api_keys (key_id, tenant_id, secret_sha256, role, domains) "
                         "VALUES (%s, %s, %s, %s, '{*}')", (key_id, TENANT, secret_hash, role))
            out[role] = {"Authorization": f"Bearer {api_key}"}
    client.app.state.refresh_packs()
    return out


def price(client, keys, b=None, domain="hotel") -> dict:
    r = client.post(f"/v1/price/{domain}", json=b or body(), headers=keys["pricing"])
    assert r.status_code == 200, r.text
    return r.json()


def draft(client, keys, text=HOTEL_V2, domain="hotel", role="editor") -> int:
    r = client.put(f"/v1/packs/{domain}", content=text, headers={**keys[role], **YAML})
    assert r.status_code == 201, r.text
    return r.json()["version"]


def checked(client, keys, version, domain="hotel") -> None:
    assert client.post(f"/v1/packs/{domain}/{version}/lint", headers=keys["editor"]).json()["ok"] is True
    r = client.post(f"/v1/packs/{domain}/{version}/verify", json={"cases": 300}, headers=keys["editor"]).json()
    assert r["ok"] is True and r["badge"] == "verified_safe"


# ---------------------------------------------------------------- the whole lifecycle

def test_draft_to_live_to_rollback(client, keys):
    assert price(client, keys)["price"] == "3600.00"                           # v1: busy = +20%
    before = price(client, keys)

    v = draft(client, keys)
    assert v == 2
    assert price(client, keys)["price"] == "3600.00"                           # a draft prices nothing
    sim = client.post("/v1/simulate/hotel", json={**body(), "pack_version": v}, headers=keys["editor"])
    assert sim.json()["price"] == "3900.00" and sim.json()["pack_version"] == v     # ... but can be tried

    shown = client.get(f"/v1/packs/hotel/{v}", headers=keys["editor"]).json()
    assert shown["status"] == "draft" and shown["is_live"] is False and shown["live_version"] == 1
    assert "-        then: { op: pct, value: 20 }" in shown["diff_from_live"]
    assert "+        then: { op: pct, value: 30 }" in shown["diff_from_live"]

    assert _code(client.post(f"/v1/packs/hotel/{v}/publish", headers=keys["approver"])) == (409, "approval_required")
    r = client.post(f"/v1/packs/hotel/{v}/approve", headers=keys["approver"])
    assert _code(r) == (409, "checks_required") and {d["path"] for d in r.json()["details"]} == {"lint", "verify"}

    checked(client, keys, v)
    r = client.post(f"/v1/packs/hotel/{v}/approve", headers=keys["approver"])
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert price(client, keys)["price"] == "3600.00"                           # approved is still not live

    r = client.post(f"/v1/packs/hotel/{v}/publish", headers=keys["approver"])
    assert r.status_code == 200 and (r.json()["live_version"], r.json()["previous_version"]) == (2, 1)
    after = price(client, keys)
    assert (after["price"], after["pack_version"]) == ("3900.00", 2)           # live at once on this server

    listing = client.get("/v1/packs/hotel", headers=keys["editor"]).json()
    assert listing["live_version"] == 2
    assert {x["version"]: x["status"] for x in listing["versions"]} == {1: "archived", 2: "published"}
    assert all(len(x["approver"]) >= 4 for x in listing["versions"])

    # a price issued under v1 still replays as v1: old prices stay explainable after the rules change
    replay = client.post("/v1/replay", json={"price_id": before["price_id"]}, headers=keys["admin"]).json()
    assert replay["identical"] is True and replay["recorded_pack_version"] == 1
    what_if = client.post("/v1/replay", json={"price_id": before["price_id"], "pack_version": 2},
                          headers=keys["admin"]).json()
    assert (what_if["identical"], what_if["replayed"]["price"]) == (False, "3900.00")

    r = client.post("/v1/packs/hotel/rollback", json={"to_version": 1}, headers=keys["approver"])
    assert r.status_code == 200 and (r.json()["live_version"], r.json()["previous_version"]) == (1, 2)
    assert price(client, keys)["price"] == "3600.00"
    assert _code(client.post("/v1/packs/hotel/rollback", json={"to_version": 1}, headers=keys["approver"])) == \
        (409, "already_live")
    assert client.post("/v1/packs/hotel/rollback", json={"to_version": 2}, headers=keys["approver"]).status_code == 200
    assert price(client, keys)["pack_version"] == 2


def test_the_author_cannot_approve_their_own_version(client, keys):
    v = draft(client, keys, role="admin")                                       # an admin key wrote it ...
    checked(client, keys, v)
    r = client.post(f"/v1/packs/hotel/{v}/approve", headers=keys["admin"])      # ... and tries to sign it off
    assert _code(r) == (409, "same_person")
    assert client.post(f"/v1/packs/hotel/{v}/approve", headers=keys["approver"]).status_code == 200


def test_roles(client, keys):
    v = draft(client, keys)
    for role in ("pricing",):
        assert client.put("/v1/packs/hotel", content=HOTEL_V2, headers={**keys[role], **YAML}).status_code == 403
        assert client.get("/v1/packs/hotel", headers=keys[role]).status_code == 403
        assert client.post(f"/v1/packs/hotel/{v}/lint", headers=keys[role]).status_code == 403
    for call in (f"/v1/packs/hotel/{v}/approve", f"/v1/packs/hotel/{v}/publish"):
        assert client.post(call, headers=keys["editor"]).status_code == 403    # editors draft, they don't decide
    assert client.post("/v1/packs/hotel/rollback", json={"to_version": 1}, headers=keys["editor"]).status_code == 403
    assert client.get("/v1/packs/hotel").status_code == 401


def test_rollback_only_to_a_version_that_was_live(client, keys):
    v = draft(client, keys)
    assert _code(client.post("/v1/packs/hotel/rollback", json={"to_version": v}, headers=keys["approver"])) == \
        (409, "never_live")
    assert _code(client.post("/v1/packs/hotel/rollback", json={"to_version": 999}, headers=keys["approver"])) == \
        (404, "pack_not_found")
    assert _code(client.post("/v1/packs/hotel/999/publish", headers=keys["approver"])) == (404, "pack_not_found")


# ---------------------------------------------------------------- what can't be saved or approved

@pytest.mark.parametrize("why, text, status", [
    ("typo in a guard name", HOTEL_V1.replace("ceiling:", "ceilng:"), 422),
    ("unknown stacking", HOTEL_V1.replace("stacking: multiply", "stacking: multiplly"), 422),
    ("type error in a rule", HOTEL_V1.replace('"festival != \'none\'"', '"festival > 3"'), 422),
    ("unknown signal in a rule", HOTEL_V1.replace('"occupancy > 0.8"', '"ocupancy > 0.8"'), 422),
    ("another domain's pack", HOTEL_V1.replace("domain: hotel", "domain: airline"), 422),
    ("not YAML", "domain: [unclosed", 422),
    ("not a mapping", "- just\n- a list\n", 422),
    ("YAML anchors", "a: &x [1, 2]\nb: *x\n" + HOTEL_V1, 422),
    ("not UTF-8", b"\xff\xfe\x00d", 422),
    ("too large", "TOO_LARGE", 413),
], ids=lambda v: v if isinstance(v, str) and len(v) < 40 else "")
def test_a_bad_pack_is_rejected_and_nothing_is_stored(client, keys, why, text, status):
    if text == "TOO_LARGE":
        text = HOTEL_V1 + "#" * 300_000
    before = len(client.get("/v1/packs/hotel", headers=keys["editor"]).json()["versions"])
    r = client.put("/v1/packs/hotel", content=text, headers={**keys["editor"], **YAML})
    assert r.status_code == status, (why, r.text)
    assert r.json()["error"] in ("pack_invalid", "payload_too_large")
    if status == 422:
        assert r.json()["details"], why                                        # says what is wrong and where
    assert len(client.get("/v1/packs/hotel", headers=keys["editor"]).json()["versions"]) == before


def test_every_problem_is_listed_at_once(client, keys):
    bad = HOTEL_V1.replace("ceiling:", "ceilng:").replace("stacking: multiply", "stacking: multiplly")
    r = client.put("/v1/packs/hotel", content=bad, headers={**keys["editor"], **YAML})
    assert r.status_code == 422 and len(r.json()["details"]) >= 2


def test_an_unfair_pack_can_be_drafted_but_never_approved(client, keys):
    unfair = HOTEL_V1.replace("signals:\n", "signals:\n  religion: { type: enum, values: [a, b], source: request }\n")
    v = draft(client, keys, text=unfair)
    report = client.post(f"/v1/packs/hotel/{v}/lint", headers=keys["editor"]).json()
    assert report["ok"] is False and "fairness" in {e["code"] for e in report["errors"]}
    client.post(f"/v1/packs/hotel/{v}/verify", json={"cases": 200}, headers=keys["editor"])
    r = client.post(f"/v1/packs/hotel/{v}/approve", headers=keys["approver"])
    assert _code(r) == (409, "checks_required") and [d["path"] for d in r.json()["details"]] == ["lint"]
    assert client.get("/v1/packs/hotel", headers=keys["editor"]).json()["versions"][0]["lint_ok"] is False


def test_an_unsafe_pack_fails_verify_and_cannot_be_approved(client, keys):
    # two rules that can both fire in an exclusive group: only running it finds that
    unsafe = HOTEL_V1.replace("stacking: multiply", "stacking: exclusive")
    v = draft(client, keys, text=unsafe)
    client.post(f"/v1/packs/hotel/{v}/lint", headers=keys["editor"])
    report = client.post(f"/v1/packs/hotel/{v}/verify", json={"cases": 500}, headers=keys["editor"]).json()
    assert report["ok"] is False and report["badge"] is None and "exclusive_conflict" in report["violation_counts"]
    assert report["smallest"]["kind"] == "exclusive_conflict"                  # the smallest input that breaks it
    r = client.post(f"/v1/packs/hotel/{v}/approve", headers=keys["approver"])
    assert _code(r) == (409, "checks_required") and "verify" in [d["path"] for d in r.json()["details"]]


def test_the_database_itself_refuses_a_live_pack_without_an_approver(client, keys, database):
    v = draft(client, keys)

    def as_app(sql: str, *params):
        """One statement as the application's own account, inside this tenant, in its own transaction."""
        with psycopg.connect(database.app_url) as conn, conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (TENANT,))
            return conn.execute(sql, params)

    with pytest.raises(psycopg.errors.CheckViolation):                          # live without any approver
        as_app("UPDATE packs SET status = 'published' WHERE domain = 'hotel' AND version = %s", v)
    with psycopg.connect(database.owner_url) as conn:
        owner = conn.execute("SELECT owner FROM packs WHERE tenant_id = %s AND domain = 'hotel' AND version = %s",
                             (TENANT, v)).fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation):                          # the author as their own approver
        as_app("UPDATE packs SET status = 'approved', approver = %s WHERE domain = 'hotel' AND version = %s", owner, v)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):                   # rewriting a stored version
        as_app("UPDATE packs SET yaml = 'evil' WHERE domain = 'hotel'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):                   # or deleting one
        as_app("DELETE FROM packs WHERE domain = 'hotel'")


# ---------------------------------------------------------------- impact preview

def test_impact_shows_what_a_draft_would_do_to_recent_real_prices(client, keys):
    live = client.get("/v1/packs/hotel", headers=keys["editor"]).json()["live_version"]
    for occupancy in (0.3, 0.5, 0.7, 0.9, 0.95, 0.99):
        price(client, keys, body(occupancy))
    current = client.get(f"/v1/packs/hotel/{live}", headers=keys["editor"]).json()["yaml"]
    pricier = current.replace("value: 30 }", "value: 40 }").replace("value: 20 }", "value: 40 }")
    v = draft(client, keys, text=pricier)
    r = client.post(f"/v1/packs/hotel/{v}/impact", json={"last_n": 6}, headers=keys["editor"])
    out = r.json()
    assert r.status_code == 200 and out["compared"] == 6 and out["compared_with_live_version"] == live
    assert (out["up_pct"], out["down_pct"], out["unchanged_pct"]) == ("50.0", "0.0", "50.0")    # only the busy three
    assert out["would_fail"] == 0 and out["replay_mismatches"] == 0
    assert len(out["biggest_movers"]) == 3


# ---------------------------------------------------------------- a new industry, no restart, no engine change

def test_a_brand_new_domain_goes_live_and_prices_without_a_restart(client, keys):
    ev = (ROOT / "packs/templates/ev_charging.yaml").read_text(encoding="utf-8")
    request = {"entity": "charger-1", "as_of": "2026-10-14T12:30:00Z",
               "signals": {"grid_price": "8.00", "charger": "dc_fast", "member": True}}
    assert _code(client.post("/v1/price/ev_charging", json=request, headers=keys["pricing"])) == \
        (404, "pack_not_found")

    v = draft(client, keys, text=ev, domain="ev_charging")
    assert v == 1
    checked(client, keys, v, domain="ev_charging")
    assert client.post(f"/v1/packs/ev_charging/{v}/approve", headers=keys["approver"]).status_code == 200
    assert client.post(f"/v1/packs/ev_charging/{v}/publish", headers=keys["approver"]).json() == \
        {"live_version": 1, "previous_version": None, "broadcast": False}

    priced = price(client, keys, request, domain="ev_charging")
    assert (priced["price"], priced["currency"], priced["pack_version"]) == ("19.50", "INR", 1)   # hand-calculated
    domains = client.get("/v1/packs", headers=keys["editor"]).json()["domains"]
    assert {d["domain"]: d["live_version"] for d in domains}["ev_charging"] == 1


# ---------------------------------------------------------------- only your own tenant, only your own scope

def test_packs_are_invisible_to_other_tenants_and_outside_a_keys_scope(client, keys, auth):
    other = auth("airline_002", "admin")
    assert _code(client.get("/v1/packs/hotel", headers=other)) == (404, "pack_not_found")
    assert _code(client.get("/v1/packs/hotel/1", headers=other)) == (404, "pack_not_found")
    assert _code(client.post("/v1/packs/hotel/1/publish", headers=other)) == (404, "pack_not_found")
    assert "hotel" not in {d["domain"] for d in client.get("/v1/packs", headers=other).json()["domains"]}

    scoped = auth("hotel_001", "editor")                                        # a key for `hotel` only
    assert client.get("/v1/packs/hotel", headers=scoped).status_code == 200
    r = client.put("/v1/packs/airline", content=HOTEL_V1, headers={**scoped, **YAML})
    assert _code(r) == (404, "pack_not_found")
    assert _code(client.get("/v1/packs/Bad Name", headers=keys["editor"])) == (404, "pack_not_found")


def test_two_drafts_saved_at_the_same_moment_get_different_version_numbers(client, keys):
    got, lock = [], threading.Lock()

    def save():
        r = client.put("/v1/packs/hotel", content=HOTEL_V2, headers={**keys["editor"], **YAML})
        with lock:
            got.append((r.status_code, r.json().get("version")))
    threads = [threading.Thread(target=save) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(code == 201 for code, _ in got) and len({v for _, v in got}) == 8


# ---------------------------------------------------------------- every server picks up what is live

def _wait(condition, seconds: float) -> float | None:
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        if condition():
            return time.monotonic() - start
        time.sleep(0.05)
    return None


def _publish_next(client, keys, text) -> int:
    v = draft(client, keys, text=text)
    checked(client, keys, v)
    assert client.post(f"/v1/packs/hotel/{v}/approve", headers=keys["approver"]).status_code == 200
    r = client.post(f"/v1/packs/hotel/{v}/publish", headers=keys["approver"])
    assert r.status_code == 200, r.text
    return v


def test_another_server_switches_to_a_published_version_by_polling(client, keys, database, tmp_path):
    other = create_app(database_url=database.app_url, audit_spool_dir=tmp_path, pack_poll_seconds=0.2)
    with TestClient(other) as c:
        v = _publish_next(client, keys, HOTEL_V1.replace("value: 20 }", "value: 50 }"))     # busy = +50%
        took = _wait(lambda: c.post("/v1/price/hotel", json=body(), headers=keys["pricing"]).json()
                     .get("pack_version") == v, 5)
        assert took is not None, "the second server never picked up the new version"
        assert c.post("/v1/price/hotel", json=body(), headers=keys["pricing"]).json()["price"] == "4500.00"


def test_another_server_hears_a_publish_through_redis_without_waiting_for_its_poll(client, keys, database, tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import setup_local_db
    url = setup_local_db.read_env().get("REDIS_URL")
    if not url:
        pytest.skip("no REDIS_URL in .env.local")
    # both servers poll only every 10 minutes, so anything quicker than that came through the broadcast
    a = create_app(database_url=database.app_url, audit_spool_dir=tmp_path / "a", redis_url=url, pack_poll_seconds=600)
    b = create_app(database_url=database.app_url, audit_spool_dir=tmp_path / "b", redis_url=url, pack_poll_seconds=600)
    with TestClient(a) as ca, TestClient(b) as cb:
        if _wait(lambda: a.state.events.connected and b.state.events.connected, 15) is None:
            pytest.skip("Redis is not reachable from here")
        v = draft(ca, keys, text=HOTEL_V1.replace("value: 20 }", "value: 60 }"))
        checked(ca, keys, v)
        ca.post(f"/v1/packs/hotel/{v}/approve", headers=keys["approver"])
        r = ca.post(f"/v1/packs/hotel/{v}/publish", headers=keys["approver"])
        assert r.status_code == 200 and r.json()["broadcast"] is True
        took = _wait(lambda: cb.post("/v1/price/hotel", json=body(), headers=keys["pricing"]).json()
                     .get("pack_version") == v, 10)
        assert took is not None, "server B never heard about the publish"
        print(f"\n   server B priced with the new version {took:.2f} s after server A published it")
