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
