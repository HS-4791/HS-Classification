"""Stage 4's only job is to never justify what the rules did not decide.

So most of these tests are on the gate, not on the prose. The prose is a
convenience; the gate is the architecture.
"""

import json

import pytest

from hsclass.obs.cost import Usage
from hsclass.pipeline.explain import (
    Explainer,
    authorities_of,
    quoted_text,
    render,
    unsupported,
)
from hsclass.pipeline.gri import Decision

TEXTS = {
    "6110303020": "Sweaters, pullovers and similar articles — Of man-made fibres — "
                  "Women's or girls'",
    "6110201020": "Sweaters, pullovers and similar articles — Of cotton",
    # The nomenclature cross-references itself, and the renderer is allowed to
    # quote this text, so the gate has to accept the heading inside it.
    "6114300000": "Other garments, knitted or crocheted — Of man-made fibres, "
                  "other than those of heading 6109",
}


def decided(**kwargs) -> Decision:
    base = dict(
        code="6110303020",
        rule_path=["GRI 1", "Section XI Note 2(A)", "GRI 6"],
        notes_cited=["Chapter 61 Note 5", "Section XI Note 2(A)"],
        reason="man-made fibres predominate by weight",
    )
    return Decision(**(base | kwargs))


class StubTransport:
    """Returns a fixed body, and records what it was asked."""

    def __init__(self, content: str):
        self.content = content
        self.prompts: list[str] = []

    def complete(self, instructions: str, description: str):
        self.prompts.append(description)
        return self.content, Usage("gpt-4o-mini", 100, 20)


def says(explanation: str) -> str:
    return json.dumps({"explanation": explanation})


# --------------------------------------------------------- the renderer alone


def test_rendering_passes_its_own_gate():
    decision = decided()
    assert unsupported(render(decision, TEXTS), decision, TEXTS) == []


def test_rendering_passes_its_own_gate_when_escalated():
    decision = Decision(
        None,
        rule_path=["GRI 1", "GRI 2(b)", "GRI 3(a)", "GRI 3(b)"],
        notes_cited=["Chapter 61 Note 5"],
        escalated=True,
        shortlist=["6110303020", "6110201020"],
        reason="essential character decides between these, and that is a judgment call",
    )
    text = render(decision, TEXTS)
    assert unsupported(text, decision, TEXTS) == []
    assert "not classified automatically" in text
    assert "6110201020" in text


def test_rendering_survives_a_note_label_the_oracle_normalises():
    """"Subheading Note 2" reduces to "Note 2" through the citation grammar.

    Both sides of the gate go through that same grammar, so the renderer's
    own citation must still be accepted. Comparing a raw label against a
    parsed one would reject it, and stage 4 would silently fall back to the
    deterministic text it had just refused.
    """
    decision = decided(notes_cited=["Section XI Subheading Note 2"])
    assert unsupported(render(decision, TEXTS), decision, TEXTS) == []


def test_rendering_may_quote_a_cross_reference_in_the_schedule_text():
    decision = decided(
        code="6114300000",
        rule_path=["GRI 1"],
        notes_cited=[],
        reason="a single heading describes the goods",
    )
    text = render(decision, TEXTS)
    assert "heading 6109" in text
    # Quoting a cross-reference is not relying on it. Without ``quoted`` the
    # gate is right to object: holding the schedule text of every candidate
    # let a decision on chicken cuts cite heading 0105, live poultry.
    assert unsupported(text, decision, TEXTS) == ["heading 6109"]
    assert unsupported(
        text, decision, TEXTS, quoted=quoted_text(decision, TEXTS)
    ) == []


def test_rendering_quotes_the_conditions_that_eliminated_provisions():
    decision = decided(
        conditions_applied=["Containing 70 percent or more by weight of silk"]
    )
    text = render(decision, TEXTS)
    assert "70 percent or more by weight of silk" in text
    assert unsupported(text, decision, TEXTS) == []


def test_rendering_needs_no_schedule_text():
    decision = decided()
    assert unsupported(render(decision), decision) == []


# ------------------------------------------------------------------- the gate


def test_authorities_of_holds_what_the_decision_records():
    held = authorities_of(decided(), TEXTS)
    assert "1" in held.gris and "6" in held.gris
    assert "Section XI Note 2(A)" in held.notes
    assert "6110303020" in held.headings


