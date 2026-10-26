"""BM25 over the schedule, and rank fusion with the dense index.

Dense retrieval and lexical retrieval fail on different cases here, which is
why both are kept. Heading recall, both evaluation sets, produced by
``scripts/measure_retrieval.py`` from this module rather than from a
prototype of it - an earlier version of this table was measured against a
near-copy whose documents differed slightly, and could not be reproduced
from the code it was describing:

| | @5 | @20 | @50 |
| --- | ---: | ---: | ---: |
| dense | 40.0% / 38.6% | 66.5% / 61.8% | 78.5% / 77.2% |
| BM25, a heading's own text only | 18.5% / 23.5% | 28.0% / 34.8% | 39.5% / 42.6% |
| BM25 with descendant vocabulary | 36.5% / 37.3% | 52.0% / 57.5% | 64.0% / 67.2% |
| **fused** | **49.0% / 49.4%** | **68.5% / 71.7%** | **83.5% / 82.5%** |

Two things in that table are the whole design.

**A heading's own text is too short to search lexically.** It averages 16
tokens of legal drafting - "Woven fabrics of carded wool or of carded fine
animal hair" - and BM25 over it recalls 18.5% at five. Adding the vocabulary
of everything beneath the heading takes the same method to 36.5%, because
the words a product is actually described with live in the statistical
breakouts, not in the heading.

**Fusion beats both by more than either gains alone.** The two methods
disagree about which cases they get right: a description sharing an exact
term with the schedule is found lexically even when the embedding puts it
elsewhere, and a paraphrase is found densely even with no term in common.
Reciprocal rank fusion needs no weight to tune and no score calibration
between two incomparable scales, which is why it is used here instead of a
weighted sum.

**Fusion is for selecting candidates, not for ranking them.** The fused
score decides which provisions survive a level; ``Walk.score`` ranks the
survivors by cosine similarity. Sorting by the fused value instead compares
a rank-fusion number against a cosine - see ``Walk.score`` for what that
measured.

Two limits, both counted rather than asserted.

**Vocabulary the schedule does not contain cannot be indexed.** The word
"cardigan" appears nowhere in the nomenclature - heading 6110 reads
"Sweaters, pullovers, sweatshirts, waistcoats (vests) and similar articles"
- so no amount of indexing finds it. Knowing a cardigan is a similar article
to a sweater is world knowledge, and supplying it is stage 1's job.

**Vocabulary the tokeniser collapses cannot be distinguished.** Of the
20,280 ten-digit provisions, 5,488 reduce to an empty bag of terms - their
own description is "Other", a unit, or a number - and 48.2% share an
identical bag with at least one other provision. That is why fusing at eight
and ten digits changes no measured figure: there is frequently nothing there
for BM25 to tell apart. See ``TOKEN_RE``.
"""

from __future__ import annotations

import math
import re
from collections import Counter

import numpy as np

from hsclass.retrieval.index import Candidate

#: Standard BM25 parameters. Not tuned: tuning them on the eval sets would
#: be fitting the test, and the fusion is what the gain comes from.
K1 = 1.2
B = 0.75

#: Reciprocal rank fusion's only constant, at its usual value. It damps the
#: influence of a method's low-ranked tail without needing either method's
#: scores to be comparable with the other's.
RRF_K = 60

#: How much descendant vocabulary a provision absorbs.
#:
#: The rationale first written here - that chapters like 85 have headings
#: with hundreds of lines beneath them whose length would dominate the BM25
#: normalisation - was simply false, and a cold read caught it by counting.
#: Deduplicating the descendants' own segments collapses the repeated
#: "Other" and "Of an output exceeding..." lines, so a document's length is
#: nearly independent of its subtree's size: heading 8501 has 100
#: descendants and 1,480 characters, and chapter 85's longest document is
#: 2,726 - no heading in chapter 84 or 85 reaches this cap at all.
#:
#: What the cap actually does: 8 of 961 headings hit it, in chapters 29, 32,
#: 61 and 62, where the distinct chemical and apparel names are genuinely
#: long. It is kept because removing it is measurably slightly worse (fused
#: heading recall@5 falls 0.5 points, BM25 alone unchanged), not because of
#: the normalisation story. Its cost is real and worth stating: heading 3204
#: loses 50.6% of its vocabulary and 2933 loses 42.5%, which is why "denim"
#: beneath heading 6204 is invisible to lexical search.
MAX_DESCENDANT_CHARS = 4000

