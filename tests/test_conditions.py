"""The KAG layer: provision text read as arithmetic.

Most of these are regressions rather than specifications. The parser was
measured against all 980 lines in the schedule that state a percentage, and
every case below is text that defeated an earlier version of it - usually
silently, by producing a confident condition with the comparison reversed.
"""

from __future__ import annotations

import pytest

from hsclass.pipeline.conditions import (
    Condition,
    conditions_in,
    consistent_conditions,
    evaluate,
)


def triples(text: str) -> list[tuple[str, str, float]]:
    return [(c.attribute, c.operator, c.value) for c in conditions_in(text)]


# ------------------------------------------------------------- the comparison

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The schedule's own phrasings, one per comparison.
        ("Containing 85 percent or more by weight of wool", [("wool", ">=", 85.0)]),
        ("Containing less than 8 percent by weight of nickel", [("nickel", "<", 8.0)]),
        ("Containing over 45 percent by weight of butterfat", [("butterfat", ">", 45.0)]),
        ("Containing 0.5 percent or less by weight of butterfat",
         [("butterfat", "<=", 0.5)]),
        ("consisting of not less than 17.5 percent by weight of sodium caseinate",
         [("sodium caseinate", ">=", 17.5)]),
    ],
)
def test_the_comparison_the_text_states(text, expected):
    assert triples(text) == expected


def test_not_over_does_not_become_over():
    """The defect that made this file necessary.

    With only "over" in the vocabulary, "not over" matched its tail and the
    condition came out inverted - a confident wrong answer, on the roughly
    100 lines of chapter 4 that phrase a butterfat ceiling this way.
    """
    assert triples("Containing not over 6 percent by weight of butterfat") == [
        ("butterfat", "<=", 6.0)
    ]
    assert triples("Of a fat content, by weight, not exceeding 1 percent") == [
        ("fat", "<=", 1.0)
    ]


def test_a_range_is_two_conditions_not_one_reversed():
    """"but not over" opens a second bound; it is not a suffix on the first.

    Read as a suffix, "over 6 percent but not over 35 percent" parsed as a
    single condition, `<= 6` - wrong bound and wrong direction at once.
    """
    assert triples("Containing over 6 percent but not over 35 percent by weight of butterfat") == [
        ("butterfat", ">", 6.0),
        ("butterfat", "<=", 35.0),
    ]
    assert triples(
        "Of a fat content, by weight, exceeding 1 percent but not exceeding 6 percent"
    ) == [("fat", ">", 1.0), ("fat", "<=", 6.0)]


def test_a_bare_percentage_states_no_comparison():
    assert conditions_in("Containing 85 percent by weight of wool") == []


# -------------------------------------------------------------- the attribute

def test_the_attribute_can_precede_the_number():
    """The largest unparsed shape in the schedule, before it was read."""
    assert triples("Having a moisture content by weight over 50 percent") == [
        ("moisture", ">", 50.0)
    ]


def test_the_attribute_can_follow_with_no_preposition():
    assert triples("Containing by weight 99 percent or more lactose") == [
        ("lactose", ">=", 99.0)
    ]


def test_the_attribute_is_the_words_touching_its_anchor():
    """Read from the far end, each of these loses the attribute or invents one."""
    assert triples("containing over 65 percent by dry weight of sugar described in note 2") == [
        ("sugar", ">", 65.0)
    ]
    assert triples("A milkfat content, by weight of the dry matter, of 5 percent or more") == [
        ("milkfat", ">=", 5.0)
    ]


def test_a_threshold_with_no_attribute_yields_nothing():
    """"Not more than 25 percent by weight" - of what? The line does not say.

    The attribute is in some parent provision, and choosing which parent
    governs is a judgment, not a parse. No condition is the honest answer.
    """
    assert conditions_in("Not more than 25 percent by weight") == []


# ------------------------------------------------------------------ the basis

@pytest.mark.parametrize(
    ("text", "basis"),
    [
        ("Containing 85 percent or more by weight of wool", "weight"),
        ("Containing 65 percent or more by dry weight of sugar", "dry_weight"),
        ("an alcoholic strength by volume of over 14 percent of alcohol", "volume"),
        ("with an ad valorem rate of less than 15 percent of duty", "value"),
    ],
)
def test_what_the_percentage_is_measured_on(text, basis):
    assert [c.basis for c in conditions_in(text)] == [basis]


def test_only_a_weight_share_is_comparable_with_the_facts():
    """The facts carry shares of the product by weight and nothing else.

    A share of the dry matter is a different quantity: 65 percent of the dry
    matter is not 65 percent of the goods. Comparing them would answer
    confidently from a unit error.
    """
    dry = Condition("sugar", ">=", 65.0, basis="dry_weight")
    assert dry.satisfied_by({"sugar": 70.0}) is None
    assert Condition("sugar", ">=", 65.0).satisfied_by({"sugar": 70.0}) is True


# ------------------------------------------------------------- what cannot be

def test_conflicting_comparisons_are_not_guessed_between():
    """Text this parser has not understood yields nothing, not a coin flip."""
    assert conditions_in("over 50 percent or less by weight of wool") == []


def test_an_unsatisfiable_group_is_dropped_not_evaluated():
    """1901.90.28.00 states both bounds, about different parts of the mixture.

    Each phrase is read correctly and the flattened set is impossible, which
    is the signal that it belongs to components rather than to the product.
    Evaluated as written it would reject every product on earth.
    """
    text = (
        "whey solids containing over 5.5 percent by weight of butterfat, and dried "
        "whey any of which contain 5.5 percent or less by weight of butterfat"
    )
    assert len(conditions_in(text)) == 2
    assert consistent_conditions(text) == []
    assert evaluate(text, {"butterfat": 9.0}) is None


# --------------------------------------------------------------- the decision

def test_evaluate_separates_untested_from_failed():
    """The distinction the whole layer turns on.

    A condition on a material the description never mentioned has not
    failed. Returning False for it would drop a candidate the goods may well
    belong to - confidently, and on no evidence.
    """
    silk = "Containing 70 percent or more by weight of silk or silk waste"
    assert evaluate(silk, {"silk": 80.0}) is True
    assert evaluate(silk, {"silk": 60.0}) is False
    assert evaluate(silk, {"cotton": 100.0}) is None
    assert evaluate("Of cotton", {"cotton": 100.0}) is None


def test_every_stated_condition_must_hold():
    text = "Containing over 6 percent but not over 35 percent by weight of butterfat"
    assert evaluate(text, {"butterfat": 20.0}) is True
    assert evaluate(text, {"butterfat": 40.0}) is False
    assert evaluate(text, {"butterfat": 3.0}) is False


def test_the_attribute_matches_a_fuller_name_in_the_facts():
    """"silk or silk waste" is one provision; the goods say "silk noil"."""
    assert evaluate("Containing 70 percent or more by weight of silk", {"silk noil": 90.0}) is True
