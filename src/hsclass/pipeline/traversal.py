"""Hierarchical traversal: narrow the nomenclature one level at a time.

GRI 6 states that only subheadings **at the same level** are comparable. That
makes walking the tree a legal requirement rather than an optimisation: you
settle the four-digit heading first, then choose among that heading's own
subheadings, and never weigh a six-digit subheading in one heading against a
six-digit subheading in another.

It also happens to solve the token budget. The schedule plus its notes cannot
fit in any context window, and at each level only that level's candidates and
their notes are loaded, so the context is bounded **by construction** rather
than by truncation.

The flat baseline compares a query against all 27,000 terminal lines at once,
which is both legally wrong and the reason a description that is obviously
apparel can be answered with a machine part.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import numpy as np

from hsclass.retrieval.embedder import Embedder
from hsclass.retrieval.index import Candidate, VectorIndex
from hsclass.retrieval.lexical import LexicalIndex, reciprocal_rank_fusion, terms_in
from hsclass.schedule.models import ScheduleTree, digits

#: The levels walked, in legal order: heading, subheading, tariff line,
#: statistical suffix. Chapter is handled by the heading step, because a
#: heading's first two digits are its chapter.
LEVELS = (4, 6, 8, 10)


@dataclass
class Step:
    """One level of the walk, kept so the path can be explained afterwards."""

    level: int
    code: str
    text: str
    #: What this level was ranked by - a cosine similarity when retrieval is
    #: dense only, a rank-fusion score when the lexical index is in play.
    #: The two are not comparable, which is why ``similarity`` is kept
    #: separately for anything that wants the embedding's own view.
    score: float
    considered: int
    similarity: float = 0.0


@dataclass
class Walk:
    """A candidate path from heading down to a full-length code."""

    steps: list[Step] = field(default_factory=list)

    @property
    def code(self) -> str:
        return self.steps[-1].code if self.steps else ""

    @property
    def score(self) -> float:
        """The deepest step's cosine similarity - the levels above only narrowed.

        Averaging the levels instead costs 5 points of top-1 at six digits,
        measured. A heading is a handful of words and scores low against a
        detailed product description, so a mean lets a weak heading match
        drag down the line that actually matched well. The walk's job is to
        decide *which* lines are legally comparable; which of those is
        closest is a question about the lines themselves.

        It is the **similarity** and deliberately not ``Step.score``, which
        is whatever the level was selected by. Once lexical fusion existed,
        ``Step.score`` became a rank-fusion value at a fused level and a
        cosine everywhere else - two scales roughly twenty times apart - and
        sorting walks by it compared them directly. A cold read measured the
        damage: with fusion at the heading and the tariff line, 15% of
        queries produced a walk list where every fused-level walk sorted
        below every unfused one regardless of merit, and with fusion at all
        four levels 91% of final walks carried a duplicate score, because
        RRF's maximum is 2/(k+1) whatever the sibling set's size. The
        rankings those runs produced were close to arbitrary.

        So fusion decides **which** candidates survive each level, and the
        similarity decides how the survivors rank. Those are separate jobs
        and only the second needs one comparable scale.

        This is worth more than a tidy-up, because the broken ranker had
        already been believed. It produced the evidence for "lexical fusion
        hurts at depth", that reading was written down as a finding, and it
        was wrong: re-measured here, fusing every level scores *better* than
        fusing only the heading. A bug that degrades a configuration looks
        exactly like a configuration that does not work.
        """
        return self.steps[-1].similarity if self.steps else 0.0

    @property
    def mean_score(self) -> float:
        """Kept for the ablation that showed why it is not used for ranking."""
        if not self.steps:
            return 0.0
        return sum(s.similarity for s in self.steps) / len(self.steps)

    def extend(self, step: Step) -> Walk:
        return Walk([*self.steps, step])


class LevelIndex:
    """A vector index searchable one level, and one parent, at a time.

    Children are found by code prefix rather than by a fixed parent width,
    because the schedule does not print every level for every heading: many
    headings carry eight-digit lines with no six-digit subheading in between.
    Assuming a fixed step leaves the walk stranded on the heading.
    """

    def __init__(self, index: VectorIndex, lexical: LexicalIndex | None = None) -> None:
        self.index = index
        self.lexical = lexical
        self.headings: list[int] = []
        self._below: dict[str, dict[int, list[int]]] = {}
        for row, candidate in enumerate(index.candidates):
            level = len(candidate.code)
            if level == LEVELS[0]:
                self.headings.append(row)
            for width in LEVELS:
                if width < level:
                    self._below.setdefault(candidate.code[:width], {}).setdefault(
                        level, []
                    ).append(row)

    def children(self, code: str) -> list[int]:
        """The next level that actually exists beneath this code."""
        below = self._below.get(code)
        if not below:
            return []
        return below[min(below)]

    def rank(
        self,
        query: np.ndarray,
        rows: list[int],
        k: int,
        terms: list[str] | None = None,
    ) -> list[tuple[int, float, float]]:
        """Best `k` of exactly these rows - never the whole schedule.

        With ``terms`` and a lexical index, dense and BM25 rankings of these
        same rows are fused. The fusion is computed *within* the rows under
        consideration, not over the whole schedule and then filtered: BM25
        ranks are positional, so a provision's rank among its 40 siblings is
        a different quantity from its rank among all 30,000 lines.
        """
        if not rows:
            return []
        similarity = self.index.vectors[rows] @ query
        if self.lexical is None or not terms:
            order = np.argsort(-similarity)[:k]
            return [(rows[i], float(similarity[i]), float(similarity[i])) for i in order]
        fused = reciprocal_rank_fusion(similarity, self.lexical.scores(terms, rows))
        order = np.argsort(-fused)[:k]
        return [(rows[i], float(fused[i]), float(similarity[i])) for i in order]


def candidates_at_every_level(tree: ScheduleTree) -> list[Candidate]:
    """Searchable entries for headings, subheadings and lines alike.

    The flat baseline indexes only terminal lines. Traversal needs the levels
    above them too, because it chooses a heading before it has any business
    looking at a subheading.
    """
    out: list[Candidate] = []
    for node in tree:
        if node.synthetic or not node.htsno:
            continue
        code = digits(node.htsno)
        if len(code) not in LEVELS:
            continue
        out.append(
            Candidate(
                code=code,
                text=tree.full_description(node.key),
                chapter=node.chapter,
                section=node.section,
            )
        )
    # One entry per code: the schedule repeats a code across its statistical
    # breakouts, and a duplicated row would crowd the beam with itself.
    seen: dict[str, Candidate] = {}
    for candidate in out:
        seen.setdefault(candidate.code, candidate)
    return list(seen.values())


class HierarchicalClassifier:
    """Walks heading -> subheading -> tariff line, keeping a beam at each level."""

    #: Heading recall caps everything below it: at 5 headings only 40% of
    #: cases keep their correct heading, at 20 it is 66.5%, at 50 it is 78.5%.
    #: The spec's own framing - "which twenty headings might this be?" - is
    #: the floor, not the ceiling.
    DEFAULT_BEAM = 50

    #: How many paths each heading keeps as the walk descends. Measured at
    #: 1, 2, 3 and 5; 5 is where it stops mattering.
    DEFAULT_PER_HEADING = 5

    #: Which levels fuse the lexical ranking into candidate selection.
    #:
    #: This was first set to ``(4,)`` on the strength of a measurement that
    #: showed fusion getting worse the deeper it went - top1@6 of 6.50% at
    #: every level against 14.50% for the heading alone - and explained by
    #: BM25 being close to noise among near-identical sibling lines. A cold
    #: read found two faults in that. The ranker was broken in those runs:
    #: walks were sorted by ``Step.score``, which was a rank-fusion value at
    #: a fused level and a cosine elsewhere, so the deeper the fusion the
    #: more of the ranking it destroyed. And the dense baseline the gain was
    #: quoted against, 14.00%, came from a different code version - before
    #: per-heading pruning - making the one-component delta look like +0.50
    #: when the same-version figure was +1.00. The explanation was plausible
    #: and the evidence for it was an artefact. See ``Walk.score``.
    #:
    #: Re-measured with walks ranked by similarity, there is no deep-level
    #: penalty at all. top1@6 and top5@6, test split then held-out 2,000:
    #:
    #: | levels fused | test | held-out |
    #: | --- | --- | --- |
    #: | none, dense only | 13.50 / 21.00 | 13.50 / 24.55 |
    #: | (4,) | 14.50 / 22.00 | 13.75 / 24.55 |
    #: | **(4, 6)** | **15.00 / 22.50** | **13.75 / 24.70** |
    #: | (4, 8) | 14.50 / 22.00 | 13.75 / 24.45 |
    #: | (4, 6, 8, 10) | 15.00 / 22.50 | 13.75 / 24.70 |
    #:
    #: ``(4, 6)`` and every level are identical on all four figures, so
    #: fusing at eight and ten digits changes nothing - consistent with
    #: 48.2% of ten-digit provisions sharing an identical bag of terms with
    #: another provision, leaving BM25 nothing there to tell apart. The
    #: cheaper of two equal configurations is the one to ship.
    DEFAULT_FUSE_LEVELS = (4, 6)

    def __init__(self, levels: LevelIndex, embedder: Embedder, k: int = 5,
                 beam: int = DEFAULT_BEAM, fuse_levels: tuple[int, ...] = DEFAULT_FUSE_LEVELS,
                 per_heading: int = DEFAULT_PER_HEADING,
                 reranker: Callable[[str, list[Walk]], list[Walk]] | None = None) -> None:
        self.levels = levels
        self.embedder = embedder
        self.k = k
        self.beam = beam
        self.fuse_levels = fuse_levels
        self.per_heading = per_heading
        #: Reorders the finished pool. Optional because it is the single
        #: most expensive thing in the pipeline - see ``pipeline/rerank.py``.
        self.reranker = reranker

    def walks(self, description: str) -> list[Walk]:
        query = self.embedder.encode([description])[0]
        terms = terms_in(description) if self.levels.lexical is not None else None

        def at(level: int) -> list[str] | None:
            return terms if level in self.fuse_levels else None

        walks = [
            Walk([Step(LEVELS[0], candidate.code, candidate.text, score,
                       len(self.levels.headings), similarity)])
            for row, score, similarity in self.levels.rank(
                query, self.levels.headings, self.beam, at(LEVELS[0])
            )
            for candidate in [self.levels.index.candidates[row]]
        ]

        # Descend until nothing is left below, rather than a fixed number of
        # levels: a heading may stop at eight digits or run to ten.
        for _ in LEVELS[1:]:
            deeper: list[Walk] = []
            for walk in walks:
                children = self.levels.children(walk.code)
                if not children:
                    deeper.append(walk)  # this code is where the schedule ends
                    continue
                width = len(self.levels.index.candidates[children[0]].code)
                for row, score, similarity in self.levels.rank(
                    query, children, self.per_heading, at(width)
                ):
                    candidate = self.levels.index.candidates[row]
                    deeper.append(walk.extend(Step(
                        len(candidate.code), candidate.code, candidate.text,
                        score, len(children), similarity,
                    )))
            walks = self._prune(deeper)
        return sorted(walks, key=lambda w: -w.score)

    def _prune(self, walks: list[Walk]) -> list[Walk]:
        """Keep each heading's own best paths, never one heading against another.

        A single global beam is the obvious implementation and it throws
        away recall. With 50 headings expanded and 50 slots to share, one
        heading's subheadings evict another heading's entire subtree:
        measured, 83.5% of gold headings were retrieved into the beam and
        only 65.0% were still standing when the walk reached the bottom.

        It is also the cross-heading comparison that traversal was adopted
        here to avoid. Retrieval is stage 2 and is not adjudicating, so this
        is not a GRI 6 violation - nothing is being classified yet - but
        ranking one heading's subheading against another's is the operation
        the architecture's own rationale for walking the tree rejects, and
        reintroducing it inside the walk undercut that rationale.

        Pruning within each heading keeps every retrieved heading alive. It
        does not move top-5 accuracy, because the final ranking rather than
        the candidate pool is what limits that: on the test split it costs
        1.0 point of top5@6 (two cases) and on the held-out 2,000 it gains
        0.3 (six cases). The reason to prefer it is the recall it stops
        discarding, not an accuracy gain it does not deliver.
        """
        by_heading: dict[str, list[Walk]] = {}
        for walk in walks:
            by_heading.setdefault(walk.steps[0].code, []).append(walk)
        return [
            walk
            for group in by_heading.values()
            for walk in sorted(group, key=lambda w: -w.score)[: self.per_heading]
        ]

    def classify(self, description: str) -> tuple[list[str], list[Walk]]:
        walks = self.walks(description)
        if self.reranker is not None:
            # After the walk, not inside it: reranking reorders the pool and
            # must not change which candidates the descent produced, or the
            # funnel that measures the descent would be measuring this too.
            walks = self.reranker(description, walks)
        ranked: list[str] = []
        for walk in walks:
            if walk.code and walk.code not in ranked:
                ranked.append(walk.code)
            if len(ranked) == self.k:
                break
        return ranked, walks

    def __call__(self, description: str) -> tuple[Iterable[str], bool]:
        """The eval runner's shape. Escalation is a week-3 question."""
        ranked, _ = self.classify(description)
        return ranked, False
