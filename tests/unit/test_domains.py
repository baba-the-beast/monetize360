"""Domain-review gaps D1-D7: features real industries need, each written as a pack that must pass the JSON Schema
AND compile, then price exactly. Arithmetic is hand-checked in the comments."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from monetize360.control.schema import validate
from monetize360.engine import PackInvalid, RuleError, SignalInvalid, compile_pack, evaluate
from tests.conftest import ctx, make_pack


def build(pack: dict):
    assert validate(pack) == [], validate(pack)       # the schema accepts it ...
    return compile_pack(pack)                          # ... and so does the engine


def rule(id_, when, then, reason="r", **extra):
    return {"id": id_, "when": when, "then": then, "reason": reason, **extra}


def group(*rules, stacking="multiply", name="g"):
    return {"name": name, "stacking": stacking, "rules": list(rules)}


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# ================================================================== D1 calendar is pack data, + month/day/minute

FESTIVAL = {"festival": {"type": "enum", "values": ["none", "christmas", "eid"], "source": "public"}}
CALENDAR = {"festival": {"2026-12-25": "christmas", "2026-03-21": "eid"}}


def test_any_festival_from_the_pack_calendar():
    fest = rule("fest", "festival != 'none'", {"op": "pct", "value": 20}, "Festival: {festival}")
    pack = build(make_pack([group(fest)], signals=FESTIVAL, calendar=CALENDAR))
    xmas = evaluate(pack, ctx(as_of=utc(2026, 12, 25, 9), override=False))          # 14:30 in Kolkata
    assert xmas.price == Decimal("1200.00") and xmas.waterfall[-1].reason == "Festival: christmas"
    assert evaluate(pack, ctx(as_of=utc(2026, 12, 24, 9), override=False)).price == Decimal("1000.00")


def test_calendar_day_follows_pack_timezone():
    pack = build(make_pack([group(rule("fest", "festival == 'christmas'", {"op": "pct", "value": 20}))],
                           signals=FESTIVAL, calendar=CALENDAR))
    # 2026-12-24 19:00 UTC is already 25 Dec 00:30 in Kolkata
    assert evaluate(pack, ctx(as_of=utc(2026, 12, 24, 19), override=False)).price == Decimal("1200.00")


def test_custom_calendar_signal():
    signals = {"sale_day": {"type": "enum", "values": ["none", "big_billion"], "source": "public"}}
    pack = build(make_pack([group(rule("sale", "sale_day == 'big_billion'", {"op": "pct", "value": -30}))],
                           signals=signals, calendar={"sale_day": {"2026-10-14": "big_billion"}}))
    assert evaluate(pack, ctx(override=False)).price == Decimal("700.00")             # ctx default: 2026-10-14


@pytest.mark.parametrize("signals, calendar, why", [
    (FESTIVAL, {"festival": {"2026-12-25": "christmas"}}, "festival can never be 'eid' — no date in the calendar"),
    (FESTIVAL, None, "festival can never be 'christmas' — no date in the built-in calendar"),
    (FESTIVAL, {"festival": {"2026-12-25": "christmas", "2026-03-21": "eid", "2026-11-08": "diwali"}},
     "'diwali' is not one of festival's values"),
    ({"x": {"type": "number", "source": "request"}}, {"sale_day": {"2026-10-14": "big"}},
     "needs a public enum signal"),
    ({"festival": {"type": "enum", "values": ["christmas", "eid"], "source": "public"}}, CALENDAR,
     "must include 'none'"),
])
def test_calendar_mistakes_are_compile_errors(signals, calendar, why):
    pack = make_pack([group(rule("r", "true", {"op": "pct", "value": 1}))], signals=signals)
    if calendar is not None:
        pack["calendar"] = calendar
    with pytest.raises(PackInvalid) as e:
        compile_pack(pack)
    assert any(why in d["message"] for d in e.value.details), e.value.details


def test_happy_hour_with_minute():
    signals = {"hour": {"type": "int", "source": "public"}, "minute": {"type": "int", "source": "public"}}
    when = "(hour == 17 && minute >= 30) || hour == 18"                              # 17:30-19:00 local
    pack = build(make_pack([group(rule("happy", when, {"op": "pct", "value": -10}))], signals=signals))
    assert evaluate(pack, ctx(as_of=utc(2026, 10, 14, 12, 15), override=False)).price == Decimal("900.00")   # 17:45
    assert evaluate(pack, ctx(as_of=utc(2026, 10, 14, 11, 45), override=False)).price == Decimal("1000.00")  # 17:15


def test_season_with_month_and_day():
    signals = {"month": {"type": "int", "source": "public"}, "day": {"type": "int", "source": "public"}}
    pack = build(make_pack([group(rule("peak", "month == 12 && day >= 20", {"op": "pct", "value": 25}))],
                           signals=signals))
    assert evaluate(pack, ctx(as_of=utc(2026, 12, 22, 6), override=False)).price == Decimal("1250.00")
    assert evaluate(pack, ctx(as_of=utc(2026, 12, 2, 6), override=False)).price == Decimal("1000.00")


# ================================================================== D2 nothing below 0.01 is silently destroyed

def test_sub_cent_base_is_an_error_not_a_floor_clamp():
    pack = build(make_pack([group(rule("r", "true", {"op": "pct", "value": 1}))]))
    with pytest.raises(SignalInvalid, match="below the smallest unit 0.01"):
        evaluate(pack, ctx("0.00102"))


@pytest.mark.parametrize("then, guards_floor, path", [
    ({"op": "abs", "value": 0.005}, 1, "groups/0/rules/0/then/value"),
    ({"op": "set", "value": 0.004}, 1, "groups/0/rules/0/then/value"),
    ({"op": "pct", "value": 1}, 0.001, "guards/floor/value"),
])
def test_money_in_a_pack_has_at_most_two_decimals(then, guards_floor, path):
    pack = make_pack([group(rule("r", "true", then))])
    pack["guards"]["floor"]["value"] = guards_floor
    with pytest.raises(PackInvalid) as e:
        compile_pack(pack)
    assert [d["path"] for d in e.value.details] == [path]


# ================================================================== D3 calculated base price

RIDE = {"distance_km": {"type": "number", "source": "request", "range": [0, 500]},
        "surge": {"type": "bool", "source": "request", "default": False}}


def test_formula_base_and_pct_of_base_guards_measure_the_real_fare():
    pack = build(make_pack(
        [group(rule("surge", "surge", {"op": "pct", "value": 20}, "Surge"))], signals=RIDE,
        base={"op": "formula", "expr": "50 + distance_km * 12"},
        guards={"floor": {"op": "pct_of_base", "value": 80}, "ceiling": {"op": "pct_of_base", "value": 300}}))
    r = evaluate(pack, ctx(None, distance_km="13.75", surge=True))
    # base 50 + 13.75 x 12 = 215.00 ; +20% = 43.00 -> 258.00 ; ceiling 300% of 215 = 645 -> no clamp
    assert [(x.step, str(x.delta)) for x in r.waterfall] == [("base", "215.00"), ("surge", "43.00")]
    assert r.waterfall[0].reason == "Base price: 50 + distance_km * 12"
    assert r.price == Decimal("258.00") and r.bounds["upper"] == Decimal("645.00")


def test_formula_base_must_be_positive():
    pack = build(make_pack([group(rule("r", "true", {"op": "pct", "value": 1}))], signals=RIDE,
                           base={"op": "formula", "expr": "distance_km - 10"}))
    with pytest.raises(RuleError, match="a base price must be greater than 0"):
        evaluate(pack, ctx(None, distance_km="5"))


def test_formula_base_cannot_use_price():
    with pytest.raises(PackInvalid, match="pack is invalid"):
        compile_pack(make_pack([group(rule("r", "true", {"op": "pct", "value": 1}))], signals=RIDE,
                               base={"op": "formula", "expr": "price + distance_km"}))


# ================================================================== D4 rules see price/base; min, max, abs

RETAIL = {"competitor": {"type": "money", "source": "request"}, "cost": {"type": "money", "source": "request"}}


@pytest.mark.parametrize("competitor, expected", [
    ("900", "900.00"),      # cheaper competitor: match it, max(900, 800 x 1.05 = 840) = 900
    ("700", "840.00"),      # much cheaper: never below cost + 5% -> 840
    ("1100", "1000.00"),    # dearer competitor: rule doesn't fire
])
def test_match_competitor_but_never_below_cost_plus_5(competitor, expected):
    pack = build(make_pack([group(rule("match", "competitor < price",
                                       {"op": "formula", "expr": "max(competitor, cost * 1.05)"}, "Match competitor"))],
                           signals=RETAIL))
    assert evaluate(pack, ctx(competitor=competitor, cost="800")).price == Decimal(expected)


def test_when_sees_price_at_group_start_and_base():
    pack = build(make_pack([
        group(rule("double", "true", {"op": "pct", "value": 100}),
              rule("not-yet", "price > 1500", {"op": "pct", "value": 10}), name="first"),   # G = 1000: no fire
        group(rule("cap", "price > base * 1.5", {"op": "pct", "value": -10}), name="second"),  # G = 2000 > 1500
    ]))
    r = evaluate(pack, ctx())
    # 1000 -> +1000 = 2000 ; second group: 2000 > 1500 -> -200 = 1800
    assert [x.step for x in r.waterfall] == ["base", "double", "cap"] and r.price == Decimal("1800.00")


def test_abs_and_min_in_formulas():
    pack = build(make_pack([group(rule("r", "true", {"op": "formula", "expr": "min(price, base + abs(cost - 900))"}))],
                           signals=RETAIL))
    assert evaluate(pack, ctx(competitor="1", cost="800")).price == Decimal("1000.00")   # min(1000, 1000+100)
    assert evaluate(pack, ctx("2000", competitor="1", cost="850")).price == Decimal("2000.00")  # min(2000, 2050)


# ================================================================== D6 "best discount wins"

@pytest.mark.parametrize("stacking, expected", [("min", "800.00"), ("max", "950.00")])
def test_min_stacking_gives_the_best_discount(stacking, expected):
    pack = build(make_pack([group(rule("small", "true", {"op": "pct", "value": -5}),
                                  rule("big", "true", {"op": "pct", "value": -20}), stacking=stacking)]))
    assert evaluate(pack, ctx()).price == Decimal(expected)


# ================================================================== D5 floor / ceiling from a signal (MRP, cost)

def test_ceiling_is_the_items_mrp_and_floor_is_cost_plus_5():
    signals = {"mrp": {"type": "money", "source": "request"}, "cost": {"type": "money", "source": "request"},
               "x": {"type": "number", "source": "request"}}
    pack = build(make_pack([group(rule("r", "true", {"op": "formula", "expr": "price * x"}))], signals=signals,
                           guards={"floor": {"op": "signal", "signal": "cost", "pct": 105},
                                   "ceiling": {"op": "signal", "signal": "mrp"}}))
    up = evaluate(pack, ctx(mrp="1299", cost="800", x="1.5"))               # 1500 > MRP 1299
    assert (up.waterfall[-1].step, up.waterfall[-1].reason, up.price) == ("ceiling", "Ceiling = mrp 1299.00",
                                                                          Decimal("1299.00"))
    down = evaluate(pack, ctx(mrp="1299", cost="800", x="0.5"))             # 500 < 105% of 800 = 840
    assert (down.waterfall[-1].reason, down.price) == ("Floor 105% of cost = 840.00", Decimal("840.00"))


def test_limit_signal_must_be_numeric():
    signals = {"code": {"type": "text", "source": "request"}}
    with pytest.raises(PackInvalid):
        compile_pack(make_pack([group(rule("r", "true", {"op": "pct", "value": 1}))], signals=signals,
                               guards={"floor": {"op": "abs", "value": 1},
                                       "ceiling": {"op": "signal", "signal": "code"}}))


# ================================================================== D7 real-world codes: text type, wider enums

ROUTE = {"route": {"type": "text", "source": "request"}}


@pytest.mark.parametrize("route, expected", [("DEL-BOM", "1200.00"), ("BLR-HYD", "1000.00")])
def test_text_signal_lookup_with_default(route, expected):
    then = {"op": "lookup_table", "signal": "route", "table": {"DEL-BOM": 1.2, "BOM-GOI": 1.1}, "default": 1.0}
    pack = build(make_pack([group(rule("route", "true", then, "Route {route}"))], signals=ROUTE))
    r = evaluate(pack, ctx(route=route))
    assert r.price == Decimal(expected) and r.waterfall[-1].reason == f"Route {route}"


def test_text_table_needs_a_default():
    then = {"op": "lookup_table", "signal": "route", "table": {"DEL-BOM": 1.2}}
    with pytest.raises(PackInvalid, match="pack is invalid"):
        compile_pack(make_pack([group(rule("route", "true", then))], signals=ROUTE))


def test_text_in_conditions_and_its_limits():
    metro = rule("metro", "route in ['DEL-BOM', 'BOM-DEL']", {"op": "pct", "value": 10})
    pack = build(make_pack([group(metro)], signals=ROUTE))
    assert evaluate(pack, ctx(route="BOM-DEL")).price == Decimal("1100.00")
    with pytest.raises(SignalInvalid, match="1 to 64 characters"):
        evaluate(pack, ctx(route="X" * 65))


def test_enum_values_can_be_real_codes():
    signals = {"room": {"type": "enum", "values": ["2BHK", "A100X", "DEL-BOM", "zone:12"], "source": "request"}}
    pack = build(make_pack([group(rule("big", "room == '2BHK'", {"op": "pct", "value": 50}))], signals=signals))
    assert evaluate(pack, ctx(room="2BHK")).price == Decimal("1500.00")


def test_enums_can_have_more_than_100_values():
    signals = {"zone": {"type": "enum", "values": [f"Z{i}" for i in range(260)], "source": "request"}}
    pack = build(make_pack([group(rule("z", "zone == 'Z259'", {"op": "pct", "value": 5}))], signals=signals))
    assert evaluate(pack, ctx(zone="Z259")).price == Decimal("1050.00")


# ================================================================== /code-review findings on this change

def test_text_table_keys_mangled_by_yaml_are_rejected():
    import yaml
    table = yaml.safe_load("{NO: 1.2, 0110: 1.1, SE: 1.0}")          # YAML: {False: 1.2, 72: 1.1, 'SE': 1.0}
    then = {"op": "lookup_table", "signal": "route", "table": table, "default": 1.0}
    with pytest.raises(PackInvalid) as e:
        compile_pack(make_pack([group(rule("r", "true", then))], signals=ROUTE))
    assert len(e.value.details) == 2 and all("quote every key" in d["message"] for d in e.value.details)
    quoted = yaml.safe_load('{"NO": 1.2, "0110": 1.1, SE: 1.0}')
    pack = compile_pack(make_pack([group(rule("r", "true", {**then, "table": quoted}))], signals=ROUTE))
    assert evaluate(pack, ctx(route="0110")).price == Decimal("1100.00")


def test_huge_pack_amount_is_a_clear_error_and_keeps_the_others():
    pack = make_pack([group(rule("r", "true", {"op": "abs", "value": 1e30}),
                            rule("bad", "nope > 1", {"op": "pct", "value": 1}))])
    with pytest.raises(PackInvalid) as e:
        compile_pack(pack)
    messages = [d["message"] for d in e.value.details]
    assert any("larger than the limit" in m for m in messages)       # not a generic "(unknown)" error ...
    assert any("unknown name 'nope'" in m for m in messages)         # ... and the other error isn't lost


def test_floor_above_ceiling_on_the_same_signal():
    signals = {"cost": {"type": "money", "source": "request"}}
    with pytest.raises(PackInvalid, match="pack is invalid") as e:
        compile_pack(make_pack([group(rule("r", "true", {"op": "pct", "value": 1}))], signals=signals,
                               guards={"floor": {"op": "signal", "signal": "cost", "pct": 120},
                                       "ceiling": {"op": "signal", "signal": "cost"}}))
    assert "floor 120% of cost is above ceiling 100% of it" in e.value.details[0]["message"]


def test_base_lookup_values_are_money():
    signals = {"vehicle": {"type": "enum", "values": ["bike", "scooter"], "source": "request"}}
    base = {"op": "lookup_table", "signal": "vehicle", "table": {"bike": 20.005, "scooter": 0.004}}
    with pytest.raises(PackInvalid) as e:
        compile_pack(make_pack([group(rule("r", "true", {"op": "pct", "value": 1}))], signals=signals, base=base))
    assert sorted(d["path"] for d in e.value.details) == ["base/table/bike", "base/table/scooter"]


def test_formula_base_has_the_same_upper_limit():
    pack = build(make_pack([group(rule("r", "true", {"op": "pct", "value": 1}))], signals=RIDE,
                           base={"op": "formula", "expr": "distance_km * 1000000000000"}))
    with pytest.raises(RuleError, match="above the limit"):
        evaluate(pack, ctx(None, distance_km="2"))


def test_zero_or_sub_cent_base_from_any_op_is_reported_where_it_comes_from():
    sub_cent = build(make_pack([group(rule("r", "true", {"op": "pct", "value": 1}))], signals=RIDE,
                               base={"op": "formula", "expr": "distance_km * 0.001"}))
    with pytest.raises(RuleError, match="below the smallest unit") as e:          # pack formula, not base_price
        evaluate(sub_cent, ctx(None, distance_km="2"))
    assert e.value.details == [{"path": "base"}]
    comp = {"comp": {"type": "money", "source": "request"}}
    zero = build(make_pack([group(rule("r", "true", {"op": "pct", "value": 1}))], signals=comp,
                           base={"op": "competitor_rel", "competitor_signal": "comp", "pct": 0}))
    with pytest.raises(SignalInvalid, match="must be greater than 0") as e:      # used to clamp to the floor
        evaluate(zero, ctx(None, comp="0"))
    assert e.value.details == [{"path": "signals.comp"}]
