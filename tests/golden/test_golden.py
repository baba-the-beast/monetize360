"""Runs every case in tests/golden/*.yaml: exact price AND exact non-zero waterfall rows must match
the hand-calculated values. This is the acceptance test for the engine (docs/ENGINE_SPEC.md).

A case may also say
    expect_error: lookup_miss          # the request must be refused with this error code (no price)
    expect_flags: ["occupancy missing, used default 0.5"]   # exactly these flags, in order
expect_price / expect_waterfall are required unless expect_error is given."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from monetize360.engine import Context, EngineError, compile_pack, evaluate

ROOT = Path(__file__).resolve().parents[2]
CONTEXT_FIELDS = {"base_price", "reference_price", "as_of"}
DEFAULT_AS_OF = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)   # a Wednesday, no festival


def load_cases():
    for file in sorted((ROOT / "tests/golden").glob("*.yaml")):
        spec = yaml.safe_load(file.read_text(encoding="utf-8"))
        pack = yaml.safe_load((ROOT / spec["pack"]).read_text(encoding="utf-8"))
        for case in spec["cases"]:
            yield pytest.param(pack, case, id=f"{file.stem}: {case['name']}")


def money(x):
    return None if x is None else Decimal(str(x))


@pytest.mark.parametrize("pack_dict, case", list(load_cases()))
def test_golden(pack_dict, case):
    inputs = dict(case["inputs"])
    ctx = Context(
        base_price=money(inputs.pop("base_price", None)),
        reference_price=money(inputs.pop("reference_price", None)),
        as_of=inputs.pop("as_of", DEFAULT_AS_OF),
        signals=inputs,
        redis_signals={},
        allow_public_override=True,
    )
    pack = compile_pack(pack_dict)

    if "expect_error" in case:
        with pytest.raises(EngineError) as refused:
            evaluate(pack, ctx)
        assert refused.value.code == case["expect_error"], refused.value.message
        return

    result = evaluate(pack, ctx)
    assert result.price == Decimal(case["expect_price"])
    rows = [
        {"step": r.step, "delta": f"{r.delta:.2f}", "after": f"{r.after:.2f}"}
        for r in result.waterfall
        if r.delta != 0
    ]
    assert rows == case["expect_waterfall"]
    if "expect_flags" in case:
        assert list(result.flags) == case["expect_flags"]
