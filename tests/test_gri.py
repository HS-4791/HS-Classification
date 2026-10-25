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
    return GRIEngine(build_exclusion_graph(notes))


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

def test_a_fifty_fifty_blend_is_settled_by_the_note_not_escalated(engine):
    """Note 2(A) carries its own tie-break, so GRI 3 is never reached.

    "When no one textile material predominates by weight, the goods are to
    be classified ... under the heading which occurs last in numerical order
    among those which equally merit consideration." Escalating here would be
    the error the primer names: reaching Rule 3 where Rule 1 answered.
    """
    facts = extract_facts("knitted t-shirt, 50% cotton and 50% polyester")
    decision = engine.adjudicate(facts, [COTTON, MAN_MADE], TEXTS)
    assert not decision.escalated
    assert decision.code == MAN_MADE  # 6109.90 occurs after 6109.10
    assert "last in numerical order" in decision.reason


def test_the_rule_path_names_only_rules_that_were_evaluated(engine):
    """A path is a record of what fired, not of where the procedure might go.

    Listing GRI 2(b) and 3(a) on a case that never reached them puts a
    fabrication into the trace that week 4 scores explanations against.
    """
    # Two headings, one material: the failure is at GRI 1 and stops there.
    decision = engine.adjudicate(
        extract_facts("a cotton t-shirt"), [COTTON, MAN_MADE], TEXTS
    )
    assert decision.escalated
    assert decision.rule_path == ["GRI 1"]


def test_a_named_heading_beats_a_residual_one_under_gri_3a(engine):
    """The narrow, deterministic part of GRI 3(a): a basket loses to a name."""
    texts = {
        "6109100012": "T-shirts, knitted — Of cotton — men's",
        "6109909000": "T-shirts, knitted — Of other textile materials — Other",
    }
    facts = extract_facts("t-shirt of 50% ramie and 50% silk")
    decision = engine.adjudicate(facts, list(texts), texts)
    assert decision.code == "6109100012"
    assert decision.rule_path == ["GRI 1", "GRI 2(b)", "GRI 3(a)"]


def test_essential_character_still_escalates_when_nothing_separates_them(engine):
    """GRI 3(b) is a judgment call; answering it confidently is the danger."""
    texts = {
        "6109100012": "T-shirts, knitted — Of cotton — men's",
        "6109901000": "T-shirts, knitted — Of man-made fibres — men's",
    }
    facts = extract_facts("a composite article of 50% leather and 50% rubber")
    decision = engine.adjudicate(facts, list(texts), texts)
    assert decision.code is None and decision.escalated
    assert decision.rule_path == ["GRI 1", "GRI 2(b)", "GRI 3(a)", "GRI 3(b)"]


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


def test_the_engine_holds_no_per_request_state(engine):
    """Candidate texts are passed in, never stashed on the engine.

    Stashing them made every helper depend on a prior adjudicate() call -
    calling one directly raised AttributeError - and meant two classifications
    sharing an engine could read each other's texts.
    """
    assert not any(attr.startswith("_texts") for attr in vars(engine))
    # Deciding one case must not change what the engine knows for the next.
    first = engine.adjudicate(extract_facts("t-shirt, 60% cotton, 40% polyester"),
                              [COTTON, MAN_MADE], TEXTS)
    second = engine.adjudicate(extract_facts("t-shirt, 60% cotton, 40% polyester"),
                               [COTTON, MAN_MADE], {})
    assert first.code == COTTON
    # With no texts supplied the rule cannot match a material, so it escalates
    # rather than reusing what the previous call happened to pass in.
    assert second.code is None and second.escalated


# ------------------------------------------------------------------ GRI 2(a)

BICYCLE = "8712003000"
BICYCLE_PARTS = "8714911000"
VEHICLE_TEXTS = {
    BICYCLE: "Bicycles and other cycles, not motorized \u2014 Other bicycles",
    BICYCLE_PARTS: "Parts and accessories of vehicles of heading 8712 \u2014 Frames and forks",
}


def test_an_unassembled_article_is_the_finished_article(engine):
    """GRI 2(a): an unassembled bicycle is a bicycle, not bicycle parts."""
    facts = extract_facts("a bicycle imported unassembled, in a carton")
    decision = engine.adjudicate(facts, [BICYCLE, BICYCLE_PARTS], VEHICLE_TEXTS)
    assert decision.code == BICYCLE
    assert decision.rule_path == ["GRI 1", "GRI 2(a)"]


def test_the_rule_needs_the_trigger_to_be_stated(engine):
    """Without "unassembled" there is nothing to establish 2(a) from."""
    decision = engine.adjudicate(
        extract_facts("a bicycle"), [BICYCLE, BICYCLE_PARTS], VEHICLE_TEXTS
    )
    assert decision.escalated
    assert "GRI 2(a)" not in decision.rule_path


def test_2a_declines_when_it_cannot_tell_the_whole_from_the_parts(engine):
    """Two parts provisions leave nothing for the rule to choose between.

    213 descriptions carry a trigger word but only 44 are 2(a) cases, so the
    rule firing whenever the word appears would be worse than not firing.
    """
    texts = {
        "8714911000": "Parts and accessories \u2014 Frames and forks",
        "8714921000": "Parts and accessories \u2014 Wheel rims and spokes",
    }
    decision = engine.adjudicate(
        extract_facts("unassembled bicycle parts"), list(texts), texts
    )
    assert decision.code is None and decision.escalated
    assert decision.rule_path[-1] == "GRI 2(a)" or "GRI 2(a)" in decision.rule_path


def test_gri_4_is_never_claimed(engine):
    """It decides 0 of 18,253 rulings and "most akin" is a judgment.

    Naming it on a path without evaluating it is the fabrication this engine
    was already corrected for once.
    """
    decisions = [
        engine.adjudicate(extract_facts("a bicycle"), [BICYCLE, BICYCLE_PARTS], VEHICLE_TEXTS),
        engine.adjudicate(extract_facts("t-shirt, 50% ramie and 50% silk"),
                          [COTTON, MAN_MADE], TEXTS),
    ]
    assert all("GRI 4" not in d.rule_path for d in decisions)
