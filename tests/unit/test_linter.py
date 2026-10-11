"""Linter (control/linter.py): one test per check in ARCHITECTURE §9 / PACK_SCHEMA "Checked by the linter";
every template lints clean."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from monetize360.control.linter import lint
from monetize360.engine import Policy
from tests.conftest import load_pack, make_pack

TEMPLATES = sorted(p.name for p in (Path(__file__).resolve().parents[2] / "packs/templates").glob("*.yaml"))


def codes(result, kind="errors"):
    return [f["code"] for f in result[kind]]


def rule(id_, when, then=None, **extra):
    return {"id": id_, "when": when, "then": then or {"op": "pct", "value": 1}, "reason": "r", **extra}


def one_group(*rules, stacking="multiply", signals=None, **overrides):
    return make_pack([{"name": "g", "stacking": stacking, "rules": list(rules)}], signals=signals, **overrides)


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_template_lints_clean(name):
    result = lint(load_pack(name))
    assert result["ok"] and result["errors"] == []


# ---------------------------------------------------------------- gate 1 and compile errors come through

def test_schema_errors_stop_the_lint():
    pack = load_pack("hotel.yaml")
    pack["guards"]["ceilng"] = pack["guards"].pop("ceiling")
    result = lint(pack)
    assert not result["ok"] and set(codes(result)) == {"schema"}


def test_compile_errors_come_through():
    result = lint(one_group(rule("r", "festival > 3"), signals={
        "festival": {"type": "enum", "values": ["none", "diwali"], "source": "public"}}))
    assert codes(result) == ["compile"] and "can't order" in result["errors"][0]["message"]


# ---------------------------------------------------------------- fairness: the jaw-drop check

@pytest.mark.parametrize("signal, code", [
    ("religion", "fairness"),
    ("customer_caste", "fairness"),
    ("gender", "fairness"),
    ("age", "fairness"),
    ("email", "personal_data"),
    ("customer_id", "personal_data"),
    ("ip_address", "personal_data"),
])
def test_protected_and_personal_signals_block_publish(signal, code):
    pack = one_group(rule("r", f"{signal} == 'x'"), signals={signal: {"type": "text", "source": "request"}})
    result = lint(pack)
    assert not result["ok"] and code in codes(result)
    if code == "fairness":
        assert result["errors"][0]["message"].startswith(f"Fairness violation: '{signal}' is a protected attribute")


@pytest.mark.parametrize("signal", ["package_size", "voltage", "stage", "sexton_fee"])
def test_innocent_names_containing_those_letters_pass(signal):
    pack = one_group(rule("r", f"{signal} > 1"), signals={signal: {"type": "number", "source": "request"}})
    assert lint(pack)["ok"]


def test_tenant_banned_signal():
    pack = one_group(rule("r", "loyalty_tier > 1"), signals={"loyalty_tier": {"type": "int", "source": "request"}})
    assert codes(lint(pack, banned_signals=["loyalty_tier"])) == ["banned_signal"]


def test_proxy_attribute_is_a_warning():
    pack = one_group(rule("r", "pincode == '110001'"), signals={"pincode": {"type": "text", "source": "request"}})
    result = lint(pack)
    assert result["ok"] and "fairness_proxy" in codes(result, "warnings")


# ---------------------------------------------------------------- tenant limits (L2 vs L1)

@pytest.mark.parametrize("policy, path", [
    (Policy(max_price=Decimal("500000")), "guards/ceiling"),    # make_pack ceiling is 1,000,000
    (Policy(min_price=Decimal("5")), "guards/floor"),           # make_pack floor is 1
])
def test_pack_limits_must_sit_inside_tenant_limits(policy, path):
    result = lint(one_group(rule("r", "true")), policy=policy)
    assert codes(result) == ["tenant_limit"] and result["errors"][0]["path"] == path


def test_surge_cap_below_floor_means_no_price_is_ever_valid():
    result = lint(load_pack("hotel.yaml"), policy=Policy(max_surge_x=Decimal("0.5")))   # floor is 70% of base
    assert codes(result) == ["tenant_limit"]


# ---------------------------------------------------------------- rules that can't do anything

def test_always_false_rule():
    assert "unreachable" in codes(lint(one_group(rule("never", "1 > 2"))), "warnings")


def test_shadowed_rule_in_first_match():
    result = lint(one_group(rule("always", "true"), rule("later", "x > 1"), stacking="first_match"))
    assert "shadowed" in codes(result, "warnings")


def test_always_firing_rule_in_exclusive_group_is_an_error():
    result = lint(one_group(rule("always", "true"), rule("other", "x > 1"), stacking="exclusive"))
    assert codes(result) == ["exclusive_overlap"]


def test_expired_rule():
    pack = one_group(rule("promo", "true", effective_to="2026-01-01T00:00:00Z"))
    result = lint(pack, as_of=datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert "expired" in codes(result, "warnings")


# ---------------------------------------------------------------- inputs left unpriced

def test_missing_default():
    pack = one_group(rule("r", "x > 1"), signals={"x": {"type": "number", "source": "request"}})
    assert "missing_default" in codes(lint(pack), "warnings")


def test_highest_base_price_the_pack_can_price():
    warning = next(w for w in lint(load_pack("hotel.yaml"))["warnings"] if w["code"] == "max_base_price")
    assert "above 7142.86" in warning["message"]                 # 5000 / 0.70


def test_band_gap():
    pack = load_pack("event_tickets.yaml")
    pack["groups"][0]["rules"][0]["then"]["bands"] = pack["groups"][0]["rules"][0]["then"]["bands"][:2]
    warning = next(w for w in lint(pack)["warnings"] if w["code"] == "band_gap")
    assert "can reach 1 but the last band stops at 0.8" in warning["message"]


def test_lookup_gap():
    signals = {"cabin": {"type": "enum", "values": ["eco", "biz", "first"], "source": "request"}}
    then = {"op": "lookup_table", "signal": "cabin", "table": {"eco": 1.0}}
    warning = next(w for w in lint(one_group(rule("r", "true", then), signals=signals))["warnings"]
                   if w["code"] == "lookup_gap")
    assert "2 value(s): biz, first" in warning["message"]


def test_unused_signal():
    signals = {"x": {"type": "number", "source": "request", "default": 0},
               "forgotten": {"type": "bool", "source": "request", "default": False}}
    result = lint(one_group(rule("r", "x > 1"), signals=signals))
    assert [w["path"] for w in result["warnings"] if w["code"] == "unused_signal"] == ["signals/forgotten"]


# ---------------------------------------------------------------- review L1, L2, C1, S5

@pytest.mark.parametrize("signal", ["vehicle_age", "stock_age_days", "battery_health", "race_day", "mobile_app",
                                    "phone_model", "shipping_address_zone"])
def test_innocent_uses_of_ambiguous_words_pass(signal):
    pack = one_group(rule("r", f"{signal} > 1"), signals={signal: {"type": "number", "source": "request"}})
    assert lint(pack)["ok"], lint(pack)["errors"]


@pytest.mark.parametrize("signal, code", [("customer_age", "fairness"), ("driver_age", "fairness"),
                                          ("passenger_sex", "fairness"), ("user_health", "fairness"),
                                          ("religion_group", "fairness"), ("customer_phone", "personal_data"),
                                          ("phone_number", "personal_data")])
def test_ambiguous_words_about_a_person_still_block(signal, code):
    pack = one_group(rule("r", f"{signal} > 1"), signals={signal: {"type": "number", "source": "request"}})
    assert codes(lint(pack)) == [code]


def test_protected_values_under_a_neutral_name():
    signals = {"segment": {"type": "enum", "values": ["male", "female"], "source": "request"}}
    result = lint(one_group(rule("r", "segment == 'female'"), signals=signals))
    assert codes(result) == ["fairness"] and "takes protected values (female, male)" in result["errors"][0]["message"]


def test_protected_values_as_table_keys():
    signals = {"group": {"type": "text", "source": "request"}}
    then = {"op": "lookup_table", "signal": "group", "table": {"hindu": 1.1, "other": 1.0}, "default": 1.0}
    assert codes(lint(one_group(rule("r", "true", then), signals=signals))) == ["fairness"]


def test_tenant_exception_turns_the_error_into_a_signed_off_warning():
    pack = one_group(rule("r", "customer_age > 60"), signals={"customer_age": {"type": "int", "source": "request"}})
    result = lint(pack, fairness_exceptions=["customer_age"])     # e.g. a legal senior-citizen discount
    assert result["ok"] and "fairness_exception" in codes(result, "warnings")


def test_calendar_that_runs_out_is_flagged():
    result = lint(load_pack("hotel.yaml"), as_of=datetime(2026, 10, 11, tzinfo=timezone.utc))
    warning = next(w for w in result["warnings"] if w["code"] == "calendar_ends")
    assert "no dates after 2027-10-29" in warning["message"]


@pytest.mark.parametrize("policy, path", [(Policy(min_price=Decimal("2000000")), "guards/ceiling"),
                                          (Policy(max_price=Decimal("0.5")), "guards/floor")])
def test_tenant_limits_that_leave_no_valid_price(policy, path):
    result = lint(one_group(rule("r", "true")), policy=policy)
    assert "tenant_limit" in codes(result) and path in [e["path"] for e in result["errors"]]
