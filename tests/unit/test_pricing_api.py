"""M3 pricing API over HTTP (in-process, no Postgres/Redis): /v1/price, /batch, /simulate, ops endpoints, the
error format, and the closed-by-default rule. The headline test reproduces the docs/API.md example exactly."""
from decimal import Decimal

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from monetize360.api import deps
from monetize360.api.app import create_control_app, create_pricing_app
from monetize360.api.errors import install
from monetize360.security.principal import Principal

DIWALI_SUNDAY = "2026-11-08T09:00:00Z"          # Sunday and Diwali in the hotel pack's calendar
WEDNESDAY = "2026-10-14T09:00:00Z"
HOTEL = {"entity": "deluxe-101@2026-11-08", "base_price": "3000", "signals": {"occupancy": 0.85},
         "as_of": DIWALI_SUNDAY}


@pytest.fixture(scope="module")
def client():
    return TestClient(create_pricing_app(dev_tenant="demo"))


def test_simulate_hotel_matches_the_api_md_example(client):
    r = client.post("/v1/simulate/hotel", json={**HOTEL, "reference_price": "4500"})
    assert r.status_code == 200
    assert r.json() == {
        "price": "4950.00", "currency": "INR", "domain": "hotel", "pack_version": 1,
        "valid_until": "2026-11-08T09:15:00Z",
        "waterfall": [
            {"step": "base", "kind": "base", "delta": "3000.00", "after": "3000.00", "reason": "Base price"},
            {"step": "high-occ", "kind": "rule", "delta": "600.00", "after": "3600.00",
             "reason": "Occupancy above 80%"},
            {"step": "weekend", "kind": "rule", "delta": "360.00", "after": "3960.00", "reason": "Weekend"},
            {"step": "festival", "kind": "rule", "delta": "990.00", "after": "4950.00",
             "reason": "Festival: diwali"}],
        "counterfactuals": [
            {"if": "without: Festival: diwali", "price": "3950.00"},
            {"if": "without: Occupancy above 80%", "price": "4150.00"},
            {"if": "without: Weekend", "price": "4500.00"}],
        "signals_used": {
            "occupancy": {"value": "0.85", "source": "request"},
            "is_weekend": {"value": True, "source": "public"},
            "festival": {"value": "diwali", "source": "public"}},
        "bounds": {"lower": "3825.00", "upper": "5000.00"},
        "flags": [],
    }


def test_price_has_an_id_and_says_what_it_could_not_enforce(client):
    r = client.post("/v1/price/hotel", json={**HOTEL, "idempotency_key": "booking-8812"})
    body = r.json()
    assert r.status_code == 200
    assert len(body["price_id"]) == 26                       # ULID
    assert body["price"] == "4950.00"
    assert body["bounds"] == {"lower": "2100.00", "upper": "5000.00"}     # no anchor -> floor 70% of base
    assert body["flags"] == ["max_change not enforced: no database configured",       # dev-tenant mode
                             "idempotency_key not honoured: no database configured",
                             "not audited: no database configured"]
    assert client.post("/v1/price/hotel", json=HOTEL).json()["price_id"] != body["price_id"]


def test_waterfall_adds_up_and_money_is_two_decimal_strings(client):
    body = client.post("/v1/price/hotel", json={**HOTEL, "base_price": 3000.10}).json()
    rows = body["waterfall"]
    assert rows[0]["delta"] == "3000.10"                     # a JSON number is read from its text, not as a float
    assert sum(Decimal(r["delta"]) for r in rows) == Decimal(body["price"])
    for text in [body["price"], *(r["delta"] for r in rows), *(r["after"] for r in rows), *body["bounds"].values()]:
        assert isinstance(text, str) and len(text.split(".")[1]) == 2


def test_as_of_defaults_to_server_time(client):
    body = client.post("/v1/price/hotel", json={"entity": "r1", "base_price": "3000"}).json()
    assert body["valid_until"].endswith("Z")
    assert "occupancy missing, used default 0.5" in body["flags"]


def test_public_signals_cannot_be_sent_to_price_but_can_be_simulated(client):
    sneaky = {**HOTEL, "as_of": WEDNESDAY, "signals": {"occupancy": 0.85, "festival": "diwali"}}
    r = client.post("/v1/price/hotel", json=sneaky)
    assert r.status_code == 422 and r.json()["error"] == "signal_invalid"
    assert client.post("/v1/simulate/hotel", json=sneaky).json()["price"] == "4500.00"   # 3000 x 1.2 x 1.25