def test_gate_catches_an_invented_gri():
    decision = decided()
    invented = unsupported("The goods are classified under GRI 3(b).", decision, TEXTS)
    assert invented == ["GRI 3(b)"]


def test_gate_catches_a_rule_applied_in_words_but_never_cited():
    """A citation gate alone cannot see this, and the first run found it.

    "Essential character" *is* GRI 3(b). Prose that applies it to a case the
    engine resolved at GRI 1 has fabricated the reasoning while keeping its
    citations clean - and the importer reads the reasoning.
    """
    decision = decided()
    assert unsupported(
        "These goods take their essential character from the knitted fabric.",
        decision,
        TEXTS,
    ) == ["essential character, which is GRI 3(b)"]


def test_gate_catches_both_the_citation_and_the_test_it_names():
    decision = decided()
    assert unsupported(
        "The goods are classified under GRI 3(b), on essential character.",
        decision,
        TEXTS,
    ) == ["GRI 3(b)", "essential character, which is GRI 3(b)"]


def test_gate_allows_an_implied_test_the_decision_actually_reached():
    decision = Decision(
        None,
        rule_path=["GRI 1", "GRI 2(b)", "GRI 3(a)", "GRI 3(b)"],
        escalated=True,
        reason="essential character decides between these, and that is a judgment call",
    )
    assert unsupported(
        "The choice turns on essential character, so the case is referred to a "
        "classifier.",
        decision,
        TEXTS,
    ) == []


def test_gate_catches_predominance_by_weight_without_the_note():
    """Section XI Note 2(A) is the engine's load-bearing rule.

    Prose asserting that a material predominates by weight has applied it,
    and 23 of the engine's 24 held-out answers come from that note - so prose
    reaching for it on a case the note never touched is the error that
    matters most here.
    """
    decision = decided(rule_path=["GRI 1"], notes_cited=[], reason="")
    assert unsupported(
        "Cotton predominates by weight, so that subheading applies.", decision, TEXTS
    ) == ["predominance by weight, which is Section XI Note 2(A)"]


def test_implied_tests_do_not_fire_on_a_quoted_threshold():
    """The schedule's own conditions talk about weight constantly."""
    decision = decided(
        rule_path=["GRI 1"],
        notes_cited=[],
        reason="",
        conditions_applied=["Containing 70 percent or more by weight of silk"],
    )
    assert unsupported(render(decision, TEXTS), decision, TEXTS) == []


def test_gate_catches_an_invented_note():
    decision = decided()
    assert unsupported(
        "Chapter 62 Note 8 excludes these goods.", decision, TEXTS
    ) == ["Chapter 62 Note 8"]


def test_gate_allows_the_heading_above_the_decided_code():
    """Heading 6110 is the correct name for the heading of 6110.30.3020."""
    decision = decided()
    assert unsupported("Classified in heading 6110.", decision, TEXTS) == []
    assert unsupported("Classified in subheading 6110.30.", decision, TEXTS) == []


def test_gate_catches_a_neighbouring_statistical_suffix():
    """6110.30.3025 is not 6110.30.3020, and inventing a suffix is the error."""
    decision = decided()
    assert unsupported("Classified in subheading 6110.30.3025.", decision, TEXTS) == [
        "heading 6110303025"
    ]


def test_gate_is_quiet_on_prose_that_cites_nothing():
    decision = decided()
    assert unsupported("These are women's knitted pullovers.", decision, TEXTS) == []


# --------------------------------------------------------------- the explainer


def test_model_prose_is_used_when_it_stays_within_the_decision():
    transport = StubTransport(says(
        "Under GRI 1 and GRI 6, these pullovers fall in subheading 6110.30.3020, "
        "because Section XI Note 2(A) assigns goods to the material that "
        "predominates by weight."
    ))
    result = Explainer(transport, TEXTS).explain(decided(), "women's acrylic pullovers")
    assert result.source == "model"
    assert result.rejected == []
    assert result.usage is not None


def test_model_prose_is_refused_when_it_invents_a_rule():
    transport = StubTransport(says(
        "These goods are classified under GRI 3(b) on their essential character."
    ))
    result = Explainer(transport, TEXTS).explain(decided(), "women's acrylic pullovers")
    assert result.source == "rules"
    assert result.rejected == [
        "GRI 3(b)", "essential character, which is GRI 3(b)"
    ]
    # The text that ships is the deterministic one, and it is faithful.
    assert result.text == render(decided(), TEXTS)


