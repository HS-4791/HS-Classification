"""BM25 over the schedule, and the fusion that uses it.

The behaviour worth pinning is not "BM25 computes BM25" - it is the two
decisions the measurements forced: that a provision absorbs its descendants'
vocabulary but not their inherited chains, and that a ranking with no signal
in it is dropped from the fusion rather than fused.
"""

from __future__ import annotations

import numpy as np

from hsclass.retrieval.index import Candidate
from hsclass.retrieval.lexical import (
    LexicalIndex,
    lexical_documents,
    reciprocal_rank_fusion,
    terms_in,
)

SCHEDULE = [
    Candidate(code="6110", text="Chapter 61 — Sweaters, pullovers and similar articles"),
    Candidate(code="611030", text="Chapter 61 — Sweaters, pullovers — Of man-made fibres"),
    Candidate(
        code="6110301560",
        text="Chapter 61 — Sweaters, pullovers — Of man-made fibres "
             "— Women's knitted jerseys of acrylic",
    ),
    Candidate(code="5111", text="Chapter 51 — Woven fabrics of carded wool"),
]


def test_a_provision_absorbs_the_words_beneath_it():
    """The product noun lives in the breakouts, not in the heading."""
    documents = dict(
        zip([c.code for c in SCHEDULE], lexical_documents(SCHEDULE), strict=True)
    )
    assert "jerseys" not in SCHEDULE[0].text
    assert "jerseys" in documents["6110"]       # inherited up from the ten-digit line
    assert "acrylic" in documents["611030"]
    assert "jerseys" not in documents["5111"]   # a different heading gains nothing


def test_a_descendant_contributes_its_own_words_only_once():
    """Adding each descendant's whole chain would repeat the parent's words
    once per descendant and swamp the term frequencies with them."""
    documents = lexical_documents(SCHEDULE)
    assert documents[0].lower().count("sweaters") == 1


def test_stop_words_are_dropped():
    assert terms_in("Of other textile materials of cotton") == ["textile", "materials", "cotton"]


def test_a_rarer_word_scores_higher():
    index = LexicalIndex.build(SCHEDULE)
    acrylic = index.scores(terms_in("acrylic"))
    chapter = index.scores(terms_in("chapter"))
    # "chapter" is in every document and discriminates nothing.
    assert acrylic.max() > chapter.max()


def test_scores_can_be_restricted_to_one_parents_children():
    index = LexicalIndex.build(SCHEDULE)
    everything = index.scores(terms_in("wool"))
    subset = index.scores(terms_in("wool"), [3])
    assert subset.shape == (1,)
    assert subset[0] == everything[3]


# ----------------------------------------------------------------- the fusion

def test_fusion_promotes_what_the_other_ranking_missed():
    """The mechanism behind the measured gain, in four documents.

    Dense ranks the right provision last. The lexical ranking puts it
    first - an exact term the embedding did not reward - and fusion lifts it
    from fourth to second, which is what turns heading recall@5 from 40.0%
    into 50.5%.

    Note what this does *not* assert: that a document ranked second by both
    beats one ranked first and last. It does not. 1/(k+rank) is convex, so a
    top rank is worth more than consistency - RRF recovers missed candidates
    rather than rewarding agreement.
    """
    dense = np.array([0.9, 0.8, 0.7, 0.1], dtype=np.float32)
    lexical = np.array([0.0, 0.0, 0.0, 9.0], dtype=np.float32)
    order = np.argsort(-reciprocal_rank_fusion(dense, lexical)).tolist()
    assert order.index(3) == 1
    assert np.argsort(-dense).tolist().index(3) == 3


def test_a_ranking_with_no_signal_is_dropped_not_fused():
    """With every score zero, argsort returns document order.

    Fused in, that order looks like evidence and is not: a description
    sharing no term with any provision would have the lexical half vote for
    whatever happens to be stored first.
    """
    dense = np.array([0.1, 0.9, 0.5], dtype=np.float32)
    nothing = np.zeros(3, dtype=np.float32)
    fused = reciprocal_rank_fusion(dense, nothing)
    dense_only = reciprocal_rank_fusion(dense)
    assert np.allclose(fused, dense_only)
    assert int(np.argmax(fused)) == 1


def test_fusion_of_nothing_at_all_is_not_a_crash():
    assert reciprocal_rank_fusion(np.zeros(3, dtype=np.float32)).tolist() == [0.0, 0.0, 0.0]


def test_tied_scores_share_the_average_of_their_positions():
    """Without this, a tier carrying no information reorders one that does.

    A product description shares a term with only a handful of provisions,
    so on a typical query 85.3% of the 961 headings score exactly zero.
    Breaking those ties by array index moved the fused top-1 heading on 3%
    of queries and the top-5 set on 15% - arbitrariness underneath the
    recall number this index is justified by.
    """
    from hsclass.retrieval.lexical import _average_ranks

    # Three tied at the bottom occupy positions 2, 3 and 4; each gets 3.
    assert _average_ranks(np.array([0.0, 0.0, 0.0, 9.0])).tolist() == [3.0, 3.0, 3.0, 1.0]
    # Distinct scores are ordinary descending ranks.
    assert _average_ranks(np.array([0.7, 0.9, 0.8])).tolist() == [3.0, 1.0, 2.0]


def test_a_tied_tier_cannot_reorder_the_other_ranking():
    """The property the averaging exists for, stated as behaviour.

    Four documents the lexical index cannot separate at all: whatever order
    they are stored in, the fused ranking must be the dense ranking.
    """
    dense = np.array([0.1, 0.9, 0.5, 0.7], dtype=np.float32)
    tied = np.array([2.0, 2.0, 2.0, 2.0], dtype=np.float32)
    fused = np.argsort(-reciprocal_rank_fusion(dense, tied)).tolist()
    assert fused == np.argsort(-dense).tolist()
