"""Does pruning per parent recover the 36 points the descent loses?

The measured funnel on the held-out 2,000: the gold four-digit heading
survives into the candidate pool 82.45% of the time and the gold six-digit
subheading only 46.30%. That gap is not retrieval - the heading is already in
hand. It is ``_prune`` keeping five *whole paths* per heading, so with roughly
nine tariff lines under each subheading, one subheading can take all five
slots and evict its siblings.

``prune_by="parent"`` groups by the step one level up instead, giving each
subheading its own five lines. Stated before running:

* **expected to rise** - subheading in the pool, from 46.30% toward the
  82.45% the heading recall allows; and top5@6 with it, since the pool is
  what top-5 is drawn from.
* **expected to cost** - latency and pool size. Each surviving subheading now
  keeps its own paths, so the pool grows by roughly the branching factor at
  each level below the heading. If p50 goes from ~30ms to seconds, the gain
  is not free and the trade has to be stated rather than buried.
* **not expected to move** - top1@6 by much on its own. Getting the answer
  into the pool is necessary, not sufficient; the final ranking decides
  top-1, which is what a reranker is for. Claiming a top-1 gain this change
  does not deliver is the error this project keeps catching.

Usage: ``python -u scripts/measure_pruning.py [test-200 | heldout-2000]``.
Costs nothing.
"""

from __future__ import annotations

import json
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.config import get_settings  # noqa: E402
from hsclass.data.atlas import load_split  # noqa: E402
from hsclass.eval.metrics import at_digits  # noqa: E402
from hsclass.pipeline.traversal import HierarchicalClassifier, LevelIndex  # noqa: E402
from hsclass.retrieval.embedder import get_embedder  # noqa: E402
from hsclass.retrieval.index import VectorIndex  # noqa: E402
from hsclass.retrieval.lexical import LexicalIndex  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ATLAS = ROOT / "data" / "raw" / "atlas"
SEED = 20261017

#: Reported at several cut-offs because the product is a shortlist a human
#: reads, not a single answer: "is it in the top 20" is a fair question when
#: the system's own output for 98% of cases is a list for a classifier.
CUTS = (1, 5, 10, 20)


def say(line: str) -> None:
    sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()


def measure(classifier, examples) -> dict:
    hits = dict.fromkeys(CUTS, 0)
    in_pool = heading_survived = 0
    pool_sizes: list[int] = []
    latencies: list[float] = []
    ranks: list[int] = []

    for example in examples:
        gold6 = at_digits(example.code, 6)
        gold4 = example.code[:4]
        started = time.perf_counter()
        walks = classifier.walks(example.description)
        latencies.append((time.perf_counter() - started) * 1000)
        pool_sizes.append(len(walks))

        if gold4 not in {w.steps[0].code for w in walks}:
            continue
        heading_survived += 1
        pool = [at_digits(w.code, 6) for w in walks]
        if gold6 in pool:
            in_pool += 1
            ranks.append(pool.index(gold6) + 1)
        for cut in CUTS:
            hits[cut] += gold6 in pool[:cut]

    n = len(examples)
    return {
        "n": n,
        "heading_in_pool": round(heading_survived / n, 4),
        "subheading_in_pool": round(in_pool / n, 4),
        "subheading_at": {f"top{c}@6": round(hits[c] / n, 4) for c in CUTS},
        "median_rank_when_in_pool": statistics.median(ranks) if ranks else None,
        "pool_size": {
            "mean": round(statistics.mean(pool_sizes), 1),
            "max": max(pool_sizes),
        },
        "latency_ms": {
            "p50": round(statistics.median(latencies), 1),
            "p95": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))], 1),
        },
    }


def main() -> int:
    wanted = sys.argv[1] if len(sys.argv) > 1 else "test-200"
    settings = get_settings()
    revision = settings.require_revision()
    embedder = get_embedder(settings.embedding_backend, settings.embedding_model)
    levelled = VectorIndex.load(settings.processed_dir / "index-levels")
    levels = LevelIndex(levelled, LexicalIndex.build(levelled.candidates))

    official, _ = load_split(ATLAS / "test.jsonl")
    train, _ = load_split(ATLAS / "train.jsonl")
    examples = (
        official if wanted == "test-200" else random.Random(SEED).sample(train, 2000)
    )

    say(f"{wanted}, revision {revision}, {len(examples)} descriptions")
    # per_heading was tuned at 1, 2, 3 and 5 against top-5 accuracy, which
    # this measurement shows barely moves with it. Pool recall is the metric
    # it should have been tuned on, so it is swept here against that.
    configurations = (
        ("heading", 5),
        ("parent", 5),
        ("parent", 10),
        ("parent", 20),
    )
    results = {}
    for mode, per_heading in configurations:
        label = f"{mode}/per_heading={per_heading}"
        say(f"\n  {label} ...")
        classifier = HierarchicalClassifier(
            levels, embedder, k=5, prune_by=mode, per_heading=per_heading
        )
        row = measure(classifier, examples)
        results[label] = row
        say(f"     heading in pool      {row['heading_in_pool']:.2%}")
        say(f"     subheading in pool   {row['subheading_in_pool']:.2%}")
        for cut in CUTS:
            say(f"     top{cut}@6              "
                f"{row['subheading_at'][f'top{cut}@6']:.2%}")
        say(f"     median rank in pool  {row['median_rank_when_in_pool']}")
        say(f"     pool size            mean {row['pool_size']['mean']}, "
            f"max {row['pool_size']['max']}")
        say(f"     latency              p50 {row['latency_ms']['p50']}ms, "
            f"p95 {row['latency_ms']['p95']}ms")

    base = results["heading/per_heading=5"]
    say("\n  against the shipped configuration, in points:")
    for label, row in results.items():
        if label == "heading/per_heading=5":
            continue
        def gain(cut: int, row: dict = row) -> float:
            key = f"top{cut}@6"
            return (row["subheading_at"][key] - base["subheading_at"][key]) * 100

        deltas = "  ".join(f"top{c}@6 {gain(c):+.2f}" for c in CUTS)
        say(f"     {label:<26} pool "
            f"{(row['subheading_in_pool'] - base['subheading_in_pool']) * 100:+.2f}   "
            f"{deltas}   p50 "
            f"{row['latency_ms']['p50'] - base['latency_ms']['p50']:+.1f}ms")

    out = ROOT / "reports" / f"pruning-{wanted}.json"
    out.write_text(
        json.dumps({"revision": revision, "set": wanted, "modes": results}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    say(f"\nwrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