@pytest.mark.parametrize("body, status, code", [
    ({**HOTEL, "signals": {"occupancy": 7}}, 422, "signal_invalid"),
    ({**HOTEL, "signals": {"ocupancy": 0.5}}, 422, "signal_invalid"),
    ({"entity": "r1", "as_of": WEDNESDAY}, 422, "signal_missing"),                # no base_price
    ({**HOTEL, "base_price": "8000"}, 422, "bounds_conflict"),
    ({**HOTEL, "base_price": "1e30"}, 422, "signal_invalid"),
    ({**HOTEL, "as_of": "2026-11-08T09:00:00"}, 422, "signal_invalid"),          # no timezone
    ({"base_price": "3000"}, 400, "bad_request"),                                 # entity missing
    ({**HOTEL, "base_prise": "3000"}, 400, "bad_request"),                        # typo'd field
    ({**HOTEL, "base_price": True}, 400, "bad_request"),
    ({**HOTEL, "base_price": "NaN"}, 400, "bad_request"),
    ({**HOTEL, "entity": ""}, 400, "bad_request"),
])
def test_errors_have_one_shape(client, body, status, code):
    r = client.post("/v1/price/hotel", json=body)
    assert (r.status_code, r.json()["error"]) == (status, code)
    assert set(r.json()) == {"error", "message", "details", "request_id"}
    assert r.json()["request_id"] == r.headers["X-Request-Id"]


def test_bounds_conflict_details_are_json_safe(client):
    detail = client.post("/v1/price/hotel", json={**HOTEL, "base_price": "8000"}).json()["details"][0]
    assert detail["lower"] == {"name": "floor", "value": "5600.00"}
    assert detail["upper"] == {"name": "ceiling", "value": "5000.00"}


def test_malformed_json_is_a_400(client):
    r = client.post("/v1/price/hotel", content="{not json", headers={"Content-Type": "application/json"})
    assert (r.status_code, r.json()["error"]) == (400, "bad_request")


def test_unknown_domain_and_unknown_route(client):
    r = client.post("/v1/price/casino", json=HOTEL)
    assert (r.status_code, r.json()["error"]) == (404, "pack_not_found")
    r = client.get("/v1/nothing")
    assert (r.status_code, r.json()["error"]) == (404, "not_found")
    assert "request_id" in r.json()


def test_simulate_only_knows_the_live_version(client):
    assert client.post("/v1/simulate/hotel", json={**HOTEL, "pack_version": 1}).status_code == 200
    r = client.post("/v1/simulate/hotel", json={**HOTEL, "pack_version": 7})
    assert (r.status_code, r.json()["error"]) == (404, "pack_not_found")


def test_request_id_is_kept_when_plain_and_replaced_when_not(client):
    assert client.get("/healthz", headers={"X-Request-Id": "abc-123"}).headers["X-Request-Id"] == "abc-123"
    replaced = client.get("/healthz", headers={"X-Request-Id": "<script>alert(1)</script>"}).headers["X-Request-Id"]
    assert replaced != "<script>alert(1)</script>" and len(replaced) == 32


# ---------------------------------------------------------------- every template domain prices over HTTP

@pytest.mark.parametrize("domain, body, price", [
    ("airline", {"signals": {"seats_sold": "0.65", "days_to_departure": 3, "cabin": "business"}}, "25499.00"),
    ("ride", {"signals": {"distance_km": 10, "duration_min": 30, "demand_ratio": 2, "vehicle": "sedan"},
              "as_of": "2026-10-14T08:30:00Z"}, "323.00"),
    ("retail", {"signals": {"cost": "1000", "inventory": 600, "qty": 12}}, "1105.00"),
    ("saas", {"signals": {"seats": 25, "plan": "pro", "region": "india", "annual": True}}, "1147.00"),
    ("cloud_gpu", {"signals": {"utilization": "0.9", "gpu": "a100"}}, "5.14"),
])
def test_other_domains_price_through_the_same_endpoint(client, domain, body, price):
    r = client.post(f"/v1/price/{domain}", json={"entity": "e1", "as_of": WEDNESDAY, **body})
    assert r.status_code == 200, r.json()
    assert (r.json()["price"], r.json()["domain"]) == (price, domain)


# ---------------------------------------------------------------- batch

def test_batch_keeps_order_and_one_bad_item_does_not_fail_the_rest(client):
    items = [HOTEL, {**HOTEL, "signals": {"occupancy": 7}}, {"base_price": "3000"}, {**HOTEL, "base_price": "2000"}]
    r = client.post("/v1/price/hotel/batch", json={"items": items})
    results = r.json()["results"]
    assert r.status_code == 200 and len(results) == 4
    assert results[0]["price"] == "4950.00" and len(results[0]["price_id"]) == 26
    assert results[0]["valid_until"] == "2026-11-08T09:15:00Z"
    assert results[1]["error"] == "signal_invalid"
    assert results[2]["error"] == "bad_request" and results[2]["details"][0]["path"] == "entity"
    assert results[3]["price"] == "3300.00"                  # 2000 x 1.2 x 1.1 x 1.25


