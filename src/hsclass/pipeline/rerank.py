"""Reordering the walk pool - stage 2, and the most expensive thing here.

The funnel says where the accuracy went. On the test split the candidate
pool holds the right subheading for 47.0% of cases and the output surfaces
it in the top five for 22.0%, so **more than half of what retrieval finds is
lost by the ranking of what it already has**. Recall was not the constraint;
this is.

Two rerankers live here because the expensive one has to beat the free one,
not the baseline. The result on the 200-case test split, which is the number
that decides it:

Proposer metrics, test split then held-out 2,000:

| | top1@6 | top5@6 | cost per case |
| --- | ---: | ---: | ---: |
| no reranker | 15.00% / 13.75% | 22.50% / 24.70% | 16ms |
| ``LexicalReranker`` | 15.00% / 13.90% | 23.00% / **22.95%** | 15ms |
| ``CrossEncoderReranker`` (MiniLM-L-6, depth 50) | **19.50%** | **26.00%** | 1,948ms |

**The free reranker is worse than nothing, and it took the larger set to say
so.** On 200 cases it looked harmless - 0.00 points of top1@6 and +0.50 of
top5@6, one case. On 2,000 it costs **1.75 points of top5@6**, which is 35
cases, and adjudicated it is worse still: it raised the number of cases the
engine answered from 5 to 7 while accuracy on those fell from 3 of 5 to 1 of
7. It is kept as the control, not as an option: a reranker costing a hundred
times as much has to be compared against the free attempt rather than
against the baseline, and deleting the comparison would make the expensive
one look like the only idea anybody had.

It also earns its place as a cautionary number, twice over. A 30-case screen
had it at 53.3% against a 43.3% baseline, on a different metric (gold within
the top five, among cases whose gold was in the pool), which looked like
closing two thirds of the gap on three cases. Then 200 cases said neutral.
Only 2,000 said harmful. The honest reading of why it churns without being
more right: 47.1% of a 212-walk pool scores exactly zero on BM25, so one
shared term lifts a walk past the entire rank-fusion spread of the top 50
cosine positions. The top-5 set changed on 88.4% of 500 cases.

**A bigger reranker is not a better one.** On the same screen,
``bge-reranker-base`` - the strongest of four on general benchmarks - scored
*below* the no-reranker baseline, 40.0% against 43.3%, at 34 seconds a case.
``jina-reranker-v1-turbo-en`` tied MiniLM-L-6 at 4.5 seconds, and MiniLM-L-12
was worse than both while costing more. These documents are terse legal
nomenclature, not the web prose the models were trained on, so general
reranking quality does not transfer and had to be measured rather than
assumed from a leaderboard.

**A reranker here does change what the deterministic engine decides, and
reasoning otherwise was an error worth recording.** The GRI engine is
order-invariant: every branch that returns a code either has a single
candidate or picks by a rule reading the whole set, asserted in
``tests/test_gri.py`` by exhausting the permutations of a fixed set. The
inference drawn from that - *so a reranker cannot reach stage 3* - is false.
Order-invariance holds for a **fixed candidate set**, and the adjudicator
never sees the pool. It sees ``classify()``'s first ``k`` distinct codes,
taken *after* reranking, so reranking changes the set rather than merely its
order, and a different set is a different decision.

Measured with the free reranker on 500 cases: the top-5 set changed on
88.4%, the answer-or-escalate outcome flipped on 7, and of the cases both
configurations answered, 3 decided a different code. One of them put a
women's coat of 70% polyester and 30% wool into 6405.20.6060, "Other
footwear, with soles and uppers of wool felt", under GRI 1 with a citation
attached - because reranking admitted a chapter 64 line into a set of
chapter 62 coat lines and the engine's condition rule then eliminated every
candidate that stated a wool threshold. That particular wrong answer is
fixed in ``GRIEngine._condition_rule``, which now requires a lone survivor
to share a heading with something eliminated; the general point is not
fixed and is not a bug to be patched.

So the architecture invariant needs stating more carefully than "the model
never decides". What holds is that **stage 3 is deterministic and its rules
are auditable**: the same candidates always produce the same decision and
the same rule path. What does *not* hold is that stage 2 is harmless. A
probabilistic stage choosing which five provisions the adjudicator may
consider is influencing the outcome, and reranking makes that influence
larger rather than introducing it - retrieval always had it.

The open design question, deliberately not settled here: whether the engine
should adjudicate the whole reranked pool, take a much wider ``k`` and
narrow it itself, or refuse a reranker upstream of the adjudicator
altogether. Each changes what the rules mean - ``len(set(candidates)) == 1``
never fires on 200 candidates - so it needs its own measurement rather than
a guess inside this docstring.

Reordering also changes the escalated shortlist, and with 97.5% of cases
escalating that shortlist is the system's main output.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

from hsclass.pipeline.traversal import Walk
from hsclass.retrieval.index import Candidate
from hsclass.retrieval.lexical import LexicalIndex, reciprocal_rank_fusion, terms_in


@runtime_checkable
class Reranker(Protocol):
    """Reorders walks. It may not add, remove or alter them.

    Runtime-checkable so the test suite can assert that both implementations
    here satisfy it, which keeps the contract from drifting away from the
    classes it describes. ``HierarchicalClassifier`` annotates the same thing
    as a plain callable, because importing this module from ``traversal``
    would be a cycle - ``Walk`` lives there.
    """

    def __call__(self, description: str, walks: list[Walk]) -> list[Walk]:
        ...


class LexicalReranker:
    """Fuse each line's cosine with BM25 over that line's own document.

    Free, in the sense that both numbers are already computed or cheap: the
    cosine is carried on the walk and the BM25 scores come from the index
    the retrieval stage already built.

    **Measured harmful - kept as the control, not for use.** It costs 1.75
    points of top5@6 on the held-out 2,000 and makes the adjudicated system
    answer more cases less accurately. See the module docstring for the
    numbers and for why the comparison is still worth keeping in the tree.
    """

    def __init__(self, lexical: LexicalIndex, candidates: list[Candidate]) -> None:
        if len(lexical) != len(candidates):
            # Built from a different candidate list than the index it serves,
            # every row would be the wrong provision. Caught here because the
            # per-call fallback below would otherwise make this a permanent
            # silent no-op: a reranker that quietly never reranks.
            raise ValueError(
                f"index has {len(lexical)} documents, candidates has {len(candidates)}"
            )
        self.lexical = lexical
        self.row_of = {candidate.code: row for row, candidate in enumerate(candidates)}

    def __call__(self, description: str, walks: list[Walk]) -> list[Walk]:
        if len(walks) < 2:
            return walks
        rows = [self.row_of.get(walk.code, -1) for walk in walks]
        if -1 in rows:
            # A walk whose code is not in the index cannot be scored lexically,
            # and guessing a row would score it against another provision.
            return walks
        cosine = np.array([walk.score for walk in walks], dtype=np.float32)
        fused = reciprocal_rank_fusion(cosine, self.lexical.scores(terms_in(description), rows))
        return [
            walk
            for _, walk in sorted(zip(fused, walks, strict=True), key=lambda pair: -pair[0])
        ]


class CrossEncoderReranker:
    """Score each (description, provision) pair with a cross-encoder.

    Only the head of the pool is reranked. The pool averages 212 walks, and
    the tail is nearly empty of right answers. Measured on the test split,
    of the cases whose gold subheading is in the pool at all, the share
    reachable within the first N walks by cosine:

    | N | reachable | cost per classification |
    | ---: | ---: | ---: |
    | 10 | 56.4% | ~880ms |
    | 30 | 76.6% | ~1,270ms |
    | **50** | **89.4%** | **1,654ms, measured** |
    | 100 | 95.7% | 2,620ms, measured |
    | 212, the whole pool | 100% | ~4,780ms |

    The cost column was first written as ``depth x 21ms`` and was wrong,
    which a cold read caught by comparing it with the project's own report.
    Two measured points - depth 50 at 1,654ms and depth 100 at 2,620ms - fit
    **19.3ms per pair plus about 670ms of fixed overhead** per call, on top
    of the 16ms walk. That model also predicts the whole-pool figure within
    5%, so it is not an artefact of two points; the rows above that are not
    marked "measured" are extrapolated from it, and the old table understated
    depth 10 by about four times.

    ``depth`` is 50 on cost rather than on arithmetic: reranking the whole
    pool is 2.9 times the budget for the last 10.6% of reachability, and
    depth 100 is 1.6 times for +6.3 points of it. Depth 100 did measure
    +0.50 top1@6 - one case in 200, which this sample cannot distinguish
    from noise. Walks past the depth keep their order and stay below the
    reranked head, so truncation bounds the cost without losing a candidate.
    """

    #: Cheapest of the four screened, and tied best. See the module docstring.
    DEFAULT_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
    DEFAULT_DEPTH = 50

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        depth: int = DEFAULT_DEPTH,
        encoder: object | None = None,
    ) -> None:
        if depth < 1:
            # walks[:0] silently reranks nothing and walks[:-5] splits the
            # pool from the wrong end; both return a permutation, so neither
            # would fail visibly.
            raise ValueError(f"depth must be at least 1, got {depth}")
        if encoder is None:
            # Imported here so that importing this module needs neither the
            # rerank extra nor a model download.
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            encoder = TextCrossEncoder(model_name=model_name)
        self.encoder = encoder
        self.model_name = model_name
        self.depth = depth

    def __call__(self, description: str, walks: list[Walk]) -> list[Walk]:
        head, tail = walks[: self.depth], walks[self.depth:]
        if len(head) < 2:
            return walks
        scores = list(self.encoder.rerank(description, [w.steps[-1].text for w in head]))
        # strict: one score per pair. A mismatch means the encoder dropped or
        # added a pair, and pairing by position would then score each walk
        # against a different provision's text than the one it was given.
        ranked = [w for _, w in sorted(zip(scores, head, strict=True), key=lambda pair: -pair[0])]
        return [*ranked, *tail]
