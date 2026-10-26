"""What reranking the walk pool buys, and what it costs.

The funnel in ``measure_retrieval.py`` found that the candidate pool holds
the right subheading twice as often as the output surfaces it. This measures
whether reordering the pool closes that gap, on the metrics the project
reports and with latency beside them - a reranker that adds two points of
top-5 for a second a query is a different proposition from one that adds two
points for free, and only reporting both lets anyone tell which this is.

Usage: ``python scripts/measure_rerank.py [SET] [CONFIG,CONFIG...]``, where
SET is ``test-200``, ``heldout-2000`` or ``both``. The cross-encoder runs at
roughly 1.7 seconds a classification at depth 50, so the held-out 2,000
takes about an hour per cross-encoder configuration - hence the filter, and
hence running the depth sweep on the test split only.
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.config import get_settings  # noqa: E402
from hsclass.data.atlas import as_dataset, load_split  # noqa: E402
from hsclass.eval.runner import run_eval  # noqa: E402
from hsclass.pipeline.adjudicated import AdjudicatedClassifier  # noqa: E402
from hsclass.pipeline.gri import GRIEngine  # noqa: E402
from hsclass.pipeline.rerank import CrossEncoderReranker, LexicalReranker  # noqa: E402
from hsclass.pipeline.traversal import HierarchicalClassifier, LevelIndex  # noqa: E402
from hsclass.retrieval.embedder import get_embedder  # noqa: E402
from hsclass.retrieval.index import VectorIndex  # noqa: E402
from hsclass.retrieval.lexical import LexicalIndex  # noqa: E402
from hsclass.schedule.exclusions import build_exclusion_graph  # noqa: E402
from hsclass.schedule.notes import Note  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ATLAS = ROOT / "data" / "raw" / "atlas"
HELD_OUT_SIZE = 2000
SEED = 20261017


def _notes(settings) -> list[Note]:
    """The binding provisions the exclusion graph is built from."""
    raw = json.loads((settings.processed_dir / "notes.json").read_text(encoding="utf-8"))
    return [Note(**{k: v for k, v in n.items() if k in Note.__dataclass_fields__})
            for n in raw]


def main() -> int:
    wanted = sys.argv[1] if len(sys.argv) > 1 else "both"
    settings = get_settings()
    revision = settings.require_revision()
    embedder = get_embedder(settings.embedding_backend, settings.embedding_model)
    levelled = VectorIndex.load(settings.processed_dir / "index-levels")
    lexical = LexicalIndex.build(levelled.candidates)
    levels = LevelIndex(levelled, lexical)

    official, _ = load_split(ATLAS / "test.jsonl")
    train, _ = load_split(ATLAS / "train.jsonl")
    held_out = random.Random(SEED).sample(train, min(HELD_OUT_SIZE, len(train)))
    sets = {"test-200": official, "heldout-2000": held_out}
    if wanted != "both":
        if wanted not in sets:
            raise SystemExit(f"unknown set: {wanted}; pick test-200, heldout-2000 or both")
        sets = {wanted: sets[wanted]}

    names = sys.argv[2].split(",") if len(sys.argv) > 2 else [
        "no-reranker", "lexical", "cross-encoder-50", "cross-encoder-100"
    ]
    known = {"no-reranker", "lexical", "cross-encoder-50", "cross-encoder-100"}
    unknown = set(names) - known
    if unknown:
        raise SystemExit(f"unknown configurations: {sorted(unknown)}; pick from {sorted(known)}")

    # Built only if asked for: the model is an 80MB download and a load, and
    # the whole point of the filter is to run the free configurations cheaply.
    cross = None
    if any(name.startswith("cross-encoder") for name in names):
        started = time.time()
        cross = CrossEncoderReranker()
        print(f"cross-encoder {cross.model_name} loaded in {time.time() - started:.1f}s, "
              f"depth {cross.depth}", flush=True)

    available = {
        "no-reranker": lambda: None,
        "lexical": lambda: LexicalReranker(lexical, levelled.candidates),
        "cross-encoder-50": lambda: cross,
        "cross-encoder-100": lambda: CrossEncoderReranker(depth=100, encoder=cross.encoder),
    }
    configurations = {name: available[name]() for name in names}

    engine = GRIEngine(build_exclusion_graph(_notes(settings)))
    texts = {candidate.code: candidate.text for candidate in levelled.candidates}

    report: dict[str, dict] = {"revision": revision, "configurations": {}}
    for label, reranker in configurations.items():
        proposer = HierarchicalClassifier(levels, embedder, k=5, reranker=reranker)
        # Both stages, because the proposer's own numbers cannot see what
        # reranking does to the adjudicator. Choosing a reranker on
        # proposer metrics alone is how a reordering that makes the engine
        # decide a coat is footwear scores as an improvement.
        stages = {
            "proposer": proposer,
            "adjudicated": AdjudicatedClassifier(proposer, engine, texts),
        }
        for stage, classifier in stages.items():
            for name, examples in sets.items():
                clock = time.time()
                result = run_eval(
                    f"rerank-{label}-{stage}-{name}", classifier, as_dataset(examples),
                    revision=revision, reports_dir=settings.reports_dir / "rerank",
                )
                per_case = (time.time() - clock) / max(result.n, 1) * 1000
                coverage = result.to_dict()["coverage"]
                report["configurations"].setdefault(label, {}).setdefault(stage, {})[name] = {
                    "top1@6": result.metrics["top1@6"],
                    "top5@6": result.metrics["top5@6"],
                    "top1@10": result.metrics["top1@10"],
                    "ms_per_classification": round(per_case, 1),
                } | coverage
                print(
                    f"  {label:<18} {stage:<12} {name:<13} "
                    f"top1@6={result.metrics['top1@6']:.2%} "
                    f"top5@6={result.metrics['top5@6']:.2%} "
                    f"decided={1 - coverage['escalation_rate']:.1%} "
                    f"on-decided={coverage['accuracy_on_covered@6']:.2%} "
                    f"{per_case:.0f}ms",
                    flush=True,
                )

    path = settings.reports_dir / f"rerank-measurements-{wanted}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