def test_batch_size_limits(client):
    assert client.post("/v1/price/hotel/batch", json={"items": []}).status_code == 400
    assert client.post("/v1/price/hotel/batch", json={"items": [HOTEL] * 501}).status_code == 400
    r = client.post("/v1/price/hotel/batch", json={"items": [HOTEL] * 500})
    assert r.status_code == 200 and len(r.json()["results"]) == 500


# ---------------------------------------------------------------- closed by default, scope, roles

def test_without_a_dev_tenant_every_v1_call_is_401():
    closed = TestClient(create_pricing_app(dev_tenant=""))
    r = closed.post("/v1/price/hotel", json=HOTEL)
    assert (r.status_code, r.json()["error"]) == (401, "unauthorized")
    assert closed.get("/healthz").status_code == 200          # ops endpoints need no key
    assert closed.get("/readyz").status_code == 503           # ... and it isn't ready: no packs


def _app_as(principal: Principal) -> TestClient:
    app = create_pricing_app(dev_tenant="demo")
    app.dependency_overrides[deps.current_principal] = lambda: principal
    return TestClient(app)


def test_key_scoped_to_one_domain_gets_404_on_the_others():
    hotel_only = _app_as(Principal("demo", "k1", "pricing", frozenset({"hotel"})))
    assert hotel_only.post("/v1/price/hotel", json=HOTEL).status_code == 200
    r = hotel_only.post("/v1/price/airline", json={"entity": "e", "signals": {}})
    assert (r.status_code, r.json()["error"]) == (404, "pack_not_found")       # not 403: don't reveal it exists


def test_another_tenant_cannot_reach_these_packs():
    other = _app_as(Principal("someone_else", "k2", "admin", frozenset({"*"})))
    r = other.post("/v1/price/hotel", json=HOTEL)
    assert (r.status_code, r.json()["error"]) == (404, "pack_not_found")


def test_role_below_the_required_one_is_403():
    app = FastAPI()
    install(app)
    app.dependency_overrides[deps.current_principal] = lambda: Principal("demo", "k", "pricing", frozenset({"*"}))

    @app.get("/editors-only")
    def editors_only(p: Principal = Depends(deps.require_principal("editor"))):   # noqa: B008
        return {"ok": True}

    r = TestClient(app).get("/editors-only")
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")


# ---------------------------------------------------------------- ops

