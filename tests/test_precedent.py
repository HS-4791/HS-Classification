"""Retrieval over past rulings - stage 2's best candidate source."""

from __future__ import annotations

import pytest

from hsclass.retrieval.precedent import Precedent, PrecedentIndex

RULINGS = [
    Precedent("a men's knit cardigan of 100% acrylic", "6110301540"),
    Precedent("a women's knit cardigan of acrylic fibre", "6110301560"),
    Precedent("a men's cotton jersey pullover, ribbed collar", "6110202067"),
    Precedent("cast carbon steel pipe fittings, ASTM A216", "7307198000"),
    Precedent("unvulcanized synthetic rubber in sheets", "4005100000"),
]


@pytest.fixture
def index():
    return PrecedentIndex(RULINGS)


def test_a_precedent_is_found_by_the_words_the_shipper_used(index):
    """The point of the whole module: "cardigan" is in no HTS provision.

    It is in the rulings, because a ruling describes the goods the way the
    importer did.
    """
    codes = index.codes("a knit cardigan made of acrylic", digits=6)
    assert codes[0] == "611030"


def test_codes_are_deduplicated_at_the_requested_width(index):
    """Two cardigan rulings share a six-digit subheading and differ at ten."""
    assert index.codes("knit cardigan acrylic", digits=6).count("611030") == 1
    ten = index.codes("knit cardigan acrylic", digits=10)
    assert len([c for c in ten if c.startswith("611030")]) == 2


def test_an_unrelated_query_does_not_return_garment_codes(index):
    codes = index.codes("cast steel pipe fittings", digits=6)
    assert codes[0] == "730719"


def test_vote_and_nearest_are_both_available(index):
    """Nearest wins at top-1 and loses at depth; the choice stays measurable."""
    assert index.codes("knit cardigan acrylic", ranking="nearest", digits=6)
    assert index.codes("knit cardigan acrylic", ranking="vote", digits=6)
    with pytest.raises(ValueError, match="ranking"):
        index.codes("x", ranking="closest")


def test_a_query_sharing_no_terms_returns_nothing(index):
    """No match is an empty candidate list, which escalates downstream."""
    assert index.codes("xyzzy plugh frobnitz", digits=6) == []


def test_the_rulings_behind_an_answer_are_citable(index):
    """A precedent can be named in an explanation; a cosine distance cannot."""
    cited = index.citations("a knit cardigan made of acrylic", limit=2)
    assert len(cited) == 2
    assert all(isinstance(c, Precedent) for c in cited)
    assert "cardigan" in cited[0].description


def test_an_empty_index_is_refused():
    with pytest.raises(ValueError, match="at least one"):
        PrecedentIndex([])


class StubReranker:
    """Reverses the pool, so a test can see that the order was applied."""

    depth = 50

    def order(self, description, texts):
        return list(reversed(range(len(texts))))


def test_a_reranker_reorders_the_precedents_before_they_become_codes(index):
    """The hook exists; the cross-encoder measured through it does not pay.

    BM25 alone gets top-1 at six digits to 52.50% on the official split
    against a depth-50 pool ceiling of 83.50%. Adding the cross-encoder moves
    top-1 to 52.00% - one case the wrong way - lifts top-5 by 4 points, and
    costs a second per classification. The hook is kept because the gap it was
    built to close is real and measured; this particular model does not close
    it.
    """
    plain = index.codes("knit cardigan acrylic", digits=10)
    reversed_ = index.codes("knit cardigan acrylic", digits=10, reranker=StubReranker())
    assert plain and reversed_
    assert plain != reversed_
    assert set(plain) == set(reversed_)
