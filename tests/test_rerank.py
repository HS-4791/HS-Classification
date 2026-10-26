"""Reranking the walk pool.

The property these pin is narrower than "it ranks well": a reranker may
only *reorder*. If it can add, drop or alter a walk then the pool the
adjudicator sees is no longer the pool the descent produced, and the funnel
that attributes accuracy to each stage stops meaning anything.

The cross-encoder is exercised through an injected stub. That is what the
``encoder`` argument is for - the real model is an 80MB download and its
numbers belong in a measurement script, not in a test suite that has to run
in CI.
"""

from __future__ import annotations

import pytest

from hsclass.pipeline.rerank import CrossEncoderReranker, LexicalReranker
from hsclass.pipeline.traversal import Step, Walk
from hsclass.retrieval.index import Candidate
from hsclass.retrieval.lexical import LexicalIndex

SCHEDULE = [
    Candidate(code="6110", text="Chapter 61 — Sweaters, pullovers and similar articles"),
    Candidate(code="611030", text="Chapter 61 — Sweaters — Of man-made fibres"),
    Candidate(code="611020", text="Chapter 61 — Sweaters — Of cotton"),
    Candidate(code="5111", text="Chapter 51 — Woven fabrics of carded wool"),
]


def walk(code: str, text: str, similarity: float) -> Walk:
    return Walk([Step(len(code), code, text, similarity, 1, similarity)])


@pytest.fixture
def pool() -> list[Walk]:
    return [
        walk("611030", "Of man-made fibres", 0.9),
        walk("611020", "Of cotton", 0.5),
        walk("5111", "Woven fabrics of carded wool", 0.4),
    ]


class StubEncoder:
    """Returns the scores it was given, in the order the pairs arrive."""

    def __init__(self, scores: list[float]) -> None:
        self.scores = scores
        self.seen: list[tuple[str, list[str]]] = []

    def rerank(self, query: str, documents: list[str]):
        self.seen.append((query, list(documents)))
        return self.scores[: len(documents)]


# --------------------------------------------------------- the free reranker

def test_the_lexical_reranker_promotes_an_exact_term(pool):
    """"cotton" is in 611020's document and nowhere else in the pool.

    The cosine ranks it second; the term it shares with the query is what
    lifts it, which is the whole reason a lexical signal is kept at all.
    """
    reranker = LexicalReranker(LexicalIndex.build(SCHEDULE), SCHEDULE)
    ordered = reranker("a cotton sweater", pool)
    assert ordered[0].code == "611020"


def test_the_lexical_reranker_only_reorders(pool):
    reranker = LexicalReranker(LexicalIndex.build(SCHEDULE), SCHEDULE)
    ordered = reranker("a cotton sweater", pool)
    assert sorted(w.code for w in ordered) == sorted(w.code for w in pool)
    assert len(ordered) == len(pool)


def test_a_walk_the_index_does_not_know_leaves_the_order_alone(pool):
    """Scoring it would mean picking a row, and any row is another provision.

    Returning the pool untouched is the honest failure: the ranking is no
    worse than it was, and nothing is scored against the wrong text.
    """
    reranker = LexicalReranker(LexicalIndex.build(SCHEDULE), SCHEDULE)
    unknown = [*pool, walk("9999999999", "not in this index", 0.99)]
    assert reranker("a cotton sweater", unknown) == unknown


def test_one_walk_is_already_in_order(pool):
    reranker = LexicalReranker(LexicalIndex.build(SCHEDULE), SCHEDULE)
    assert reranker("anything", pool[:1]) == pool[:1]
    assert reranker("anything", []) == []


# -------------------------------------------------------- the cross-encoder

def test_the_cross_encoder_reorders_by_its_own_score(pool):
    encoder = StubEncoder([-5.0, 1.0, -9.0])  # the middle pair wins
    reranker = CrossEncoderReranker(encoder=encoder, depth=50)
    ordered = reranker("a cotton sweater", pool)
    assert [w.code for w in ordered] == ["611020", "611030", "5111"]
    # It is shown the deepest step's text, which carries the inherited chain.
    assert encoder.seen == [("a cotton sweater", ["Of man-made fibres", "Of cotton",
                                                  "Woven fabrics of carded wool"])]


def test_only_the_head_is_reranked_and_the_tail_stays_below(pool):
    """``depth`` bounds the cost; it must not be able to lose a candidate.

    The pool averages 212 walks at about 21ms a pair, so reranking all of it
    costs 4.5 seconds. Walks past the depth keep their order, below the
    reranked head.
    """
    encoder = StubEncoder([-9.0, 1.0])
    reranker = CrossEncoderReranker(encoder=encoder, depth=2)
    ordered = reranker("a cotton sweater", pool)
    assert [w.code for w in ordered] == ["611020", "611030", "5111"]
    assert len(encoder.seen[0][1]) == 2  # the third pair was never scored
    assert sorted(w.code for w in ordered) == sorted(w.code for w in pool)


def test_a_pool_shorter_than_two_is_returned_untouched(pool):
    encoder = StubEncoder([1.0])
    reranker = CrossEncoderReranker(encoder=encoder, depth=50)
    assert reranker("anything", pool[:1]) == pool[:1]
    assert encoder.seen == []  # nothing was scored, so nothing was paid for


def test_a_score_for_every_pair_or_an_error(pool):
    """Silently pairing by position would score walks against other texts.

    Verified to fail without ``strict=True`` rather than merely to pass
    with it: removing it turns this into DID NOT RAISE.
    """

    class Short(StubEncoder):
        def rerank(self, query, documents):
            return [1.0]  # one score for three pairs

    reranker = CrossEncoderReranker(encoder=Short([1.0]), depth=50)
    with pytest.raises(ValueError):
        reranker("a cotton sweater", pool)


def test_both_rerankers_match_the_declared_signature():
    """``Reranker`` is the contract a third implementation must meet.

    Checked by signature, not by ``isinstance``. A ``runtime_checkable``
    Protocol only tests that ``__call__`` exists at all, so
    ``isinstance(print, Reranker)`` is True and an implementation taking
    four unrelated arguments would have passed - the test claimed to stop
    the Protocol drifting from the classes it describes and could not.
    """
    import inspect

    from hsclass.pipeline.rerank import Reranker

    expected = inspect.signature(Reranker.__call__)
    for reranker in (
        LexicalReranker(LexicalIndex.build(SCHEDULE), SCHEDULE),
        CrossEncoderReranker(encoder=StubEncoder([1.0])),
    ):
        actual = inspect.signature(type(reranker).__call__)
        assert list(actual.parameters) == list(expected.parameters), type(reranker).__name__
        for name, parameter in expected.parameters.items():
            if name != "self":
                assert actual.parameters[name].annotation == parameter.annotation


def test_an_index_that_does_not_match_its_candidates_is_refused():
    """Otherwise it is a reranker that quietly never reranks.

    Every row would be the wrong provision, the per-call fallback would bail
    out on the first unknown code, and the pool would come back untouched on
    every query with nothing said about it.
    """
    with pytest.raises(ValueError, match="documents"):
        LexicalReranker(LexicalIndex.build(SCHEDULE), SCHEDULE[:2])


@pytest.mark.parametrize("depth", [0, -5])
def test_a_depth_below_one_is_refused(depth):
    with pytest.raises(ValueError, match="depth"):
        CrossEncoderReranker(encoder=StubEncoder([1.0]), depth=depth)
