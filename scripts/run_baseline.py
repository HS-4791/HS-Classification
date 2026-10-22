"""Build the vector index and score the vector-only baseline.

Two evaluation sets, reported separately and never averaged together:

* **the official 200-row test split**, so the number is comparable to the
  published baselines
* **a larger held-out slice of train**, because 200 rows carry a confidence
  interval of roughly +/-3.5 points, which is wider than most of the deltas
  the later ablations need to show

Nothing is trained on ATLAS - the index is built from the tariff schedule
alone - so a slice of train is a legitimate second test set rather than a
leak. It is sampled with a fixed seed so the same rows are used every run.
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
from hsclass.pipeline.baseline import VectorOnlyClassifier, candidates_from_tree  # noqa: E402
from hsclass.retrieval.embedder import get_embedder  # noqa: E402
from hsclass.retrieval.index import VectorIndex  # noqa: E402
from hsclass.schedule.models import build_from_export, load_export  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ATLAS = ROOT / "data" / "raw" / "atlas"
HELD_OUT_SIZE = 2000
SEED = 20261017


def main() -> int:
    settings = get_settings()
    revision = settings.require_revision()

    rows = load_export(settings.processed_dir / "pdf-rows.json")
    tree = build_from_export(rows, revision)
    candidates = candidates_from_tree(tree)
    print(f"{len(tree)} nodes -> {len(candidates)} searchable tariff lines", flush=True)

    embedder = get_embedder(settings.embedding_backend, settings.embedding_model)
    index_dir = settings.processed_dir / "index"
    if (index_dir / "vectors.npy").exists():
        index = VectorIndex.load(index_dir)
        print(f"loaded index: {len(index)} lines, {index.embedder_name}", flush=True)
    else:
        started = time.time()
        index = VectorIndex.build(candidates, embedder, revision, progress=True)
        index.save(index_dir)
        print(f"built index in {time.time() - started:.0f}s", flush=True)

    classifier = VectorOnlyClassifier(index, embedder, k=5)

    official, dropped = load_split(ATLAS / "test.jsonl")
    print(f"official test split: {len(official)} rows ({dropped} dropped)", flush=True)

    train, _ = load_split(ATLAS / "train.jsonl")
    held_out = random.Random(SEED).sample(train, min(HELD_OUT_SIZE, len(train)))
    print(f"held-out slice of train: {len(held_out)} rows (seed {SEED})", flush=True)

    summary = {}
    for name, examples in (("baseline-test-200", official),
                           ("baseline-heldout-train", held_out)):
        started = time.time()
        result = run_eval(
            name, classifier, as_dataset(examples),
            revision=revision, reports_dir=settings.reports_dir,
        )
        elapsed = time.time() - started
        summary[name] = result.to_dict()["metrics"] | {
            "n": result.n,
            "seconds": round(elapsed, 1),
            "per_classification_ms": round(elapsed / max(result.n, 1) * 1000, 1),
        }
        print(f"\n{result.to_markdown()}")

    (settings.reports_dir / "baseline-summary.json").write_text(
        json.dumps({"revision": revision, "embedder": index.embedder_name,
                    "searchable_lines": len(index), "sets": summary}, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
