"""The index and the vector-only baseline, exercised without downloading a model."""

from __future__ import annotations

import numpy as np
import pytest

from hsclass.pipeline.baseline import VectorOnlyClassifier, candidates_from_tree
from hsclass.retrieval.embedder import HashingEmbedder, get_embedder
from hsclass.retrieval.index import Candidate, VectorIndex

CANDIDATES = [
    Candidate("6109100012", "T-shirts, singlets, tank tops, knitted - of cotton - men's"),
    Candidate("6109901000", "T-shirts, singlets, tank tops, knitted - of man-made fibres"),
    Candidate("0803901045", "Bananas, fresh - plantains"),
    Candidate("3924104000", "Tableware and kitchenware of plastics"),
]


@pytest.fixture
def index():
    embedder = HashingEmbedder()
    return VectorIndex.build(CANDIDATES, embedder, revision="TESTREV"), embedder


def test_vectors_are_normalised_so_a_dot_product_is_a_cosine():
    vectors = HashingEmbedder().encode(["cotton t-shirt", "fresh bananas"])
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0)


def test_the_same_text_always_gives_the_same_vector():
    """Determinism starts here - week 4 compares runs, not tolerances."""
    embedder = HashingEmbedder()
    assert np.array_equal(embedder.encode(["knitted cotton shirt"]),
                          embedder.encode(["knitted cotton shirt"]))


def test_search_ranks_the_closest_line_first(index):
    idx, embedder = index
    hits = idx.search(embedder.encode(["fresh bananas plantains"]), k=2)[0]
    assert hits[0].code == "0803901045"
    assert hits[0].score >= hits[1].score


def test_search_never_returns_more_than_the_index_holds(index):
    idx, embedder = index
    assert len(idx.search(embedder.encode(["anything"]), k=99)[0]) == len(CANDIDATES)


def test_which_of_many_tied_candidates_gets_in_does_not_depend_on_the_sort():
    """A tie wider than k used to be resolved by whatever argpartition did.

    ``np.argpartition`` chooses WHICH k rows it returns, and when the k-th and
    (k+1)-th scores are equal it picks among the tied rows arbitrarily - so the
    stable sort that followed put a reproducible order on an irreproducible
    set. This is not hypothetical: stripping the inherited chain leaves 6,370
    provisions whose own text is the single word "Other", giving
    byte-identical vectors that tie exactly against every query.

    Measured over the 200 test queries against that index, the old selection
    disagreed with a deterministic one on 42 queries at k=5, 46 at k=20 and 60
    at k=50. Against ``index`` and ``index-levels`` it disagreed on 0, because
    neither has a tie straddling the cut - which is why this fix cannot move
    any published pipeline number.
    """
    # 600 provisions that read identically once the chain is stripped, so
    # every one of them scores the same against the query, plus a few real
    # ones. The codes carry the row number so the assertion can name them.
    tied = [Candidate(f"9999{n:06d}", "Other") for n in range(600)]
    distinct = [
        Candidate("0803901045", "Bananas, fresh - plantains"),
        Candidate("3924104000", "Tableware and kitchenware of plastics"),
    ]
    embedder = HashingEmbedder()
    idx = VectorIndex.build(tied + distinct, embedder, revision="TESTREV")

    hits = idx.search(embedder.encode(["Other"]), k=5)[0]
    # All five come from the tie, and they are the five lowest rows in it.
    assert [h.code for h in hits] == [f"9999{n:06d}" for n in range(5)], (
        "the five candidates admitted from a 600-wide tie are not the five "
        "lowest rows, so which ones get in depends on the sort rather than on "
        "the index"
    )
    # And the tie really is a tie, or the assertion above proves nothing.
    assert len({h.score for h in hits}) == 1


def test_the_index_round_trips_through_disk(tmp_path, index):
    idx, _ = index
    idx.save(tmp_path / "index")
    reloaded = VectorIndex.load(tmp_path / "index")
    assert len(reloaded) == len(idx)
    assert reloaded.revision == "TESTREV"
    assert np.array_equal(reloaded.vectors, idx.vectors)


def test_a_vector_and_candidate_count_mismatch_is_rejected():
    with pytest.raises(ValueError, match="one vector per candidate"):
        VectorIndex(CANDIDATES, np.zeros((2, 8), dtype=np.float32), "x", "TESTREV")


def test_the_classifier_returns_distinct_codes_and_never_declines(index):
    idx, embedder = index
    classifier = VectorOnlyClassifier(idx, embedder, k=3)
    ranked, escalated = classifier("knitted cotton t-shirt for men")
    ranked = list(ranked)
    assert escalated is False
    assert len(ranked) == len(set(ranked)) == 3


def test_only_terminal_lines_are_searchable(tree):
    """A heading is not a destination; a classification lands on 8 or 10 digits."""
    candidates = candidates_from_tree(tree)
    codes = {c.code for c in candidates}
    assert "6109" not in codes  # the heading itself
    assert "610910" not in codes  # the subheading
    assert "6109100012" in codes
    assert all(len(c.code) in (8, 10) for c in candidates)


def test_candidates_carry_the_inherited_chain_not_just_their_own_words(tree):
    candidate = next(c for c in candidates_from_tree(tree) if c.code == "6109100012")
    assert "T-shirts" in candidate.text  # from the heading above it
    assert "Of cotton" in candidate.text  # from the subheading
    assert candidate.chapter == "61"


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="unknown embedding backend"):
        get_embedder("not-a-backend")
