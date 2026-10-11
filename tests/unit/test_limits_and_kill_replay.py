"""Tenant limits and kill switches without a database: the in-memory view, and replaying records that carry them."""
from datetime import datetime, timezone
from decimal import Decimal

from monetize360.control.replay import output_of, policy_json, policy_of, replay
from monetize360.engine import Context, Policy, compile_pack, evaluate
from monetize360.store.audit import plain
from monetize360.store.controls import Controls
from tests.conftest import load_pack

AS_OF = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)          # a Wednesday, no festival


def test_controls_start_empty_and_are_replaced_as_a_whole():
    c = Controls()
    assert c.policy("t1") is None and c.kill_mode("t1", "hotel") == "normal"

    c.apply([{"tenant_id": "t1", "min_price": None, "max_price": Decimal("6000.00"), "max_surge_x": Decimal("2.00")},
             {"tenant_id": "t2", "min_price": None, "max_price": None, "max_surge_x": None}],
            [{"tenant_id": "t1", "domain": "hotel", "mode": "freeze"},
             {"tenant_id": "t1", "domain": "ride", "mode": "explode"}])          # an unknown mode is ignored
    assert c.policy("t1") == Policy(max_price=Decimal("6000.00"), max_surge_x=Decimal("2.00"))
    assert c.policy("t2") is None                                                # a row with no limits = no policy
    assert c.kill_mode("t1", "hotel") == "freeze" and c.kill_mode("t1", "ride") == "normal"
    assert c.kill_mode("t2", "hotel") == "normal"                                # another tenant's switch isn't ours

    c.apply([], [])
    assert c.policy("t1") is None and c.kill_mode("t1", "hotel") == "normal"     # cleared in the database = cleared


def test_policy_round_trips_through_an_audit_record_as_text():
    p = Policy(min_price=Decimal("100"), max_surge_x=Decimal("1.10"))
    stored = policy_json(p)
    assert stored == {"min_price": "100", "max_surge_x": "1.10"}
    assert policy_of({"inputs": {"policy": stored}}) == p
    assert policy_json(None) is None and policy_json(Policy()) is None
    assert policy_of({"inputs": {"policy": None}}) is None and policy_of({"inputs": {}}) is None


def _record(pack, policy=None, kill=None, occupancy="0.9"):
    compiled = compile_pack(pack)
    ctx = Context(as_of=AS_OF, base_price=Decimal("3000"), signals={"occupancy": occupancy})
    result = evaluate(compiled, ctx, policy, compiled.rule_ids if kill == "base_only" else frozenset())
    inputs = {"base_price": "3000", "signals": {"occupancy": occupancy}, "redis_signals": {},
              "allow_public_override": False, "policy": policy_json(policy)}
    if kill:
        inputs["kill_mode"] = kill
    return {"as_of": AS_OF, "reference_price": None, "output": output_of(result), "inputs": plain(inputs)}


def test_replay_uses_the_limits_that_were_recorded():
    pack = load_pack("hotel.yaml")
    record = _record(pack, Policy(max_surge_x=Decimal("1.1")))
    assert record["output"]["price"] == "3300.00"                                # capped at 1.1 x base
    out = replay(record, pack)
    assert out["identical"] is True and out["replayed"]["price"] == "3300.00"

    record["inputs"]["policy"] = None                                            # someone strips the limits
    out = replay(record, pack)
    assert (out["identical"], out["replayed"]["price"]) == (False, "3600.00")    # ... and replay shows it


def test_replay_of_a_base_only_price_keeps_the_rules_off():
    pack = load_pack("hotel.yaml")
    record = _record(pack, kill="base_only")
    assert record["output"]["price"] == "3000.00"
    assert replay(record, pack)["identical"] is True


def test_replay_of_a_frozen_price_says_nothing_was_calculated():
    pack = load_pack("hotel.yaml")
    record = _record(pack)
    record["inputs"].update(kill_mode="freeze", frozen_from="01SOURCE")
    out = replay(record, pack)
    assert out["identical"] is None and out["replayed"] is None and out["frozen_from"] == "01SOURCE"