#: The inherited chain's separator, as ``full_description`` writes it.
CHAIN_SEPARATOR = "—"

#: Code widths a provision can inherit vocabulary upward to.
PARENT_WIDTHS = (2, 4, 6, 8)

#: Letters only, two or more. This throws away more than it looks like, and
#: a cold read counted what: every isomer prefix ("m-Xylene", "o-Xylene" and
#: "p-Xylene" all become ``['xylene']``, which is the entire legal
#: distinction between 2902.41, .42 and .43), every numeral ("60% cotton 40%
#: polyester" becomes ``['cotton', 'polyester']``, discarding the
#: predominant-weight figures Section XI Note 2(A) turns on), and every
#: identifier ("CAS No. 37640-57-6" becomes ``['cas']``, "vitamin B12"
#: becomes ``['vitamin']``).
#:
#: Widening it is not obviously right - a lexical index is not where the
#: composition percentages should be read, and ``pipeline/conditions.py``
#: already reads them as arithmetic - but the limit belongs on the record,
#: because two descriptions differing only by an isomer prefix produce
#: byte-identical BM25 rankings and so contribute the same vote to
#: provisions at different duty rates.
TOKEN_RE = re.compile(r"[a-z]{2,}")

#: Words that carry no discrimination in a tariff schedule, where nearly
#: every line contains "of" and a third contain "other".
STOP_WORDS = frozenset(
    """a an the and or of in on to by with for from this that these those is are be been
    other others not no any such which as it its their there per than more less over
    under whether including included includes also same each both""".split()
)


def terms_in(text: str) -> list[str]:
    """The query or document as BM25 sees it."""
    return [w for w in TOKEN_RE.findall(text.lower()) if w not in STOP_WORDS]


def lexical_documents(candidates: list[Candidate]) -> list[str]:
    """Each provision's own words plus the vocabulary of everything beneath it.

    A heading is searched for the goods it covers, and the schedule names
    those goods in the lines underneath rather than in the heading itself.
    Only each descendant's *own* segment is added, not its whole inherited
    chain, because the chain repeats the parent's words once per descendant
    and would swamp the term frequencies with the heading's own vocabulary.
    """
    beneath: dict[str, list[str]] = {}
    for candidate in candidates:
        own = candidate.text.split(CHAIN_SEPARATOR)[-1].strip()
        for width in PARENT_WIDTHS:
            if width < len(candidate.code):
                beneath.setdefault(candidate.code[:width], []).append(own)
    return [
        candidate.text + " " + _capped(beneath.get(candidate.code, []))
        for candidate in candidates
    ]


def _capped(segments: list[str]) -> str:
    """Join descendants' own words, stopping at a whole segment.

    Truncating the joined string mid-word cut four of the eight capped
    headings in the middle of a term, leaving a fragment that matches no
    query and no document - a token invented by the cap.
    """
    kept, budget = [], MAX_DESCENDANT_CHARS
    for segment in dict.fromkeys(segments):
        if len(segment) + 1 > budget:
            break
        kept.append(segment)
        budget -= len(segment) + 1
    return " ".join(kept)


