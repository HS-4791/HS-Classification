"""The live service: two tiers of answer, and neither pretends to be the other."""

from __future__ import annotations

import json

import pytest

from hsclass.serve.app import PAGE, Service

PROVISIONS = [
    {"c": "6110", "t": "Sweaters, pullovers and similar articles, knitted"},
    {"c": "611020", "t": "Sweaters, pullovers — Of cotton"},
    {"c": "6110202069", "t": "Sweaters, pullovers — Of cotton — Men's or boys' (338)"},
    {"c": "611030", "t": "Sweaters, pullovers — Of man-made fibres"},
    {"c": "6110301560", "t": "Sweaters, pullovers — Of man-made fibres — Women's"},
    # The eight-digit parent exists; the ...32 suffix the ruling cites does not.
    {"c": "73063050", "t": "Tubes and pipes, of iron or nonalloy steel — Other"},
]
RULINGS = [
    {"d": "a men's knit pullover of 100% cotton, ribbed collar", "c": "6110202069"},
    {"d": "a women's knit cardigan of acrylic fibre", "c": "6110301560"},
    # A ruling citing a ten-digit suffix this revision does not carry. Not a
    # contrivance: 2,106 of the 18,253 real rulings do exactly this, because
    # the rulings are historical and the statistical suffixes get renumbered.
    {"d": "a galvanised steel pipe nipple, threaded", "c": "7306305032"},
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
    assert len(service.precedents.precedents) == len(RULINGS)
    assert len(service.texts) == len(PROVISIONS)


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
    # And no accuracy claim for a suggestion that does not exist. This shipped
    # once: the page hid the string because it renders it inside the branch
    # that needs a suggestion, so only an API consumer would have seen a basis
    # justifying a null.
    assert answer["suggested_basis"] is None
    # And no rule is named, because none was evaluated: with no candidates
    # there were no heading terms to compare, so GRI 1 was never reached.
    # It used to report rule_path ["GRI 1"] and say "the rules were applied as
    # far as GRI 1", which CLAUDE.md calls a fabrication in the trace.
    assert answer["rule_path"] == []
    assert "GRI" not in answer["explanation"]
    assert "No rule was reached" in answer["explanation"]


def test_the_basis_is_sent_whenever_a_suggestion_is(bundle):
    answer = Service(bundle).classify("a men's knit pullover of 100% cotton")
    assert answer["suggested"]
    assert answer["suggested_basis"] and "18,253 rulings" in answer["suggested_basis"]


def test_extracted_attributes_are_reported(bundle):
    answer = Service(bundle).classify("a pullover of 60% cotton and 40% modal")
    assert answer["attributes"]["materials"] == {"cotton": 60.0, "modal": 40.0}


def test_a_revision_mismatch_refuses_to_serve(bundle, monkeypatch):
    """The Dockerfile's claim, now enforced rather than asserted.

    ``--build-arg HTS_REVISION=2026HTSRev22`` over a Rev21 bundle used to
    produce a container whose environment said Rev22 and whose every response
    said Rev21, with nothing reading the variable at all.
    """
    monkeypatch.setenv("HTS_REVISION", "SOMEOTHERREV")
    with pytest.raises(ValueError, match="only valid against a schedule version"):
        Service(bundle)


def test_a_matching_revision_serves(bundle, monkeypatch):
    monkeypatch.setenv("HTS_REVISION", "TESTREV")
    assert Service(bundle).revision == "TESTREV"


def test_a_code_missing_from_the_revision_resolves_to_its_parent(bundle):
    """11.5% of the indexed rulings cite a code this revision does not carry.

    Found by running the container: the page printed ``7306305032`` with no
    text beside it, two lines under a banner naming the revision - offering a
    code as an answer while the revision it claims to classify against does
    not contain it.
    """
    at = Service(bundle).resolve("7306305032")
    assert at["in_revision"] is False
    assert at["text_of"] == "73063050"
    assert at["text"].startswith("Tubes and pipes")
    assert at["reason"] == "suffix renumbered"


def test_a_code_of_illegal_length_is_called_malformed_not_renumbered(bundle):
    """A nine-digit code is not a renumbered suffix, and saying so was wrong.

    Three of the 18,253 indexed rulings cite one. The guard used to skip level
    10 for them and drop to a bare heading while reporting the same
    "renumbered statistical suffix" cause as a genuine ten-digit miss.
    """
    at = Service(bundle).resolve("611020206")
    assert at["in_revision"] is False
    assert at["reason"] == "malformed"


def test_a_withdrawn_tariff_line_is_not_called_a_renumbered_suffix(bundle):
    """When the eight-digit line is gone too, more than the suffix changed.

    Measured over the 200-case split: 22 of the 35 flagged codes resolve to an
    eight-digit parent, so 13 had a cause asserted about them that was false.
    """
    at = Service(bundle).resolve("6110209999")
    assert at["in_revision"] is False
    assert at["text_of"] == "611020"
    assert at["reason"] == "line withdrawn"


def test_a_code_present_in_the_revision_is_reported_as_present(bundle):
    at = Service(bundle).resolve("6110202069")
    assert at["in_revision"] is True
    assert at["text_of"] == "6110202069"


def test_the_resolver_only_cuts_at_legal_levels(bundle):
    """A code truncated at 7 or 9 digits is not a provision, so never tried.

    The fixture has no eight-digit 61102020, so a ten-digit code falls past it
    to the six-digit subheading. A walk that stepped one digit at a time would
    stop at the nine-digit string planted here instead.
    """
    service = Service(bundle)
    service.texts["611020206"] = "nine digits is not a level of the nomenclature"
    at = service.resolve("6110202061")
    assert at["text_of"] == "611020"
    assert at["in_revision"] is False


def test_an_unresolvable_code_says_so_rather_than_guessing(bundle):
    at = Service(bundle).resolve("9999999999")
    assert at["text"] is None
    assert at["text_of"] is None
    assert at["in_revision"] is False


def test_the_response_says_whether_the_suggestion_is_in_the_revision(bundle):
    answer = Service(bundle).classify("a galvanised steel pipe nipple, threaded")
    assert answer["suggested"] == "7306305032"
    assert answer["suggested_in_revision"] is False
    assert answer["suggested_text_of"] == "73063050"
    # And the page has to say it, not merely carry it in the JSON.
    assert "suggested_in_revision === false" in PAGE


def test_the_page_trims_chains_by_segment_not_by_character():
    """Found by running the container, not by reading the code.

    The shortlist used to keep the last 90 characters of a provision's
    inherited chain, which cut mid-word: ``6105901000 rts, knitted or
    crocheted`` and ``6110301560 23 percent or more by weight of wool``. A
    reviewer reads that as a broken page, not as an abbreviation.
    """
    assert "esc(tail(s.text, 90))" in PAGE
    assert ".slice(-90)" not in PAGE


def test_the_page_carries_no_backslash():
    """PAGE is not a raw string, so a backslash in it is a Python escape.

    ``\\s`` inside the trimming regex was an invalid escape sequence: a
    DeprecationWarning today, a SyntaxError in a later Python. The rule is
    cheaper to pin than to remember, and it covers the comments too - the
    first fix left the sequence in a comment and the warning stayed.
    """
    assert "\\" not in PAGE
