"""The GRI engine - the stage that decides, with no model in it."""

from __future__ import annotations

import pytest

from hsclass.pipeline.gri import Decision, Facts, GRIEngine, extract_facts
from hsclass.pipeline.gri import without as without_mechanisms
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


# ------------------------------------- GRI 1: conditions stated in a provision

SILK_HIGH = "6204194000"
SILK_LOW = "6204192000"
CONDITION_TEXTS = {
    SILK_HIGH: "Women's suits — Of other textile materials — Containing 70 percent "
               "or more by weight of silk or silk waste",
    SILK_LOW: "Women's suits — Of other textile materials — Other",
}


def test_a_threshold_the_goods_fail_removes_the_provision(engine):
    """GRI 1 at its most direct, and arithmetic rather than similarity.

    A suit of 40 percent silk is outside "70 percent or more by weight of
    silk" as a matter of fact. The two descriptions are near-identical
    strings, so nothing before this stage can separate them.
    """
    facts = Facts(description="a suit of 40% silk and 60% cotton",
                  materials={"silk": 40.0, "cotton": 60.0})
    decision = engine.adjudicate(facts, [SILK_HIGH, SILK_LOW], CONDITION_TEXTS)
    assert decision.code == SILK_LOW
    assert decision.rule_path == ["GRI 1", "GRI 6"]  # same heading, one level down
    assert decision.conditions_applied and SILK_HIGH in decision.conditions_applied[0]
    assert not decision.escalated


def test_a_threshold_the_goods_meet_does_not_remove_it(engine):
    facts = Facts(description="a suit of 80% silk", materials={"silk": 80.0})
    decision = engine.adjudicate(facts, [SILK_HIGH, SILK_LOW], CONDITION_TEXTS)
    assert decision.conditions_applied == []


def test_a_survivor_that_survived_by_ignorance_does_not_decide(engine):
    """The defect this guard exists for, from the held-out set.

    A cardigan of 53 percent rayon and 47 percent wool had its "85 percent
    or more by weight of wool" candidates correctly eliminated, leaving
    "Containing 30 percent or more by weight of silk" standing - not because
    the goods qualify, but because they say nothing about silk, so the
    condition could not be tested. Deciding on it was a confident wrong
    answer; the elimination is set aside instead.
    """
    wool = "5111196040"
    silk = "5111903000"
    texts = {
        wool: "Woven fabrics of carded wool — Containing 85 percent or more by "
              "weight of wool or of fine animal hair",
        silk: "Woven fabrics of carded wool — Other — Containing 30 percent or more "
              "by weight of silk or silk waste",
    }
    facts = Facts(description="a cardigan of 53% rayon and 47% wool",
                  materials={"rayon": 53.0, "wool": 47.0})
    decision = engine.adjudicate(facts, [wool, silk], texts)
    assert decision.code != silk
    assert decision.conditions_applied == []


def test_an_untestable_condition_never_removes_a_candidate(engine):
    """Nothing is known about silk, so nothing about silk can be concluded."""
    facts = Facts(description="a cotton suit", materials={"cotton": 100.0})
    decision = engine.adjudicate(facts, [SILK_HIGH, SILK_LOW], CONDITION_TEXTS)
    assert decision.conditions_applied == []


# ------------------------------- what reranking stage 2 can and cannot change

def test_the_decision_does_not_depend_on_the_order_of_the_candidates(engine):
    """The engine is order-invariant for a **fixed** candidate set.

    Every branch that returns a code either has one candidate to return
    (``candidates[0]`` after a length check) or picks by a rule that reads
    the whole set (``max`` for the numerical-order tie-break).

    Read what this does and does not establish. It was first written down as
    proof that "a probabilistic reranker may reorder what the adjudicator
    sees without being able to change what it decides", and that does not
    follow. The adjudicator never receives the pool - it receives
    ``classify()``'s first ``k`` distinct codes, taken after reranking, so a
    reranker changes the *set* and not only its order. Measured, the free
    reranker changed the top-5 set on 88.4% of 500 cases and the decided
    code on 3 of the 5 both configurations answered. See
    ``pipeline/rerank.py``.

    What the invariant below is good for is narrower and still worth having:
    the engine cannot be made to answer differently by presenting the same
    provisions in a different order, so its decisions are reproducible and
    its rule paths auditable.
    """
    from itertools import permutations

    facts = Facts(description="a knitted shirt of 60% cotton and 40% polyester",
                  materials={"cotton": 60.0, "polyester": 40.0})
    decisions = {
        engine.adjudicate(facts, list(order), TEXTS).code
        for order in permutations([COTTON, MAN_MADE])
    }
    assert len(decisions) == 1

    # And with a candidate set the rules cannot settle, every ordering still
    # escalates - the shortlist differs, the outcome does not.
    unsettled = Facts(description="a steel bracket", materials={})
    outcomes = {
        (d.code, d.escalated)
        for order in permutations([COTTON, MAN_MADE, "7326908688"])
        for d in [engine.adjudicate(unsettled, list(order), TEXTS)]
    }
    assert outcomes == {(None, True)}


