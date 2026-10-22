"""The vector-only baseline: the floor every later component has to beat.

This is deliberately the dumb version. Embed the product description, embed
every terminal tariff line, return the nearest ones. No hierarchy, no notes,
no rules - which is the point. It is the configuration nearly everyone ships,
and publishing it is what makes the later deltas mean anything.

One honest note on what it is *not*: the ablation table calls this row
"vector retrieval only, model picks the code". There is no model here. Nearest
neighbour picks the code, so this is the floor under that floor; adding the
model-picks step can only be measured against this.
"""

from __future__ import annotations

from collections.abc import Iterable

from hsclass.retrieval.embedder import Embedder
from hsclass.retrieval.index import Candidate, VectorIndex
from hsclass.schedule.models import ScheduleTree, digits

#: Only lines a classification can actually land on. The duty attaches at
#: eight digits; ten-digit lines split the statistics beneath it.
TERMINAL_LENGTHS = (8, 10)


def candidates_from_tree(tree: ScheduleTree) -> list[Candidate]:
    """One searchable entry per terminal line, carrying its inherited text.

    The embeddable unit is the whole chain, never the line's own words:
    6109.10 reads as "Of cotton", which on its own matches nothing useful.
    """
    out: list[Candidate] = []
    for node in tree:
        if node.synthetic or not node.htsno:
            continue
        if len(digits(node.htsno)) not in TERMINAL_LENGTHS:
            continue
        out.append(
            Candidate(
                code=digits(node.htsno),
                text=tree.full_description(node.key),
                chapter=node.chapter,
                section=node.section,
            )
        )
    return out


class VectorOnlyClassifier:
    """Nearest tariff lines by cosine similarity, deduplicated by code."""

    def __init__(self, index: VectorIndex, embedder: Embedder, k: int = 5,
                 pool: int = 50) -> None:
        self.index = index
        self.embedder = embedder
        self.k = k
        #: Several statistical lines share a six-digit code, so more hits are
        #: pulled than returned and then collapsed by code.
        self.pool = max(pool, k)

    def classify_many(self, descriptions: list[str]) -> list[list[str]]:
        vectors = self.embedder.encode(descriptions)
        results = []
        for hits in self.index.search(vectors, k=self.pool):
            seen: list[str] = []
            for hit in hits:
                if hit.code not in seen:
                    seen.append(hit.code)
                if len(seen) == self.k:
                    break
            results.append(seen)
        return results

    def __call__(self, description: str) -> tuple[Iterable[str], bool]:
        """The eval runner's classifier shape: ranked codes, and whether it declined.

        It never declines. Escalation is a week-3 question, and a baseline
        that refuses nothing is the honest floor to measure against.
        """
        return self.classify_many([description])[0], False
