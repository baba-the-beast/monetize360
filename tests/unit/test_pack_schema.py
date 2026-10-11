"""Pack JSON Schema: every template is valid, and each class of mistake is rejected."""
import copy
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "packs/schema/pack.schema.json").read_text(encoding="utf-8"))
Draft202012Validator.check_schema(SCHEMA)
VALIDATOR = Draft202012Validator(SCHEMA)
TEMPLATES = sorted((ROOT / "packs/templates").glob("*.yaml"))


def load(name):
    return yaml.safe_load((ROOT / "packs/templates" / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_templates_are_valid(path):
    errors = [e.message for e in VALIDATOR.iter_errors(yaml.safe_load(path.read_text("utf-8")))]
    assert errors == []


def broken(mutate):
    pack = copy.deepcopy(load("hotel.yaml"))
    mutate(pack)
    return list(VALIDATOR.iter_errors(pack))


@pytest.mark.parametrize("why, mutate", [
    ("typo in guard name", lambda p: p["guards"].update(ceilng=p["guards"].pop("ceiling"))),
    ("missing ceiling", lambda p: p["guards"].pop("ceiling")),
    ("unknown stacking", lambda p: p["groups"][0].update(stacking="multiplly")),
    ("unknown operator", lambda p: p["groups"][0]["rules"][0].update(then={"op": "magic"})),
    ("pct missing value", lambda p: p["groups"][0]["rules"][0].update(then={"op": "pct"})),
    ("extra field on pct", lambda p: p["groups"][0]["rules"][0]["then"].update(signal="x")),
    ("pct below -100%", lambda p: p["groups"][0]["rules"][0]["then"].update(value=-150)),
    ("rule without reason", lambda p: p["groups"][0]["rules"][0].pop("reason")),
    ("enum signal without values", lambda p: p["signals"]["festival"].pop("values")),
    ("redis signal without staleness", lambda p: p["signals"].update(
        competitor={"type": "money", "source": "redis"})),
    ("staleness on a request signal", lambda p: p["signals"]["occupancy"].update(max_staleness="1h")),
    ("bad signal name", lambda p: p["signals"].update({"Bad Name": {"type": "bool", "source": "request"}})),
    ("unknown base op", lambda p: p.update(base={"op": "guess"})),
    ("too many rules", lambda p: p["groups"][0].update(rules=p["groups"][0]["rules"] * 100)),
    ("zero rounding step", lambda p: p["rounding"].update(step=0)),
    ("negative rounding step", lambda p: p["rounding"].update(step=-50)),
    ("zero floor", lambda p: p["guards"]["floor"].update(value=0)),
    ("negative ceiling", lambda p: p["guards"]["ceiling"].update(value=-1)),
    ("ending above 99", lambda p: p["rounding"].update(ending=100)),
    ("negative ending", lambda p: p["rounding"].update(ending=-1)),
    ("banker's rounding", lambda p: p["rounding"].update(mode="half_even")),
    ("lowercase currency", lambda p: p.update(currency="inr")),
])
def test_mistakes_are_rejected(why, mutate):
    assert broken(mutate), why