def test_the_escalation_shortlist_is_what_reranking_improves(engine):
    """The shortlist *is* order-dependent, and that is the point.

    97.5% of cases escalate, so the ranked shortlist handed to a human is
    the system's main output. ``top5@6`` scores it. A reranker that cannot
    move top-1 can still move this.
    """
    facts = Facts(description="a steel bracket", materials={})
    first = engine.adjudicate(facts, [COTTON, MAN_MADE], TEXTS)
    second = engine.adjudicate(facts, [MAN_MADE, COTTON], TEXTS)
    assert first.escalated and second.escalated
    assert first.shortlist != second.shortlist


def test_a_survivor_from_another_heading_does_not_decide(engine):
    """The second half of "surviving by ignorance is not qualifying".

    A women's coat of 70% polyester and 30% wool had four chapter 62
    candidates each requiring 36 percent wool or more. All four were
    correctly eliminated - and the survivor was 6405.20.6060, "Other
    footwear, with soles and uppers of wool felt", which states no threshold
    at all. Neither failed nor untested, it won, and the engine decided a
    coat was footwear under GRI 1 with a citation attached.

    A provision that merely has no condition was never competing on these
    terms. The elimination has to be between siblings of one heading, which
    is the GRI 6 comparison this rule exists for.
    """
    coat = "6202401510"
    footwear = "6405206060"
    texts = {
        coat: "Chapter 62 - Women's overcoats - Containing 36 percent or more by "
              "weight of wool or fine animal hair",
        footwear: "Chapter 64 - Other footwear - With soles and uppers of wool felt",
    }
    facts = Facts(description="a women's knee-length coat of 70% polyester and 30% wool",
                  materials={"polyester": 70.0, "wool": 30.0})
    decision = engine.adjudicate(facts, [coat, footwear], texts)
    assert decision.code != footwear
    assert decision.conditions_applied == []


def test_notes_are_cited_in_chapter_order_not_candidate_order(engine):
    """The trace's first authority must not depend on how retrieval ranked.

    Week 4 scores explanations against these traces, so the same decision
    over the same notes has to name them in the same order whichever way
    the proposer happened to sort its candidates.
    """
    facts = Facts(description="woven fabrics laminated with plastics of chapter 39",
                  materials={})
    forward = engine.adjudicate(facts, [COTTON, "3921905050"], TEXTS)
    backward = engine.adjudicate(facts, ["3921905050", COTTON], TEXTS)
    assert forward.notes_cited == backward.notes_cited


def test_a_section_xi_note_cannot_classify_outside_section_xi(engine):
    """Note 2(A) governs chapters 50 to 63. It cannot reach chapter 64.

    A woman's 100% nylon lace dress with a 100% polyester slip gives two
    materials tied at 100, so the rule reaches its "last in numerical order"
    tie-break - and ``max`` over a candidate set spanning chapters returned
    6405.20.6060, "Other footwear, with soles and uppers of wool felt". The
    engine cited a textile note to put a dress in chapter 64, in the shipped
    configuration with no reranker involved.
    """
    dress = "6114901030"
    footwear = "6405206060"
    texts = {
        dress: "Section XI - Chapter 61 - Other garments, knitted - Of other textile "
               "materials - of nylon and polyester",
        footwear: "Section XII - Chapter 64 - Other footwear - With uppers of textile "
                  "materials - of nylon",
    }
    facts = Facts(description="a woman's 100% nylon lace knit dress with a 100% polyester slip",
                  materials={"nylon": 100.0, "polyester": 100.0})
    decision = engine.adjudicate(facts, [dress, footwear], texts)
    assert decision.code != footwear
    # And with nothing inside Section XI to choose between, the note does not fire.
    only_outside = engine.adjudicate(facts, [footwear], {footwear: texts[footwear]})
    assert "Section XI Note 2(A)" not in only_outside.rule_path


# ------------------------------------- mechanisms the ablations switch off

