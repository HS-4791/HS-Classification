"""The live service: two tiers of answer, and neither pretends to be the other."""

from __future__ import annotations

import json

import pytest

from hsclass.serve.app import Service

PROVISIONS = [
    {"c": "6110", "t": "Sweaters, pullovers and similar articles, knitted"},
    {"c": "611020", "t": "Sweaters, pullovers — Of cotton"},
    {"c": "6110202069", "t": "Sweaters, pullovers — Of cotton — Men's or boys' (338)"},
    {"c": "611030", "t": "Sweaters, pullovers — Of man-made fibres"},
    {"c": "6110301560", "t": "Sweaters, pullovers — Of man-made fibres — Women's"},
]
RULINGS = [
    {"d": "a men's knit pullover of 100% cotton, ribbed collar", "c": "6110202069"},
    {"d": "a women's knit cardigan of acrylic fibre", "c": "6110301560"},
]


@pytest.fixture
def bundle(tmp_path):
    (tmp_path / "precedents.json").write_text(json.dumps(RULINGS), encoding="utf-8")
    (tmp_path / "provisions.json").write_text(json.dumps(PROVISIONS), encoding="utf-8")
    (tmp_path / "notes.json").write_text("[]", encoding="utf-8")
    (tmp_path / "bundle.json").write_text(
        json.dumps({"revision": "TESTREV"}), encoding="utf-8"
    )
    return tmp_path


def test_the_service_loads_without_embeddings_or_a_key(bundle):
    """The reason this path is the deployable one: nothing to download."""
    service = Service(bundle)
    assert service.revision == "TESTREV"
    assert len(service.precedents.precedents) == 2
    assert len(service.texts) == 5


def test_a_response_carries_the_schedule_revision(bundle):
    """A classification is only true against a version of the nomenclature."""
    answer = Service(bundle).classify("a men's cotton knit pullover")
    assert answer["schedule_revision"] == "TESTREV"


def test_precedent_is_offered_separately_from_the_rules(bundle):
    """Two tiers, labelled.

    The rules answer about 2% of cases at 42% accuracy; precedent answers
    everything at 52.5% top-1. Collapsing them would either report "not
    classified" to almost every query while holding a better-than-even guess
    back, or pass a similarity match off as an adjudicated answer.
    """
    answer = Service(bundle).classify("a men's knit pullover of 100% cotton")
    assert answer["suggested"] == "6110202069"
    assert "ruling" in answer["suggested_basis"]
    # Whatever the rules did, the suggestion is never presented as their work.
    assert answer["decided"] is (answer["code"] is not None)


def test_the_rulings_behind_an_answer_are_returned(bundle):
    answer = Service(bundle).classify("a women's knit cardigan of acrylic")
    assert answer["precedents"]
    assert answer["precedents"][0]["code"] == "6110301560"


def test_a_description_matching_nothing_still_answers_honestly(bundle):
    """No precedent means no suggestion - not a fabricated one."""
    answer = Service(bundle).classify("xyzzy plugh frobnitz")
    assert answer["suggested"] is None
    assert answer["decided"] is False
    assert answer["explanation"]


def test_extracted_attributes_are_reported(bundle):
    answer = Service(bundle).classify("a pullover of 60% cotton and 40% modal")
    assert answer["attributes"]["materials"] == {"cotton": 60.0, "modal": 40.0}
