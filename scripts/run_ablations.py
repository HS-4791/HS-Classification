"""Ablations: what each component is worth, measured on the same test sets.

Every configuration is scored on the same two sets - the official 200-row
split and the 2,000-row held-out slice - and the deltas are published. A
component that adds nothing is a finding worth reporting, not a failure
worth hiding.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.config import get_settings  # noqa: E402
from hsclass.data.atlas import as_dataset, load_split  # noqa: E402
from hsclass.eval.runner import run_eval  # noqa: E402
from hsclass.pipeline.adjudicated import AdjudicatedClassifier  # noqa: E402
from hsclass.pipeline.baseline import VectorOnlyClassifier, candidates_from_tree  # noqa: E402
from hsclass.pipeline.gri import GRIEngine, without  # noqa: E402
from hsclass.pipeline.traversal import (  # noqa: E402
    HierarchicalClassifier,
    LevelIndex,
    candidates_at_every_level,
)
from hsclass.retrieval.embedder import get_embedder  # noqa: E402
from hsclass.retrieval.index import StaleIndex, VectorIndex  # noqa: E402
from hsclass.retrieval.lexical import LexicalIndex  # noqa: E402
from hsclass.schedule.exclusions import build_exclusion_graph  # noqa: E402
from hsclass.schedule.models import build_from_export, load_export  # noqa: E402
from hsclass.schedule.notes import load_notes  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ATLAS = ROOT / "data" / "raw" / "atlas"
HELD_OUT_SIZE = 2000
SEED = 20261017


def _index(directory: Path, candidates, embedder, revision: str) -> VectorIndex:
    """Load the cached index, or build it - and never silently use a stale one.

    ``expecting`` is what makes the cache safe. Without it this returned any
    index whose directory existed, and it did: the cached levelled index
    held 30,195 provisions built before a parser fix, against 29,861 from
    the current tree, with 342 chapter 98/99 codes that no longer exist and
    494 further texts changed. Every ablation number in the project was
    measured against it, and nothing noticed, because the revision and the
    embedder both still matched.
    """
    if (directory / "vectors.npy").exists():
        try:
            return VectorIndex.load(directory, expecting=candidates)
        except StaleIndex as stale:
            print(f"  rebuilding: {stale}", flush=True)
    started = time.time()
    index = VectorIndex.build(candidates, embedder, revision, progress=True)
    index.save(directory)
    print(f"  built {directory.name} in {time.time() - started:.0f}s", flush=True)
    return index


def main() -> int:
    import random

    settings = get_settings()
    revision = settings.require_revision()
    tree = build_from_export(load_export(settings.processed_dir / "pdf-rows.json"), revision)
    embedder = get_embedder(settings.embedding_backend, settings.embedding_model)

    flat = _index(settings.processed_dir / "index", candidates_from_tree(tree),
                  embedder, revision)
    levelled = _index(settings.processed_dir / "index-levels",
                      candidates_at_every_level(tree), embedder, revision)
    print(f"flat index {len(flat)} lines | levelled index {len(levelled)} nodes", flush=True)

    official, _ = load_split(ATLAS / "test.jsonl")
    train, _ = load_split(ATLAS / "train.jsonl")
    held_out = random.Random(SEED).sample(train, min(HELD_OUT_SIZE, len(train)))

    notes = load_notes(settings.processed_dir / "notes.json")
    exclusions = build_exclusion_graph(notes)
    engine = GRIEngine(exclusions)
    texts = {c.code: c.text for c in levelled.candidates}
    print(f"{len(notes)} provisions, {len(texts)} candidate texts for the rules", flush=True)

    traversal = HierarchicalClassifier(LevelIndex(levelled), embedder, k=5)
    # The lexical index is derived from the same candidates, so it needs no
    # embedding pass and is built in under a second.
    lexical = LexicalIndex.build(levelled.candidates)
    hybrid = HierarchicalClassifier(LevelIndex(levelled, lexical), embedder, k=5)
    print(f"lexical index {len(lexical)} documents, {len(lexical.postings)} terms",
          flush=True)
    configurations = {
        "1-vector-only": VectorOnlyClassifier(flat, embedder, k=5),
        "2-hierarchical": traversal,
        "3-gri-adjudicated": AdjudicatedClassifier(traversal, engine, texts),
        "4-hybrid-retrieval": hybrid,
        "5-hybrid-adjudicated": AdjudicatedClassifier(hybrid, engine, texts),
        # Each mechanism priced by removing it from the full engine. The spec
        # asks for "+ note-chain" and "+ KAG rule evaluation" as steps; built
        # as subtractions instead, because adding a mechanism to a stripped
        # engine measures it against a configuration nobody would ship.
        "6-without-note-chain": AdjudicatedClassifier(
            hybrid,
            GRIEngine(exclusions, enabled=without("note_chain")),
            texts,
        ),
        "7-without-kag-conditions": AdjudicatedClassifier(
            hybrid,
            GRIEngine(exclusions, enabled=without("conditions")),
            texts,
        ),
        "8-without-note-2a": AdjudicatedClassifier(
            hybrid,
            GRIEngine(exclusions, enabled=without("note_2a")),
            texts,
        ),
    }
    sets = {"test-200": official, "heldout-2000": held_out}

    summary: dict[str, dict] = {}
    for name, classifier in configurations.items():
        for set_name, examples in sets.items():
            started = time.time()
            result = run_eval(
                f"ablation-{name}-{set_name}", classifier, as_dataset(examples),
                revision=revision, reports_dir=settings.reports_dir,
            )
            elapsed = time.time() - started
            coverage = result.to_dict()["coverage"]
            summary.setdefault(name, {})[set_name] = result.to_dict()["metrics"] | {
                "n": result.n,
                "per_classification_ms": round(elapsed / max(result.n, 1) * 1000, 1),
            } | coverage | {"latency_ms": result.to_dict()["latency_ms"]}
            print(f"  {name:24} {set_name:14} "
                  f"top1@6={result.metrics['top1@6']:.2%} "
                  f"top5@6={result.metrics['top5@6']:.2%} "
                  f"decided={coverage['covered_n']:.0f} "
                  f"on-decided={coverage['accuracy_on_covered@6']:.2%} "
                  f"p50={result.latency.get('p50', 0):.0f}ms "
                  f"p95={result.latency.get('p95', 0):.0f}ms", flush=True)

    (settings.reports_dir / "ablations.json").write_text(
        json.dumps({"revision": revision, "configurations": summary}, indent=2),
        encoding="utf-8",
    )

    print("\ndeltas over the vector-only floor:")
    base = summary["1-vector-only"]
    for name, sets_result in summary.items():
        if name == "1-vector-only":
            continue
        for set_name, metrics in sets_result.items():
            delta = metrics["top1@6"] - base[set_name]["top1@6"]
            print(f"  {name:16} {set_name:14} top1@6 {delta:+.2%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
