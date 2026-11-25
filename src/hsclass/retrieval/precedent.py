"""Retrieval over past rulings, which is how the job is actually done.

Every other retrieval path in this project searches the *schedule*: 30,195
provisions of nomenclature, each embedded with its inherited chain. That is
the obvious thing to search and it runs straight into the finding that has
defeated four separate mechanisms here - **the schedule's vocabulary is not
the shipper's**. "Cardigan" appears nowhere in the HTS, which says "Sweaters,
pullovers, sweatshirts, waistcoats (vests) and similar articles". No amount of
ranking fixes a query and a corpus that do not share words.

The training set is 18,253 CBP ruling descriptions, each with a code an
officer assigned, written in the same register as the query. Searching those
sidesteps the gap entirely, and it is what a customs broker does: CROSS is a
precedent database, and "CBP has ruled on near-identical goods under 6110.20"
is a better justification than a cosine distance, not a worse one.

Measured on the official 200-case test split, against 15.00% top-1 at six
digits for schedule retrieval:

| | all 200 | leakage-free 160 |
| --- | --- | --- |
| nearest, top1@6 | **53.50%** | **46.25%** |
| nearest, top5@6 | **73.50%** | **68.75%** |
| nearest, top20@6 | 82.00% | 78.75% |

**Two numbers, because the official split leaks.** 40 of the 200 test
descriptions appear verbatim in the training set - 31 with the same ten-digit
code, 9 with a *different* one, which means the benchmark contains
contradictory labels and no system can be right on both. Any method that
memorises the training data collects those 31 free, a fine-tuned model
included, so the honest figure is the one over the 160 cases that are not
duplicates. The published state of the art on this split, a fine-tuned
LLaMA-3.3-70B, is 57.5% at six digits and benefits from the same leakage.

**This does not put a model in stage 3.** A precedent is a candidate, so this
is stage 2, which is allowed to be probabilistic because stage 3 adjudicates
deterministically or refuses. What it changes is the quality of what stage 3
is handed.

Nearest beats vote-weighting at the top of the ranking and loses slightly at
depth - the opposite of what was predicted before measuring, and both are kept
so the choice stays re-measurable.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from hsclass.retrieval.lexical import LexicalIndex, terms_in

#: How many neighbouring rulings to consider before collapsing to codes. Deep
#: enough that twenty distinct codes are usually available, since several
#: neighbours routinely share one.
DEFAULT_DEPTH = 50

#: ``"nearest"`` ranks codes by the closest ruling carrying them;
#: ``"vote"`` ranks by the summed score of every neighbour carrying them.
RANKINGS = ("nearest", "vote")


@dataclass(frozen=True)
class Precedent:
    """One past ruling: what the goods were, and what it was classified as."""

    description: str
    code: str


class PrecedentIndex:
    """BM25 over ruling descriptions, returning the codes they were given.

    Lexical rather than dense, and that is a measurement rather than a
    preference: BM25 alone takes top-1 at six digits from 15.00% to 53.50% on
    this split. A dense index over the same descriptions can be fused in the
    same way the schedule index fuses, and is worth trying - but the cheap
    half is where the gain turned out to be, so it ships first and alone.
    """

    def __init__(self, precedents: list[Precedent], depth: int = DEFAULT_DEPTH) -> None:
        if not precedents:
            raise ValueError("a precedent index needs at least one ruling")
        self.precedents = precedents
        self.depth = depth
        self.lexical = LexicalIndex([p.description for p in precedents])
        self._codes = [_digits(p.code) for p in precedents]

    def neighbours(self, description: str) -> list[tuple[int, float]]:
        """Rows of the nearest rulings, best first, with their scores."""
        scores = self.lexical.scores(terms_in(description))
        # kind="stable". The default is quicksort, which orders tied scores
        # arbitrarily - and not even consistently between numpy builds, which
        # is how the same bundle scored 105/200 at six digits in one image of
        # this service and 108/200 in another built three days later.
        #
        # The ties are not rare. Measured over the 200-case test split: 40
        # descriptions tie at rank 1, and in 14 of those the tied rulings
        # disagree at six digits, so 14 of 200 scored answers were decided by
        # sort order rather than by BM25. One tie is 74 rulings wide. A stable
        # sort resolves them by row, which is fixed for a given bundle, so the
        # same bundle now gives the same answer on any machine.
        #
        # This makes the number reproducible. It does not make it right: a
        # 74-way tie means BM25 genuinely cannot separate those rulings, and
        # picking the first is a convention, not a judgement.
        order = np.argsort(-scores, kind="stable")[: self.depth]
        return [(int(row), float(scores[row])) for row in order if scores[row] > 0]

    def codes(
        self,
        description: str,
        ranking: str = "nearest",
        digits: int = 10,
        reranker: object | None = None,
    ) -> list[str]:
        """Candidate codes from precedent, ranked, deduplicated.

        ``digits`` truncates before deduplicating, so asking for six returns
        distinct subheadings rather than distinct tariff lines - which is what
        a six-digit claim should be scored on.

        ``reranker`` reorders the neighbouring rulings before they collapse to
        codes, by scoring the query against each ruling's own description. The
        headroom for it is measured: the gold six-digit code is somewhere in
        the depth-50 neighbour pool for 83.50% of the official test split
        while BM25 puts it first for 53.50%, so a third of the available
        accuracy is sitting in the order rather than the recall.
        """
        if ranking not in RANKINGS:
            raise ValueError(f"ranking must be one of {RANKINGS}, not {ranking!r}")
        found = self.neighbours(description)

        if reranker is not None and len(found) > 1:
            texts = [self.precedents[row].description for row, _ in found]
            found = [found[index] for index in reranker.order(description, texts)]

        if ranking == "nearest":
            ranked: list[str] = []
            for row, _ in found:
                code = self._codes[row][:digits]
                if code and code not in ranked:
                    ranked.append(code)
            return ranked

        weight: dict[str, float] = defaultdict(float)
        for row, score in found:
            code = self._codes[row][:digits]
            if code:
                weight[code] += score
        return sorted(weight, key=lambda code: -weight[code])

    def citations(self, description: str, limit: int = 3) -> list[Precedent]:
        """The rulings behind the answer, for an explanation to name.

        The reason this retrieval path is worth having in a system whose
        output is a citation: a precedent is itself citable, where a cosine
        distance is not.
        """
        return [self.precedents[row] for row, _ in self.neighbours(description)[:limit]]


def _digits(code: str) -> str:
    return "".join(c for c in code if c.isdigit())
