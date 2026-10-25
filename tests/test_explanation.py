"""Scoring the reasoning against what the ruling says it applied.

Every phrasing below is taken from the ATLAS reasoning text, not invented.
"""

from __future__ import annotations

from hsclass.eval.explanation import (
    Authorities,
    ExplanationScore,
    authorities_in,
    score_explanations,
)
from hsclass.pipeline.gri import Decision

BOILERPLATE = (
    "According to GRI 1, classification is determined based on the terms of the "
    "headings and any relevant section and chapter notes."
)
ESSENTIAL_CHARACTER = (
    "The kit is classified as a set under GRI 3(b) because it contains multiple "
    "articles put together to fulfill a specific activity."
)
TIE_BREAK = (
    "Since neither item has essential character over the other, GRI 3(c) applies, "
    "which states that when goods cannot be classified by reference to 3(a) or 3(b), "
    "they are classified under the heading occurring last."
)


def test_a_gri_is_read_with_its_letter():
    assert authorities_in(ESSENTIAL_CHARACTER).gris == {"3(b)"}
    assert "3(c)" in authorities_in(TIE_BREAK).gris


def test_the_spelled_out_form_is_read_too():
    text = "General Rule of Interpretation (GRI) 1 states that classification..."
    assert "1" in authorities_in(text).gris


def test_the_gri_1_recital_is_not_a_discriminating_citation():
    """404 rulings recite it. A system that always said "GRI 1" would score
    well on it while knowing nothing, so it is excluded from the headline."""
    cited = authorities_in(BOILERPLATE)
    assert cited.gris == {"1"}
    assert cited.beyond_gri_1 == frozenset()


def test_generic_section_and_chapter_notes_are_not_a_note_citation():
    """"any relevant section and chapter notes" names no provision."""
    assert authorities_in(BOILERPLATE).notes == frozenset()


def test_a_numbered_note_is_a_citation():
    text = "...meeting the criteria set forth in Additional U.S. Note 4 to chapter 90."
    assert "Additional U.S. Note 4" in authorities_in(text).notes


def test_the_deciding_rule_is_the_furthest_the_reasoning_went():
    """Reaching Rule 3 where Rule 1 answered is itself an error, so how far
    the ruling had to go is the comparable quantity."""
    assert authorities_in(TIE_BREAK).deciding_rule == "3(c)"
    assert authorities_in(BOILERPLATE).deciding_rule == "1"


def test_gri_6_does_not_compete_with_the_main_sequence():
    """It governs the subheading once the heading is settled."""
    cited = Authorities(gris=frozenset({"1", "6"}))
    assert cited.deciding_rule == "1"
    assert cited.beyond_gri_1 == frozenset()


def test_headings_are_collected_without_their_dots():
    text = "classified under subheading 7307.91.3000 rather than heading 7307"
    assert authorities_in(text).headings == {"7307913000", "7307"}


# ------------------------------------------------------------------ scoring

def decided(code: str, *rules: str) -> Decision:
    return Decision(code, list(rules))


def escalated() -> Decision:
    return Decision(None, ["GRI 1"], escalated=True)


def test_only_discriminating_rulings_count_toward_the_headline():
    score = score_explanations([
        (decided("610910", "GRI 1"), "610910", BOILERPLATE),
        (decided("950300", "GRI 1", "GRI 3(b)"), "950300", ESSENTIAL_CHARACTER),
    ])
    assert score.n == 2
    assert score.discriminating == 1  # the boilerplate case is not evidence


def test_agreement_requires_reaching_the_same_rule():
    score = score_explanations([
        (decided("950300", "GRI 1", "GRI 3(b)"), "950300", ESSENTIAL_CHARACTER),
    ])
    assert score.decided == 1 and score.agreed == 1
    assert score.agreement == 1.0


def test_right_code_by_the_wrong_route_is_counted_separately():
    """The class accuracy cannot see: correct answer, route the ruling never used."""
    score = score_explanations([
        (decided("950300", "GRI 1"), "950300", ESSENTIAL_CHARACTER),
    ])
    assert score.agreed == 0
    assert score.right_code_wrong_path == 1
    assert score.unreached == {"3(b)": 1}


def test_a_wrong_code_by_the_wrong_route_is_not_counted_as_right_code():
    score = score_explanations([
        (decided("610910", "GRI 1"), "950300", ESSENTIAL_CHARACTER),
    ])
    assert score.right_code_wrong_path == 0
    assert score.unreached == {"3(b)": 1}


def test_an_escalation_reaches_no_rule_but_still_records_what_it_missed():
    score = score_explanations([(escalated(), "950300", ESSENTIAL_CHARACTER)])
    assert score.discriminating == 1
    assert score.decided == 0
    assert score.unreached == {"3(b)": 1}


def test_an_empty_run_does_not_divide_by_zero():
    assert ExplanationScore().agreement == 0.0
    assert score_explanations([]).to_dict()["agreement"] == 0.0


def test_a_gri_cited_without_its_letter_still_counts():
    """103 rulings say "GRI 3" with no letter.

    Omitting the bare forms from the ordering made deciding_rule fall through
    to GRI 1, or to nothing - scoring a case the ruling plainly took to Rule 3
    as though the terms of the heading had settled it.
    """
    cited = authorities_in("The goods are classified under GRI 3 as a composite good.")
    assert cited.gris == {"3"}
    assert cited.deciding_rule == "3"


def test_a_lettered_rule_outranks_the_bare_one_it_belongs_to():
    cited = authorities_in("GRI 3 applies; specifically GRI 3(b), essential character.")
    assert cited.deciding_rule == "3(b)"
