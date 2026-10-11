"""control.schema.validate: templates are valid; errors come back with paths (PACK_SCHEMA "In code")."""
from monetize360.control.schema import validate
from tests.conftest import load_pack


def test_templates_are_valid():
    assert validate(load_pack("hotel.yaml")) == []
    assert validate(load_pack("airline.yaml")) == []


def test_errors_have_paths():
    pack = load_pack("hotel.yaml")
    pack["groups"][0]["stacking"] = "multiplly"
    pack["guards"].pop("ceiling")
    paths = [e["path"] for e in validate(pack)]
    assert "groups/0/stacking" in paths and "guards" in paths