def test_an_unknown_mechanism_is_refused(engine):
    """A typo in an ablation config would otherwise silently measure the
    full engine and report it as an ablation."""
    with pytest.raises(ValueError, match="unknown mechanisms"):
        GRIEngine(engine.exclusions, enabled=frozenset({"note_chain", "typo"}))

    # And a subtraction, which is how the ablations are written. Set
    # difference is silent: MECHANISMS - {"note-chain"} is MECHANISMS, so a
    # hyphen for an underscore would measure the full engine and report it
    # as an ablation - indistinguishable from the two rows that genuinely
    # come out identical to the baseline.
    with pytest.raises(ValueError, match="unknown mechanisms"):
        without_mechanisms("note-chain")
    assert without_mechanisms("note_chain") == frozenset({"note_2a", "conditions"})


def test_switching_off_the_note_chain_removes_citations_not_decisions(engine):
    """What the note-chain ablation can possibly show, stated as a test.

    ``_excluded`` reports the exclusions in scope rather than acting on
    them, deliberately - dropping a candidate on an unevaluated condition is
    the confident-but-wrong behaviour the project exists to avoid. So the
    note-chain contributes citations and nothing else, and its ablation row
    belongs in the explanation scores rather than the accuracy table.
    """
    # The running example, so a decision *is* made and the invariance half
    # of this test has something to compare. An earlier version used facts
    # with no composition, where both configurations escalated and the
    # assertion compared (None, ['GRI 1']) with itself.
    facts = Facts(
        description="a knitted shirt of 60% cotton and 40% polyester, laminated "
                    "with plastics of chapter 39",
        materials={"cotton": 60.0, "polyester": 40.0},
    )
    full = engine.adjudicate(facts, [COTTON, MAN_MADE], TEXTS)
    stripped = GRIEngine(
        engine.exclusions, enabled=without_mechanisms("note_chain")
    ).adjudicate(facts, [COTTON, MAN_MADE], TEXTS)

    assert full.code == COTTON and not full.escalated      # it decided
    assert stripped.code == COTTON                          # and decided the same
    assert stripped.rule_path == full.rule_path
    # Only the exclusion citations go. Note 2(A)'s own citations stay,
    # because note_chain governs the exclusion graph and not the note that
    # actually decided this case.
    assert [n for n in full.notes_cited if "Note 1" in n] != []
    assert [n for n in stripped.notes_cited if "Note 1" in n] == []
    assert "Section XI Note 2(A)" in stripped.notes_cited


def test_switching_off_note_2a_changes_the_decision(engine):
    """Unlike the note-chain, this one does decide things.

    The running example: 60% cotton against 40% polyester is settled by the
    predominant-weight test under GRI 1. Without it the case falls through
    to GRI 3(b) and escalates.
    """
    facts = Facts(description="a knitted shirt of 60% cotton and 40% polyester",
                  materials={"cotton": 60.0, "polyester": 40.0})
    full = engine.adjudicate(facts, [COTTON, MAN_MADE], TEXTS)
    stripped = GRIEngine(
        engine.exclusions, enabled=without_mechanisms("note_2a")
    ).adjudicate(facts, [COTTON, MAN_MADE], TEXTS)

    assert full.code == COTTON and not full.escalated
    assert stripped.escalated and stripped.code is None
    # It falls through 2(b) and 3(a) and stops at essential character.
    assert stripped.rule_path[-1] == "GRI 3(b)"


def test_switching_off_conditions_stops_threshold_elimination(engine):
    facts = Facts(description="a suit of 40% silk and 60% cotton",
                  materials={"silk": 40.0, "cotton": 60.0})
    full = engine.adjudicate(facts, [SILK_HIGH, SILK_LOW], CONDITION_TEXTS)
    stripped = GRIEngine(
        engine.exclusions, enabled=without_mechanisms("conditions")
    ).adjudicate(facts, [SILK_HIGH, SILK_LOW], CONDITION_TEXTS)

    assert full.conditions_applied and full.code == SILK_LOW
    assert stripped.conditions_applied == []

    # And what it decides instead is the case for keeping the mechanism.
    # Without the threshold check, GRI 3(a) strikes SILK_LOW as residual -
    # its own segment is "Other" - and the engine answers "Containing 70
    # percent or more by weight of silk" for a suit that is 40% silk. The
    # layer decides nothing on the current corpus and still prevents a
    # confidently wrong answer the moment retrieval offers a threshold pair.
    assert stripped.code == SILK_HIGH
    assert "GRI 3(a)" in stripped.rule_path
