"""How much of stage 2's ranking is decided by a tie rather than by a score.

The check, stated before running. Three ranking sites broke ties with an
unstable sort, so the answer depended on the numpy build rather than on the
data. They are fixed. This script measures how much was at stake, and it
exists because the first version of those numbers was taken from a synthetic
sweep - 400 sibling groups crossed with 200 queries - rather than from the
walk the pipeline actually performs. That is not what the pipeline does, and a
figure with no committed script behind it is not a result.

What is counted, during real classifications:

* ``rank()`` calls per classification, because "on the order of hundreds" was
  a guess and the arithmetic resting on it needed a number.
* calls whose ordering scores contain an exact tie, and how wide.
* calls where a tie **straddles the cut** at k. Those are the consequential
  ones: a tie below the cut changes the order of candidates that were all
  going to be returned anyway, while a tie across it changes which
  provisions stage 3 ever sees.

Two indices, because they are not alike. ``index-levels`` is the shipped one.
``index-levels-own-text`` is the chunking ablation's second arm, where
stripping the inherited chain leaves 6,370 provisions reading "Other" - so it
is saturated with exact ties and shows what the mechanism looks like at its
worst.

Usage: ``python -u scripts/measure_tie_exposure.py``
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.config import get_settings  # noqa: E402
from hsclass.data.atlas import load_split  # noqa: E402
from hsclass.pipeline.traversal import (  # noqa: E402
    HierarchicalClassifier,
    LevelIndex,
    reciprocal_rank_fusion,
)
from hsclass.retrieval.embedder import get_embedder  # noqa: E402
from hsclass.retrieval.index import VectorIndex  # noqa: E402
from hsclass.retrieval.lexical import LexicalIndex  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ATLAS = ROOT / "data" / "raw" / "atlas"
REPORTS = ROOT / "reports"


def say(line: str) -> None:
    sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()


class CountingLevelIndex(LevelIndex):
    """A LevelIndex that records what its ranking had to break ties over.

    A subclass rather than a monkeypatch, so what is measured is exactly what
    ``rank`` scores: the fused vector where both signals are present, the
    cosine similarity where they are not.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0
        self.calls_with_a_tie = 0
        self.calls_tied_across_the_cut = 0
        self.widths: Counter = Counter()
        self.from_duplicate_text = 0

    def rank(self, query, rows, k, terms=None):
        self.calls += 1
        if rows:
            similarity = self.index.vectors[rows] @ query
            if self.lexical is not None and terms:
                scores = reciprocal_rank_fusion(
                    similarity, self.lexical.scores(terms, rows)
                )
                fused = True
            else:
                scores = similarity
                fused = False
            scores = np.asarray(scores)
            counts = Counter(scores.tolist())
            widest = max(counts.values()) if len(counts) else 1
            if widest > 1:
                self.calls_with_a_tie += 1
                for _value, n in counts.items():
                    if n > 1:
                        self.widths[n] += 1
                        # A tie in the cosine signal means identical vectors,
                        # i.e. duplicate text. A tie only in the fused score
                        # means two rows whose ranks are swapped between the
                        # two lists, which needs no duplicates at all.
                        if not fused or len(set(similarity.tolist())) < len(rows):
                            self.from_duplicate_text += 1
                        break
                # Does a tie decide MEMBERSHIP, or only order among rows that
                # were all going to be returned?
                if k < len(scores):
                    ordered = np.sort(scores)[::-1]
                    if ordered[k - 1] == ordered[k]:
                        self.calls_tied_across_the_cut += 1
        return super().rank(query, rows, k, terms)


def measure(name: str, directory: Path, examples, embedder) -> dict:
    index = VectorIndex.load(directory)
    level = CountingLevelIndex(index, LexicalIndex.build(index.candidates))
    classifier = HierarchicalClassifier(level, embedder, k=5)
    say(f"  {name}: {len(index.candidates)} candidates, walking {len(examples)} cases",)

    per_case = []
    queries_touching_a_tie = 0
    for example in examples:
        before_calls = level.calls
        before_ties = level.calls_with_a_tie
        classifier.classify(example.description)
        per_case.append(level.calls - before_calls)
        if level.calls_with_a_tie > before_ties:
            queries_touching_a_tie += 1

    result = {
        "candidates": len(index.candidates),
        "cases": len(examples),
        "rank_calls": level.calls,
        "rank_calls_per_case": {
            "mean": round(sum(per_case) / len(per_case), 1),
            "min": min(per_case),
            "max": max(per_case),
        },
        "calls_with_a_tie": level.calls_with_a_tie,
        "calls_with_a_tie_pct": round(100 * level.calls_with_a_tie / max(level.calls, 1), 2),
        "calls_tied_across_the_cut": level.calls_tied_across_the_cut,
        "queries_touching_a_tie": queries_touching_a_tie,
        "tie_widths": dict(sorted(level.widths.items())),
    }
    say(f"    {result['rank_calls']} rank() calls, "
        f"{result['rank_calls_per_case']['mean']} per case")
    say(f"    {result['calls_with_a_tie']} contain an exact tie "
        f"({result['calls_with_a_tie_pct']}%), "
        f"{result['calls_tied_across_the_cut']} of those straddle the cut")
    say(f"    {queries_touching_a_tie}/{len(examples)} cases touch at least one tie")
    say(f"    tie widths: {result['tie_widths']}")
    return result


def main() -> int:
    settings = get_settings()
    revision = settings.require_revision()
    embedder = get_embedder(settings.embedding_backend, settings.embedding_model)
    examples, _ = load_split(ATLAS / "test.jsonl")

    say(f"revision {revision}, {len(examples)} test cases")
    report: dict = {
        "revision": revision,
        "what_this_measures": (
            "Exact ties in the scores LevelIndex.rank orders by, counted during "
            "real classifications rather than over a synthetic sweep of sibling "
            "groups. A tie straddling the cut changes which provisions stage 3 "
            "is offered; a tie below it changes only their order."
        ),
        "indices": {},
    }
    for name, directory in (
        ("index-levels", settings.processed_dir / "index-levels"),
        ("index-levels-own-text", settings.processed_dir / "index-levels-own-text"),
    ):
        if not (directory / "vectors.npy").exists():
            say(f"  {name}: absent, skipped")
            continue
        report["indices"][name] = measure(name, directory, examples, embedder)

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / "tie-exposure-test-200.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    say("")
    say(f"wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
