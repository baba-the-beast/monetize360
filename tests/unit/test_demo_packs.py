"""demo/packs: the files uploaded live in the admin console must do on stage exactly what their headers promise.
Upload = control.publisher.parse (schema + compile), then the console's Check (lint) and Safety test (verify)."""
from pathlib import Path

import pytest
import yaml

from monetize360.control import publisher
from monetize360.control.linter import lint
from monetize360.control.verifier import verify

DEMO = Path(__file__).resolve().parents[2] / "demo" / "packs"
GOOD = sorted(DEMO.glob("*.yaml"))


def domain_of(path: Path) -> str:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["domain"]


def test_every_domain_has_a_demo_file():
    templates = {p.stem for p in (DEMO.parents[1] / "packs" / "templates").glob("*.yaml")}
    assert templates <= {domain_of(p) for p in GOOD}


@pytest.mark.parametrize("path", GOOD, ids=lambda p: p.name)
def test_good_demo_file_uploads_lints_and_is_verified_safe(path):
    pack, _ = publisher.parse(path.read_bytes(), domain_of(path))      # what PUT /v1/packs/{domain} runs
    assert lint(pack)["ok"]
    result = verify(pack, cases=300)
    assert result["ok"] and result["badge"] == "verified_safe"


def test_gate1_typo_is_refused_at_upload():
    path = DEMO / "should-fail" / "gate1-typo-hotel.yaml"
    with pytest.raises(publisher.LifecycleError) as refused:
        publisher.parse(path.read_bytes(), "hotel")
    assert any("ceilng" in str(d) for d in refused.value.details)


def test_gate2_unfair_rule_is_saved_but_lint_blocks_it():
    pack, _ = publisher.parse((DEMO / "should-fail" / "gate2-unfair-hotel.yaml").read_bytes(), "hotel")
    result = lint(pack)
    assert not result["ok"]
    assert "Fairness violation: 'religion' is a protected attribute" in result["errors"][0]["message"]


def test_gate3_overlap_passes_lint_but_fails_the_safety_test():
    pack, _ = publisher.parse((DEMO / "should-fail" / "gate3-overlap-retail.yaml").read_bytes(), "retail")
    assert lint(pack)["ok"]
    result = verify(pack, cases=1_500)
    assert not result["ok"] and result["smallest"]["input"]["signals"]["inventory"] == 501
