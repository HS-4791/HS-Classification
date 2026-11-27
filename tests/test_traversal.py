"""Hierarchical traversal - the walk GRI 6 requires."""

from __future__ import annotations

import numpy as np
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


# ------------------- pruning: siblings must not evict one another


def test_pruning_per_parent_keeps_every_surviving_subheading(levels):
    """Heading 6109 has two subheadings; both must reach the bottom.

    With ``prune_by="heading"`` a heading keeps its best N whole paths, so
    one subheading's tariff lines can fill every slot and evict its sibling.
    Measured on the held-out 2,000 that cost 12.6 points of pool recall: the
    gold heading survived 82.50% of the time and the gold subheading only
    46.65%.
    """
    index, embedder = levels
    walk = HierarchicalClassifier(
        index, embedder, k=5, per_heading=2, prune_by="parent"
    )
    reached = {
        w.code[:6] for w in walk.walks("cotton t-shirt") if w.code.startswith("6109")
    }
    assert reached == {"610910", "610990"}


def test_pruning_per_heading_can_lose_a_sibling_subheading(levels):
    """The defect the parent mode fixes, pinned so it cannot come back quietly.

    Heading 6109 has two subheadings, one with two tariff lines and one with
    one. At per_heading=2 the heading gets two slots for all three paths, and
    610910's two lines take both - so 610990 disappears even though it
    survived the descent to six digits. Parent mode gives each subheading its
    own two slots and all three paths live.
    """
    index, embedder = levels
    walk = HierarchicalClassifier(
        index, embedder, k=5, per_heading=2, prune_by="heading"
    )
    reached = {
        w.code[:6] for w in walk.walks("cotton t-shirt") if w.code.startswith("6109")
    }
    assert reached == {"610910"}


def test_an_unknown_prune_mode_is_refused(levels):
    index, embedder = levels
    with pytest.raises(ValueError, match="prune_by"):
        HierarchicalClassifier(index, embedder, prune_by="subheading")


class SwappedRankLexical:
    """A lexical index that ranks two siblings the opposite way round.

    Enough to force an exact tie in the fused score without duplicate text:
    reciprocal rank fusion sums ``1/(K+rank)`` over both rankings, so two rows
    whose ranks are swapped between them contribute the same two terms and sum
    to a bit-identical value.
    """

    def __init__(self, scores: np.ndarray) -> None:
        self._scores = scores

    def scores(self, terms, rows):  # noqa: ARG002 - signature set by LevelIndex
        return self._scores[list(rows)]


def test_tied_siblings_are_ranked_by_schedule_order_not_by_the_sort():
    """GRI 6 compares provisions at one level; the tie-break was numpy's.

    ``LevelIndex.rank`` ordered siblings with a default ``argsort``, so an
    exact tie resolved to whatever the sort did - and differently on another
    numpy build. Fused scores tie readily and need no duplicate text to do it,
    which is what this constructs.

    Measured during real walks by ``scripts/measure_tie_exposure.py``, over the
    200 test cases on ``index-levels``: 49,299 ``rank()`` calls, 1,714 holding
    an exact tie (3.48%), 191 of the 200 cases touching at least one, and 39
    ties straddling the cut - those last change which provisions stage 3 is
    offered at all. Fixing this moved one published number, the own-text arm's
    held-out top5@6, by two cases in two thousand.

    The shape is load-bearing and was arrived at by measurement, not by
    argument. A first version put the tie at rows 0 and 1 of 600 and **passed
    with the fix reverted** - quicksort left those two alone, so it proved
    nothing. Whether the default sort disturbs a tied pair depends on where
    the pair sits as much as on how many rows there are: at n=600 rows (0,1)
    and (598,599) keep their order while (10,11) and (300,301) do not. That is
    the introsort pivot pattern, not a size threshold, so there is no rule to
    pick a shape by - this one is pinned by reverting the fix and watching it
    fail.
    """
    n = 600
    first, second = 10, 11
    # One dimension of signal, so similarity is exactly the number planted.
    dense = np.linspace(0.70, 0.10, n).astype(np.float32)
    dense[first], dense[second] = 0.9, 0.8      # `first` leads the dense list
    lexical = np.linspace(0.50, 0.05, n).astype(np.float32)
    lexical[first], lexical[second] = 0.8, 0.9  # `second` leads the lexical one

    vectors = np.zeros((n, 2), dtype=np.float32)
    vectors[:, 0] = dense
    candidates = [Candidate(f"6109{i:06d}", f"line {i}") for i in range(n)]
    index = VectorIndex(candidates, vectors, "test", "TESTREV")
    level = LevelIndex(index, SwappedRankLexical(lexical))

    query = np.array([1.0, 0.0], dtype=np.float32)
    ranked = level.rank(query, list(range(n)), k=5, terms=["anything"])
    top_rows = [row for row, _, _ in ranked]

    # The construction is only meaningful if the two really do tie.
    fused = dict((row, score) for row, score, _ in ranked)
    assert fused[first] == fused[second], (
        f"rows {first} and {second} were supposed to tie in the fused score"
    )
    assert top_rows[:2] == [first, second], (
        f"tied siblings came back as {top_rows[:2]}, not in schedule order. "
        f"Which of two provisions at the same level is preferred is being "
        f"decided by the sort rather than by the schedule."
    )
