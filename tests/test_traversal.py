"""Hierarchical traversal - the walk GRI 6 requires."""

from __future__ import annotations

import pytest

from hsclass.pipeline.traversal import (
    HierarchicalClassifier,
    LevelIndex,
    candidates_at_every_level,
)
from hsclass.retrieval.embedder import HashingEmbedder
from hsclass.retrieval.index import Candidate, VectorIndex

#: A miniature nomenclature: two headings, one of which skips the six-digit
#: level entirely - which the real schedule does whenever a tariff line has a
#: single statistical breakout.
CANDIDATES = [
    Candidate("6109", "T-shirts, singlets, tank tops, knitted or crocheted"),
    Candidate("610910", "T-shirts knitted - of cotton"),
    Candidate("6109100012", "T-shirts knitted - of cotton - men's"),
    Candidate("6109100027", "T-shirts knitted - of cotton - other"),
    Candidate("610990", "T-shirts knitted - of other textile materials"),
    Candidate("6109901000", "T-shirts knitted - of man-made fibres"),
    Candidate("5204", "Cotton sewing thread"),
    Candidate("5204110000", "Cotton sewing thread - containing 85 percent or more cotton"),
]


@pytest.fixture
def levels():
    embedder = HashingEmbedder()
    index = VectorIndex.build(CANDIDATES, embedder, revision="TESTREV")
    return LevelIndex(index), embedder


def test_children_are_found_by_prefix(levels):
    index, _ = levels
    assert sorted(index.index.candidates[r].code for r in index.children("6109")) == [
        "610910", "610990"
    ]


def test_a_level_the_schedule_skips_is_stepped_over(levels):
    """5204 prints no six- or eight-digit line, only the ten-digit one.

    A walk that assumes a fixed four-then-six-then-eight step strands itself
    on the heading and never reaches a classifiable code.
    """
    index, _ = levels
    assert [index.index.candidates[r].code for r in index.children("5204")] == ["5204110000"]


def test_a_terminal_code_has_no_children(levels):
    index, _ = levels
    assert index.children("6109100012") == []


def test_only_one_heading_level_is_ever_compared(levels):
    """GRI 6: subheadings are comparable only at the same level.

    The walk ranks 6109's own subheadings against each other, never against
    a subheading belonging to a different heading.
    """
    index, _ = levels
    children = index.children("6109")
    chapters = {index.index.candidates[r].code[:4] for r in children}
    assert chapters == {"6109"}


def test_the_walk_reaches_a_full_length_code(levels):
    index, embedder = levels
    classifier = HierarchicalClassifier(index, embedder, k=3, beam=3)
    ranked, walks = classifier.classify("knitted cotton t-shirt")
    assert ranked, "the walk returned nothing"
    assert all(len(code) in (4, 6, 8, 10) for code in ranked)
    # The path is kept so the decision can be explained afterwards.
    assert walks[0].steps[0].level == 4
    assert walks[0].steps[-1].code == walks[0].code


def test_each_step_records_how_many_it_chose_between(levels):
    """The narrowing is the token-budget argument, so it has to be measurable."""
    index, embedder = levels
    classifier = HierarchicalClassifier(index, embedder, k=3, beam=3)
    _, walks = classifier.classify("knitted cotton t-shirt")
    steps = walks[0].steps
    assert steps[0].considered == 2  # two headings in this miniature schedule
    assert all(step.considered >= 1 for step in steps)


def test_the_classifier_never_declines(levels):
    index, embedder = levels
    ranked, escalated = HierarchicalClassifier(index, embedder)("anything at all")
    assert escalated is False and list(ranked)


def test_candidates_cover_every_level_not_just_the_leaves(tree):
    """The flat baseline indexes only terminal lines; the walk needs the rest.

    It chooses a heading before it has any business looking at a subheading.
    """
    codes = {c.code for c in candidates_at_every_level(tree)}
    assert "6109" in codes        # the heading
    assert "610990" in codes      # a six-digit subheading
    assert "61091000" in codes    # an eight-digit tariff line
    assert "6109100012" in codes  # a ten-digit statistical line