class LexicalIndex:
    """BM25 with an inverted index, aligned row-for-row with the dense one.

    The alignment is what makes fusion a two-line operation: row *i* here and
    row *i* of the ``VectorIndex`` are the same provision, so both can be
    restricted to one parent's children and fused without a lookup.
    """

    def __init__(self, documents: list[str], k1: float = K1, b: float = B) -> None:
        self.k1, self.b = k1, b
        counted = [Counter(terms_in(d)) for d in documents]
        self.length = np.array([sum(c.values()) for c in counted], dtype=np.float32)
        average = float(self.length.mean()) if len(self.length) else 1.0
        # Precomputed because it depends only on the document, not the query.
        self._norm = k1 * (1 - b + b * self.length / (average or 1.0))

        rows: dict[str, list[int]] = {}
        frequencies: dict[str, list[float]] = {}
        for row, counts in enumerate(counted):
            for word, count in counts.items():
                rows.setdefault(word, []).append(row)
                frequencies.setdefault(word, []).append(float(count))

        total = len(documents)
        self.postings: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.idf: dict[str, float] = {}
        for word, word_rows in rows.items():
            self.postings[word] = (
                np.array(word_rows, dtype=np.int64),
                np.array(frequencies[word], dtype=np.float32),
            )
            document_frequency = len(word_rows)
            self.idf[word] = math.log(
                1 + (total - document_frequency + 0.5) / (document_frequency + 0.5)
            )

    def __len__(self) -> int:
        return len(self.length)

    @classmethod
    def build(cls, candidates: list[Candidate]) -> LexicalIndex:
        return cls(lexical_documents(candidates))

    def scores(self, terms: list[str], rows: np.ndarray | list[int] | None = None) -> np.ndarray:
        """BM25 score of every document, or of exactly the rows asked for."""
        total = np.zeros(len(self.length), dtype=np.float32)
        for word in set(terms):
            posting = self.postings.get(word)
            if posting is None:
                continue
            word_rows, frequency = posting
            total[word_rows] += (
                self.idf[word]
                * frequency
                * (self.k1 + 1)
                / (frequency + self._norm[word_rows])
            )
        return total if rows is None else total[rows]


def reciprocal_rank_fusion(*scores: np.ndarray, k: int = RRF_K) -> np.ndarray:
    """Combine rankings by position rather than by score.

    Cosine similarity and BM25 are different scales with different spreads,
    and a weighted sum of them needs a calibration that would have to be
    fitted - on the eval sets, which is where that goes wrong. Rank fusion
    needs neither.

    A ranking that scored nothing at all is dropped rather than fused: with
    every score zero there is no ranking, only storage order, and fusing it
    would give that order a real vote while looking like evidence.

    **Tied scores share the average of their positions.** This is not a
    refinement; without it the mostly-zero case does exactly what the
    all-zero case was guarded against. A product description shares a term
    with only a handful of provisions, so 85.3% of the 961 headings score
    exactly zero on a typical query, and breaking those ties by array index
    separated roughly 820 headings arbitrarily. Measured by reordering only
    that zero tier under five seeds, it moved the fused top-1 heading on 3%
    of queries and changed the fused top-5 *set* on 15% - arbitrariness
    sitting underneath the recall number this file is justified by. Averaged
    ranks give every tied provision the same contribution, so a tier that
    carries no information can no longer reorder one that does.
    """
    useful = [s for s in scores if s.size and float(s.max()) > 0.0]
    if not useful:
        return np.zeros(scores[0].shape, dtype=np.float32) if scores else np.zeros(0)
    fused = np.zeros(useful[0].shape, dtype=np.float32)
    for score in useful:
        fused += 1.0 / (k + _average_ranks(score))
    return fused


def _average_ranks(scores: np.ndarray) -> np.ndarray:
    """Descending ranks, with tied scores sharing the mean of their positions.

    A group of *c* documents tied above *g* better ones occupies positions
    ``g + 1 .. g + c``, whose mean is ``g + (c + 1) / 2``.
    """
    values, inverse = np.unique(scores, return_inverse=True)
    counts = np.bincount(inverse, minlength=values.size)
    # np.unique sorts ascending, so "better" means later in `values`.
    better = np.cumsum(counts[::-1])[::-1] - counts
    return (better + (counts + 1) / 2.0).astype(np.float32)[inverse]
