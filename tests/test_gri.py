"""The GRI engine - the stage that decides, with no model in it."""

from __future__ import annotations

import pytest

from hsclass.pipeline.gri import Decision, Facts, GRIEngine, extract_facts
from hsclass.schedule.exclusions import build_exclusion_graph
from hsclass.schedule.notes import Note

COTTON = "6109100012"
MAN_MADE = "6109901000"
TEXTS = {
    COTTON: "T-shirts, singlets, tank tops, knitted or crocheted - Of cotton - men's",
    MAN_MADE: "T-shirts, singlets, tank tops, knitted - Of other textile materials "
              "- of man-made fibres polyester",
}


@pytest.fixture
def engine():
    notes = [
        Note(kind="note", number="2(A)", section="XI",
             text="Goods classifiable in chapters 50 to 55 ... predominates by weight ..."),
        Note(kind="subheading_note", number="2(A)", section="XI",
             text="Products of chapters 56 to 63 containing two or more textile materials ..."),
        Note(kind="note", number="1(h)", section="XI",
             lead_in="This section does not cover:",
             text="Woven fabrics laminated with plastics, of chapter 39;"),
    ]
    return GRIEngine(notes, build_exclusion_graph(notes))


# ------------------------------------------------------------ fact extraction

@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("60% cotton and 40% polyester", {"cotton": 60.0, "polyester": 40.0}),
        ("cotton 60%, polyester 40%", {"cotton": 60.0, "polyester": 40.0}),
        ("containing 85 percent or more by weight of wool", {"wool": 85.0}),
        ("a plain cotton shirt", {}),
    ],
)
def test_composition_is_read_out_of_the_description(description, expected):
    assert extract_facts(description).materials == expected


def test_predominance_requires_a_winner():
    assert Facts(materials={"cotton": 60, "polyester": 40}).predominant() == ("cotton", 60)
    # Note 2(A) turns on predominance over each *other single* material, so a
    # tie does not resolve and must not be guessed.
    assert Facts(materials={"cotton": 50, "polyester": 50}).predominant() is None


# ------------------------------------------------------------- the running case

def test_sixty_forty_cotton_classifies_as_cotton(engine):
    facts = extract_facts("men's knitted t-shirt, 60% cotton and 40% polyester")
    decision = engine.adjudicate(facts, [COTTON, MAN_MADE], TEXTS)
    assert decision.code == COTTON
    assert not decision.escalated


def test_reversing_the_blend_changes_the_answer(engine):
    """A number in a composition table decides the legal outcome."""
    facts = extract_facts("men's knitted t-shirt, 40% cotton and 60% polyester")
    assert engine.adjudicate(facts, [COTTON, MAN_MADE], TEXTS).code == MAN_MADE


def test_the_rule_path_cites_what_a_ruling_would(engine):
    """Chapter 61 reaches Note 2(A) only through the Subheading Note.

    Citing Note 2(A) alone skips the provision that brings it to bear on a
    knitted shirt, which is the kind of gap an explanation gets marked on.
    """
    facts = extract_facts("knitted t-shirt, 60% cotton, 40% polyester")
    decision = engine.adjudicate(facts, [COTTON, MAN_MADE], TEXTS)
    assert decision.rule_path == [
        "GRI 1",
        "Section XI Subheading Note 2(A)",
        "Section XI Note 2(A)",
        "GRI 6",
    ]


# ----------------------------------------------------------------- escalation

def test_a_fifty_fifty_blend_escalates_at_essential_character(engine):
    """GRI 3(b) is a judgment call; answering it confidently is the danger."""
    facts = extract_facts("knitted t-shirt, 50% cotton and 50% polyester")
    decision = engine.adjudicate(facts, [COTTON, MAN_MADE], TEXTS)
    assert decision.code is None
    assert decision.escalated
    assert decision.rule_path[-1] == "GRI 3(b)"
    assert decision.shortlist == [COTTON, MAN_MADE]


def test_nothing_proposed_escalates_rather_than_inventing_a_code(engine):
    decision = engine.adjudicate(Facts(description="a thing"), [])
    assert decision.code is None and decision.escalated


def test_a_single_candidate_resolves_at_gri_1(engine):
    decision = engine.adjudicate(extract_facts("cotton t-shirt"), [COTTON], TEXTS)
    assert decision.code == COTTON
    assert decision.rule_path == ["GRI 1"]


# --------------------------------------------------------------- determinism

def test_the_same_input_decides_the_same_way_every_time(engine):
    """No model, no sampling: the decision is a function of its inputs."""
    facts = extract_facts("men's knitted t-shirt, 60% cotton and 40% polyester")
    first = engine.adjudicate(facts, [COTTON, MAN_MADE], TEXTS).to_dict()
    for _ in range(100):
        assert engine.adjudicate(facts, [COTTON, MAN_MADE], TEXTS).to_dict() == first


def test_candidate_order_does_not_change_the_decision(engine):
    """Retrieval ranking must not leak into adjudication."""
    facts = extract_facts("knitted t-shirt, 60% cotton, 40% polyester")
    forward = engine.adjudicate(facts, [COTTON, MAN_MADE], TEXTS)
    reversed_ = engine.adjudicate(facts, [MAN_MADE, COTTON], TEXTS)
    assert forward.code == reversed_.code == COTTON


def test_a_decision_serialises_with_its_reasoning():
    decision = Decision("6109100012", ["GRI 1", "GRI 6"], ["Section XI Note 2(A)"])
    assert decision.to_dict()["rule_path"] == ["GRI 1", "GRI 6"]
    assert decision.to_dict()["notes_cited"] == ["Section XI Note 2(A)"]