def test_ops_endpoints(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["ready"] is True and ready.json()["self_test"] is True
    assert ready.json()["packs_loaded"] == 10 and ready.json()["pack_load_errors"] == {}
    client.post("/v1/price/hotel", json=HOTEL)
    metrics = client.get("/metrics").text
    assert 'm360_prices_total{domain="hotel",kind="price",outcome="ok"}' in metrics
    assert "m360_price_seconds_bucket" in metrics


def test_a_broken_pack_file_is_reported_and_the_rest_still_load(tmp_path):
    good = "packs/templates/hotel.yaml"
    from tests.conftest import ROOT
    (tmp_path / "hotel.yaml").write_text((ROOT / good).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "broken.yaml").write_text("domain: broken\ncurrency: INR\n", encoding="utf-8")
    c = TestClient(create_pricing_app(dev_tenant="demo", packs_dir=tmp_path))
    ready = c.get("/readyz").json()
    assert ready["packs_loaded"] == 1 and list(ready["pack_load_errors"]) == ["demo/broken"]
    assert c.post("/v1/price/hotel", json=HOTEL).status_code == 200


def test_unexpected_errors_never_leak_a_stack_trace():
    app = create_pricing_app(dev_tenant="demo")

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret internal detail")

    r = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert (r.status_code, r.json()["error"]) == (500, "internal_error")
    assert "secret" not in r.text and r.json()["request_id"]


def test_control_app_starts_with_ops_endpoints_only():
    c = TestClient(create_control_app(dev_tenant="demo"))
    assert c.get("/healthz").status_code == 200
    assert c.post("/v1/price/hotel", json=HOTEL).status_code == 404


def test_console_is_on_the_control_app_only_and_no_script_runs_inline():
    control = TestClient(create_control_app(dev_tenant="demo"))
    page = control.get("/admin/")
    assert page.status_code == 200 and "default-src 'self'" in page.headers["content-security-policy"]
    # the policy forbids inline code, so the page must not rely on any
    assert "<style" not in page.text and "style=" not in page.text and "onclick" not in page.text
    assert control.get("/v1/me").json() == {"tenant_id": "demo", "key_id": "dev", "role": "admin", "domains": ["*"]}
    assert TestClient(create_pricing_app(dev_tenant="demo")).get("/admin/").status_code == 404


def test_every_answer_says_how_long_the_server_took_and_the_scoreboard_adds_it_up_by_route(client):
    r = client.post("/v1/simulate/hotel", json=HOTEL)
    assert r.status_code == 200
    name, _, value = r.headers["server-timing"].partition(";dur=")
    assert name == "app" and 0 < float(value) < 5000
    assert "server-timing" in client.get("/v1/price/nope").headers          # refusals are timed too

    rows = client.get("/v1/scoreboard").json()["live"]["http"]
    row = next(x for x in rows if x["route"] == "/v1/simulate/{domain}" and x["method"] == "POST")
    assert row["calls"] >= 1 and row["mean_ms"] > 0 and row["p95_ms_at_most"] >= row["p50_ms_at_most"]
    assert not any("hotel" in x["route"] for x in rows)                      # templates only, never a domain name


def test_console_never_builds_html_from_text():
    """Everything the server sends is put on the page as text nodes. innerHTML would turn a pack's `reason`
    or a key's name into markup."""
    from monetize360.api.routes.console import FILES
    scripts = list((FILES / "js").glob("*.js"))
    assert scripts
    for script in scripts:
        code = "\n".join(line.split("//")[0] for line in script.read_text(encoding="utf-8").splitlines())
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            assert banned not in code, f"{script.name} uses {banned}"


def test_swagger_prefilled_examples_work_as_is(client):
    """'Try it out' then Execute must succeed without editing anything (domain is pre-filled with hotel)."""
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    assert client.post("/v1/price/hotel", json=schemas["PriceRequest"]["examples"][0]).json()["price"] == "4950.00"
    assert client.post("/v1/simulate/hotel", json=schemas["SimulateRequest"]["examples"][0]).status_code == 200
    batch = client.post("/v1/price/hotel/batch", json=schemas["BatchRequest"]["examples"][0]).json()["results"]
    assert [r["price"] for r in batch] == ["4950.00", "3300.00"]
    for path in ("/v1/price/{domain}", "/v1/price/{domain}/batch", "/v1/simulate/{domain}"):
        assert spec["paths"][path]["post"]["parameters"][0]["schema"]["example"] == "hotel"


def test_scoreboard_counts_capabilities_from_the_schema_and_only_this_replicas_work(client):
    before = client.get("/v1/scoreboard").json()["live"]["prices"]["ok"]
    client.post("/v1/price/hotel", json=HOTEL)
    client.post("/v1/price/hotel", json={**HOTEL, "signals": {"occupancy": 7}})
    r = client.get("/v1/scoreboard")
    board = r.json()
    assert r.status_code == 200 and set(board) == {"capabilities", "live", "measured"}

    caps = board["capabilities"]
    assert caps["operator_count"] == len(caps["operators"]) >= 11
    assert {"pct", "lookup_table", "piecewise_curve", "formula", "cost_plus"} <= set(caps["operators"])
    assert caps["stacking_policies"] == ["multiply", "add", "max", "min", "first_match", "exclusive"]
    assert caps["guard_layers"] == ["pack", "tenant", "platform"]

    live = board["live"]
    assert live["prices"]["ok"] == before + 1 and live["prices"]["rejected"] >= 1
    assert live["live_packs"] == 10 and live["self_test"] is True
    assert (live["database"], live["redis"]) == ("not_configured", "not_configured")
    assert live["engine_ms_at_most"]["p95"] is not None
    assert "demo" not in r.text and "deluxe" not in r.text          # no tenant, no entity


def test_scoreboard_measured_block_is_absent_until_a_results_file_exists(client, tmp_path, monkeypatch):
    from monetize360 import config
    from monetize360.api.routes import scoreboard

    missing = config.settings().__class__(**{**config.settings().__dict__, "measured_path": tmp_path / "none.json"})
    monkeypatch.setattr(scoreboard, "settings", lambda: missing)
    assert client.get("/v1/scoreboard").json()["measured"] is None                  # "not measured", not zeros

    (tmp_path / "m.json").write_text('{"load_test": {"req_per_s": 123}}', encoding="utf-8")
    found = config.settings().__class__(**{**config.settings().__dict__, "measured_path": tmp_path / "m.json"})
    monkeypatch.setattr(scoreboard, "settings", lambda: found)
    assert client.get("/v1/scoreboard").json()["measured"] == {"load_test": {"req_per_s": 123}}
