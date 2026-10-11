"""The audit hash chain and replay, without a database: what is hashed, that a jsonb round trip can't change it,
and that replay compares the right things."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from monetize360.control.replay import context_of, output_of, replay
from monetize360.engine import Context, compile_pack, evaluate
from monetize360.store.audit import GENESIS, AuditEvent, _event, _row, canonical, chain_hash, plain
from tests.conftest import load_pack

AS_OF = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)


def event(**over) -> AuditEvent:
    base = dict(price_id="01TEST", tenant_id="hotel_001", domain="hotel", pack_version=1, entity="deluxe-101",
                key_id="K1", as_of=AS_OF,
                inputs={"base_price": Decimal("3000"), "signals": {"occupancy": 0.85, "n": 3, "flag": True},
                        "redis_signals": {}, "allow_public_override": False, "policy": None},
                signal_sources={"occupancy": {"value": Decimal("0.85"), "source": "request"}},
                reference_price=None, output={"price": "4950.00", "flags": []})
    return AuditEvent(**{**base, **over})


def test_plain_keeps_types_that_survive_jsonb_and_turns_the_rest_into_text():
    assert plain({"a": Decimal("0.10"), "b": 0.85, "c": 3, "d": True, "e": None, "f": "x", "g": [Decimal("1.50")]}) \
        == {"a": "0.10", "b": "0.85", "c": 3, "d": True, "e": None, "f": "x", "g": ["1.50"]}
    assert plain(AS_OF) == "2026-11-08T09:00:00+00:00"


def test_hash_survives_a_json_round_trip_like_the_one_jsonb_does():
    row = _row(event(reference_price=Decimal("4500")), seq=7)
    stored = {**row, "inputs": json.loads(json.dumps(row["inputs"])),
              "signal_sources": json.loads(json.dumps(row["signal_sources"])),
              "output": json.loads(json.dumps(row["output"])),
              "reference_price": Decimal("4500.00"),                              # numeric(14,2) adds the zeros
              "as_of": AS_OF.astimezone(timezone(timedelta(hours=5, minutes=30)))}   # server shows another zone
    assert canonical(stored) == canonical(row)
    assert chain_hash(GENESIS, stored) == chain_hash(GENESIS, row)


def test_key_order_does_not_matter_but_every_value_does():
    a = _row(event(), 1)
    b = {**a, "inputs": dict(reversed(list(a["inputs"].items())))}
    assert canonical(a) == canonical(b)
    for change in ({"entity": "deluxe-102"}, {"pack_version": 2}, {"key_id": "K2"}, {"price_id": "01OTHER"},
                   {"as_of": AS_OF + timedelta(microseconds=1)}, {"output": {"price": "4950.01", "flags": []}},
                   {"reference_price": Decimal("4500")}):
        assert chain_hash(GENESIS, _row(event(**change), 1)) != chain_hash(GENESIS, a), change
    assert chain_hash(GENESIS, _row(event(), 2)) != chain_hash(GENESIS, a)            # position
    assert chain_hash(b"\x01" * 32, a) != chain_hash(GENESIS, a)                        # what came before


def test_reference_price_is_two_decimals_half_up_before_hashing():
    assert _row(event(reference_price=Decimal("4500.005")), 1)["reference_price"] == Decimal("4500.01")
    assert b'"reference_price":"4500.01"' in canonical(_row(event(reference_price=Decimal("4500.005")), 1))
    assert b'"reference_price":null' in canonical(_row(event(), 1))


def test_spooled_event_comes_back_identical():
    e = event(reference_price=Decimal("4500.00"))
    from dataclasses import asdict
    back = _event(json.loads(json.dumps(plain(asdict(e)))))
    assert chain_hash(GENESIS, _row(back, 1)) == chain_hash(GENESIS, _row(e, 1))


# ---------------------------------------------------------------- replay

def _record(pack, ctx: Context, base="3000", signals=None):
    result = evaluate(compile_pack(pack), ctx)
    return {"as_of": ctx.as_of, "reference_price": ctx.reference_price, "output": output_of(result),
            "inputs": plain({"base_price": Decimal(base), "signals": signals or {}, "redis_signals": {},
                             "allow_public_override": False, "policy": None})}


def test_replay_of_an_untouched_record_is_identical():
    pack = load_pack("hotel.yaml")
    signals = {"occupancy": 0.85}
    ctx = Context(as_of=AS_OF, base_price=Decimal("3000"), signals=signals, reference_price=Decimal("4500"))
    record = _record(pack, ctx, signals=signals)
    out = replay(record, pack)
    assert out["identical"] is True and out["diff"] == []
    assert out["replayed"]["price"] == "4950.00"
    assert out["replayed"]["bounds"] == {"lower": "3825.00", "upper": "5000.00"}
    rebuilt = context_of(record)                                       # stored text rebuilds the same inputs
    assert (rebuilt.as_of, rebuilt.base_price) == (ctx.as_of, ctx.base_price)
    assert rebuilt.reference_price == ctx.reference_price
    assert rebuilt.signals == {"occupancy": "0.85"}                    # a float is kept as its text, never as a float


def test_replay_against_changed_rules_shows_what_would_differ():
    pack = load_pack("hotel.yaml")
    signals = {"occupancy": 0.85}
    record = _record(pack, Context(as_of=AS_OF, base_price=Decimal("3000"), signals=signals), signals=signals)
    draft = load_pack("hotel.yaml")
    draft["groups"][0]["rules"][1]["then"]["value"] = 0                # weekend uplift removed
    out = replay(record, draft)
    assert out["identical"] is False
    assert (out["original"]["price"], out["replayed"]["price"]) == ("4950.00", "4500.00")
    assert {d["field"] for d in out["diff"]} >= {"price", "waterfall.weekend", "waterfall.festival"}


def test_replay_of_a_tampered_record_is_not_identical():
    pack = load_pack("hotel.yaml")
    signals = {"occupancy": 0.85}
    record = _record(pack, Context(as_of=AS_OF, base_price=Decimal("3000"), signals=signals), signals=signals)
    record["output"] = {**record["output"], "price": "1.00"}           # someone lowered the recorded price
    out = replay(record, pack)
    assert out["identical"] is False and out["diff"][0] == {"field": "price", "original": "1.00", "replayed": "4950.00"}


def test_replay_reports_an_engine_error_instead_of_raising():
    pack = load_pack("hotel.yaml")
    record = {"as_of": AS_OF, "reference_price": None, "output": {"price": "4950.00"},
              "inputs": {"base_price": "8000", "signals": {}, "redis_signals": {}}}
    out = replay(record, pack)
    assert out["identical"] is False and out["replayed"]["error"]["code"] == "bounds_conflict"