def test_refused_prose_never_reaches_the_caller():
    fabrication = "Classified under GRI 4 by analogy with heading 9503."
    transport = StubTransport(says(fabrication))
    result = Explainer(transport, TEXTS).explain(decided(), "a toy")
    assert fabrication not in result.text
    assert sorted(result.rejected) == [
        "GRI 4", "heading 9503", "the goods-most-akin rule, which is GRI 4"
    ]


@pytest.mark.parametrize("content", ["not json at all", json.dumps({}),
                                     json.dumps({"explanation": "   "})])
def test_an_unusable_response_falls_back_rather_than_failing(content):
    result = Explainer(StubTransport(content), TEXTS).explain(decided(), "pullovers")
    assert result.source == "rules"
    assert result.rejected


def test_no_transport_is_a_supported_configuration():
    result = Explainer(texts=TEXTS).explain(decided(), "pullovers")
    assert result.source == "rules"
    assert result.rejected == []
    assert result.usage is None
    assert "6110303020" in result.text


def test_the_model_is_never_shown_a_gold_answer():
    """It sees the goods and the decision. There is nowhere to leak an answer."""
    transport = StubTransport(says("Classified under GRI 1."))
    Explainer(transport, TEXTS).explain(decided(), "women's acrylic pullovers")
    prompt = transport.prompts[0]
    assert "women's acrylic pullovers" in prompt
    assert "Section XI Note 2(A)" in prompt
    assert "gold" not in prompt.lower()


def test_an_escalated_decision_explains_the_refusal():
    decision = Decision(
        None, rule_path=["GRI 1"], escalated=True, shortlist=["6110303020"],
        reason="more than one heading describes the goods and no note separates them",
    )
    text = Explainer(texts=TEXTS).explain(decision, "a garment").text
    assert "referred to a classifier" in text
    assert unsupported(text, decision, TEXTS) == []


# ------------------------------- evasions a cold read found passing the gate
#
# Every case below returned [] from the first version of the gate. It parsed
# citations in one format, so a fabrication in any other format was invisible,
# and the prompt and the renderer both taught the model the format that
# evaded it. These are regression tests for a claim, not for a function.


def test_a_bare_tariff_code_is_a_citation():
    """The first gate needed the word "heading" before the digits.

    So prose naming 6405.20.6060 - the dress-as-footwear answer this engine
    was corrected for twice - shipped as model prose with nothing rejected.
    """
    decision = decided(rule_path=["GRI 1"], notes_cited=[], reason="")
    for prose in (
        "The goods fall in 6405.20.6060, other footwear with uppers of wool felt.",
        "HTS 6110.20.1010 provides for these goods.",
        "Tariff item 9503.00.0090 covers the article.",
    ):
        assert unsupported(prose, decision, TEXTS), prose


def test_an_undotted_neighbouring_suffix_is_caught():
    r"""``\d{4}`` with nothing anchoring its end read 6109100040 as "6109".

    This is the form the project writes internally and shows the model, so
    it was the one format that mattered and the one the tests missed.
    """
    decision = decided(rule_path=["GRI 1"], notes_cited=[], reason="")
    assert unsupported("Classified in subheading 6110303025.", decision, TEXTS) == [
        "heading 6110303025"
    ]


def test_a_rule_named_without_the_token_gri_is_caught():
    decision = decided(rule_path=["GRI 1"], notes_cited=[], reason="")
    for prose in (
        "Under Rule 3(b) the goods take the identity of the dominant component.",
        "Applying the third General Rule, the dominant component governs.",
        "By operation of the General Interpretative Rules, Rule 3(c) applies.",
    ):
        assert unsupported(prose, decision, TEXTS), prose


def test_a_legal_test_in_paraphrase_is_caught():
    """The model does not reach for the textbook phrase. It paraphrases."""
    decision = decided(rule_path=["GRI 1"], notes_cited=[], reason="")
    for prose in (
        "Classification follows the component giving the goods their fundamental "
        "character.",
        "The material imparting the essential nature of the article decides.",
        "Section XI treats the goods as wholly of the predominant textile material.",
        "The predominant fibre decides the subheading.",
        "No heading describes them, so they go with the goods they most closely "
        "resemble.",
        "Where two provisions equally merit consideration, the later prevails.",
    ):
        assert unsupported(prose, decision, TEXTS), prose


