"""What the admin console needs from the API, against a real Postgres: GET /v1/me, the newest-first audit list,
a version's rules as data, and the console's own files."""
import pytest

pytestmark = pytest.mark.integration


def _room(n: int) -> dict:
    return {"entity": f"console-room-{n}", "base_price": "3000", "signals": {"occupancy": 0.5},
            "as_of": "2026-10-14T09:00:00Z"}


def test_me_says_whose_key_it_is_and_nothing_else(client, auth):
    r = client.get("/v1/me", headers=auth("hotel_001", "pricing"))
    assert r.status_code == 200
    assert set(r.json()) == {"tenant_id", "key_id", "role", "domains"}
    assert (r.json()["tenant_id"], r.json()["role"]) == ("hotel_001", "pricing")
    assert client.get("/v1/me", headers=auth("travel_003", "pricing_hotel_only")).json()["domains"] == ["hotel"]


def test_me_without_a_working_key_is_401(client):
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers={"Authorization": "Bearer m360_live_AAAAAAAAAA_nope"}).status_code == 401


def test_audit_list_is_newest_first_and_only_your_own(client, auth):
    admin = auth("hotel_001", "admin")
    ids = [client.post("/v1/price/hotel", json=_room(n), headers=admin).json()["price_id"] for n in range(3)]

    rows = client.get("/v1/audit", params={"domain": "hotel", "limit": 3}, headers=admin).json()["rows"]
    assert [r["price_id"] for r in rows] == ids[::-1]
    assert rows[0]["seq"] > rows[1]["seq"] > rows[2]["seq"]
    assert rows[0]["price"] == "3000.00" and rows[0]["entity"] == "console-room-2"

    # another tenant asking for the same domain name sees none of them
    theirs = client.get("/v1/audit", params={"domain": "hotel", "limit": 500}, headers=auth("airline_002", "admin"))
    assert not set(ids) & {r["price_id"] for r in theirs.json().get("rows", [])}


def test_audit_list_needs_an_admin_key_and_a_sane_limit(client, auth):
    assert client.get("/v1/audit", params={"domain": "hotel"}, headers=auth("hotel_001", "editor")).status_code == 403
    assert client.get("/v1/audit", params={"domain": "hotel", "limit": 501},
                      headers=auth("hotel_001", "admin")).status_code in (400, 422)


def test_a_version_comes_with_its_rules_as_data(client, auth):
    admin = auth("hotel_001", "admin")
    live = client.get("/v1/packs/hotel", headers=admin).json()["live_version"]
    v = client.get(f"/v1/packs/hotel/{live}", headers=admin).json()
    assert v["pack"]["domain"] == "hotel"
    assert "occupancy" in v["pack"]["signals"] and v["pack"]["groups"][0]["rules"]


def test_console_files_are_served_with_a_strict_policy_and_hold_no_secret(client):
    page = client.get("/admin/")
    assert page.status_code == 200 and "js/app.js" in page.text
    policy = page.headers["content-security-policy"]
    assert "default-src 'self'" in policy and "unsafe" not in policy
    assert page.headers["x-content-type-options"] == "nosniff"
    for path in ("/admin/app.css", "/admin/icons.svg", "/admin/js/core.js", "/admin/fonts/inter.woff2"):
        assert client.get(path).status_code == 200, path
    assert client.get("/admin/../../config.py").status_code == 404


def test_domains_lists_only_what_the_key_may_price_and_only_what_to_send(client, auth):
    def names(tenant, label):
        r = client.get("/v1/domains", headers=auth(tenant, label))
        assert r.status_code == 200
        return {d["domain"] for d in r.json()["domains"]}

    assert names("travel_003", "pricing_hotel_only") == {"hotel"}
    assert {"hotel", "parking", "ride"} <= names("travel_003", "pricing_all")
    assert "airline" not in names("travel_003", "pricing_all")          # another tenant's domain

    hotel = next(d for d in client.get("/v1/domains", headers=auth("hotel_001", "pricing")).json()["domains"]
                 if d["domain"] == "hotel")
    assert set(hotel) == {"domain", "currency", "pack_version", "needs_base_price", "signals"}   # no rules, no limits
    assert hotel["needs_base_price"] is True
    assert [s["name"] for s in hotel["signals"]] == ["occupancy"]        # calendar signals are not the caller's job
    assert hotel["signals"][0]["default"] == "0.5" and hotel["signals"][0]["required"] is False
    assert client.get("/v1/domains").status_code == 401


def test_portal_page_is_served_with_the_same_strict_policy(client):
    page = client.get("/portal")
    assert page.status_code == 200 and "/admin/js/portal.js" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert "<style" not in page.text and "style=" not in page.text
    assert client.get("/admin/js/portal.js").status_code == 200
