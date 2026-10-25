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

from collections.abc import Iterable
from dataclasses import dataclass, field

import numpy as np

from hsclass.retrieval.embedder import Embedder
from hsclass.retrieval.index import Candidate, VectorIndex
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
    score: float
    considered: int


@dataclass
class Walk:
    """A candidate path from heading down to a full-length code."""

    steps: list[Step] = field(default_factory=list)

    @property
    def code(self) -> str:
        return self.steps[-1].code if self.steps else ""

    @property
    def score(self) -> float:
        """The deepest step's similarity - the levels above it only narrowed.

        Averaging the levels instead costs 5 points of top-1 at six digits,
        measured. A heading is a handful of words and scores low against a
        detailed product description, so a mean lets a weak heading match
        drag down the line that actually matched well. The walk's job is to
        decide *which* lines are legally comparable; which of those is
        closest is a question about the lines themselves.
        """
        return self.steps[-1].score if self.steps else 0.0

    @property
    def mean_score(self) -> float:
        """Kept for the ablation that showed why it is not used for ranking."""
        return sum(s.score for s in self.steps) / len(self.steps) if self.steps else 0.0

    def extend(self, step: Step) -> Walk:
        return Walk([*self.steps, step])


class LevelIndex:
    """A vector index searchable one level, and one parent, at a time.

    Children are found by code prefix rather than by a fixed parent width,
    because the schedule does not print every level for every heading: many
    headings carry eight-digit lines with no six-digit subheading in between.
    Assuming a fixed step leaves the walk stranded on the heading.
    """

    def __init__(self, index: VectorIndex) -> None:
        self.index = index
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

    def rank(self, query: np.ndarray, rows: list[int], k: int) -> list[tuple[int, float]]:
        """Best `k` of exactly these rows - never the whole schedule."""
        if not rows:
            return []
        scores = self.index.vectors[rows] @ query
        order = np.argsort(-scores)[:k]
        return [(rows[i], float(scores[i])) for i in order]


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

    def __init__(self, levels: LevelIndex, embedder: Embedder, k: int = 5,
                 beam: int = DEFAULT_BEAM) -> None:
        self.levels = levels
        self.embedder = embedder
        self.k = k
        self.beam = beam

    def walks(self, description: str) -> list[Walk]:
        query = self.embedder.encode([description])[0]
        walks = [
            Walk([Step(LEVELS[0], candidate.code, candidate.text, score,
                       len(self.levels.headings))])
            for row, score in self.levels.rank(query, self.levels.headings, self.beam)
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
                for row, score in self.levels.rank(query, children, self.beam):
                    candidate = self.levels.index.candidates[row]
                    deeper.append(walk.extend(Step(
                        len(candidate.code), candidate.code, candidate.text,
                        score, len(children),
                    )))
            walks = sorted(deeper, key=lambda w: -w.score)[: self.beam]
        return sorted(walks, key=lambda w: -w.score)

    def classify(self, description: str) -> tuple[list[str], list[Walk]]:
        walks = self.walks(description)
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