def test_predominant_is_matched_as_well_as_predominating():
    """"predominant" is predomin+ant; a `predominat` stem never matched it.

    The pattern only caught the renderer's own "predominating", so the one
    phrasing a shipper or a model actually uses walked through.
    """
    decision = decided(rule_path=["GRI 1"], notes_cited=[], reason="")
    for word in ("predominant", "predominates", "predominating", "predominance"):
        assert unsupported(
            f"The cotton {word} by weight.", decision, TEXTS
        ) == ["predominance by weight, which is Section XI Note 2(A)"], word


def test_a_refusal_must_refuse():
    """Asked about an escalated apron, the model wrote that "the
    classification will be determined by the most appropriate heading" -
    presenting an unresolved case as all but resolved. The refusal is the
    product."""
    decision = Decision(
        None, rule_path=["GRI 1"], escalated=True, shortlist=["6212105020"],
        reason="more than one heading describes the goods and no note separates them",
    )
    assert "a refusal written as though the goods had been classified" in unsupported(
        "These goods are knitted garments of textile material.", decision, TEXTS
    )
    assert unsupported(
        "The case is referred to a classifier.", decision, TEXTS
    ) == []


def test_a_refusal_in_the_models_own_words_is_accepted():
    """A quarter of the first run's refusals were this error, not a fabrication.

    Both of these hand the case over as plainly as the renderer does, and
    both were counted as answers because the pattern wanted "referred to"
    with nothing between the words and "determination" rather than
    "determine". That inflated the one rate this stage is judged on.
    """
    decision = Decision(
        None, rule_path=["GRI 1"], escalated=True, shortlist=["6212105020"],
        reason="more than one heading describes the goods and no note separates them",
    )
    for prose in (
        "Multiple headings apply, leading to the conclusion that a classifier "
        "will further determine the appropriate classification.",
        "Since no note separates them, it was necessary to refer the case to a "
        "classifier for a final decision.",
    ):
        assert unsupported(prose, decision, TEXTS) == [], prose


def test_prose_that_answers_an_escalated_case_is_still_refused():
    decision = Decision(
        None, rule_path=["GRI 1"], escalated=True, shortlist=["6212105020"],
        reason="more than one heading describes the goods and no note separates them",
    )
    for prose in (
        "The classification will be determined based on the most appropriate heading.",
        "These goods are classified under the relevant headings for cast iron.",
    ):
        assert unsupported(prose, decision, TEXTS), prose


def test_shorthand_for_a_held_note_is_not_a_fabrication():
    """Refusing correct paraphrase inflated the one rate the claim rests on."""
    decision = decided()
    for prose in (
        "Section XI Note 2(A) assigns the goods by predominant weight.",
        "Note 2(A) to Section XI assigns the goods by predominant weight.",
        "Under note 2(A) the material predominating by weight governs.",
    ):
        assert unsupported(prose, decision, TEXTS) == [], prose


def test_the_wrong_scope_on_a_note_is_still_a_fabrication():
    """Relaxing scope is one-directional: a chapter named wrongly is wrong."""
    assert unsupported("Chapter 61 Note 2(A) applies.", decided(), TEXTS) == [
        "Chapter 61 Note 2(A)"
    ]


def test_a_decision_may_not_cite_a_cross_reference_in_its_own_chain():
    """6,703 of 29,861 provisions name a heading other than their own."""
    decision = decided(code="6114300000", rule_path=["GRI 1"], notes_cited=[],
                       reason="a single heading describes the goods")
    assert unsupported("The goods are classified in heading 6109.", decision, TEXTS) == [
        "heading 6109"
    ]


def test_the_refusal_first_prompt_differs_and_is_not_the_default():
    """Measured at 0% refusals and 1.5% rule completeness. Kept, not used."""
    from hsclass.pipeline.explain import INSTRUCTIONS, REFUSAL_FIRST_INSTRUCTIONS

    assert REFUSAL_FIRST_INSTRUCTIONS != INSTRUCTIONS
    assert "could not be classified automatically" in REFUSAL_FIRST_INSTRUCTIONS
    assert "could not be classified automatically" not in INSTRUCTIONS
    assert Explainer(StubTransport(says("x"))).instructions == INSTRUCTIONS
