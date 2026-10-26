"""Retrieval measurements the README cites, run from the committed code.

Every number in the README's retrieval section comes from here. That is the
point of the script existing: the first version of those numbers was taken
from a prototype that built its BM25 documents slightly differently from
``lexical_documents``, and the published table could not be reproduced from
the code it described. A cold read caught it by re-running. Anything quoted
about retrieval is now produced by importing the same functions the pipeline
imports.

Three measurements, on both evaluation sets:

1. **Heading recall** for dense, lexical and fused ranking. Recall at the
   heading caps everything below it, so this is the ceiling available to the
   rest of the pipeline.
2. **The descent funnel** - how much of that recall survives the walk, and
   where the right answer is lost between the candidate pool and the output.
3. **Which levels should fuse**, end to end. This is re-run rather than
   inherited because the earlier version of it ranked walks by a score that
   mixed rank-fusion values with cosine similarities.
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.config import get_settings  # noqa: E402
from hsclass.data.atlas import as_dataset, load_split  # noqa: E402
from hsclass.eval.runner import run_eval  # noqa: E402
from hsclass.pipeline.traversal import (  # noqa: E402
    HierarchicalClassifier,
    LevelIndex,
)
from hsclass.retrieval.embedder import get_embedder  # noqa: E402
from hsclass.retrieval.index import VectorIndex  # noqa: E402
from hsclass.retrieval.lexical import (  # noqa: E402
    LexicalIndex,
    reciprocal_rank_fusion,
    terms_in,
)

ROOT = Path(__file__).resolve().parents[1]
ATLAS = ROOT / "data" / "raw" / "atlas"
HELD_OUT_SIZE = 2000
SEED = 20261017
KS = (5, 20, 50)

#: The configurations compared for which levels should fuse the lexical
#: ranking with the dense one. Named by the code widths that fuse.
FUSE_CONFIGURATIONS: dict[str, tuple[int, ...] | None] = {
    "dense-only": None,
    "fuse-heading": (4,),
    "fuse-heading-subheading": (4, 6),
    "fuse-heading-tariff-line": (4, 8),
    "fuse-every-level": (4, 6, 8, 10),
}


def heading_recall(
    examples, queries, vectors, rows, codes, lexical, every_term
) -> dict[str, dict[int, float]]:
    """Recall of the gold heading, by ranking method and cut-off."""
    hits = {
        method: dict.fromkeys(KS, 0)
        for method in ("dense", "lexical", "fused")
    }
    for example, query, terms in zip(examples, queries, every_term, strict=True):
        gold = example.code[:4]
        dense = vectors[rows] @ query
        lex = lexical.scores(terms, rows)
        rankings = {
            "dense": dense,
            "lexical": lex,
            "fused": reciprocal_rank_fusion(dense, lex),
        }
        for method, scores in rankings.items():
            order = np.argsort(-scores)
            for k in KS:
                hits[method][k] += gold in {codes[i] for i in order[:k]}
    n = len(examples)
    return {
        method: {k: count / n for k, count in cuts.items()}
        for method, cuts in hits.items()
    }


def descent_funnel(classifier: HierarchicalClassifier, examples) -> dict[str, float]:
    """Where the walk loses an answer its candidate pool already held."""
    counts = dict.fromkeys(
        ("n", "heading_survived", "subheading_in_pool", "subheading_in_top5"), 0
    )
    ranks: list[int] = []
    for example in examples:
        walks = classifier.walks(example.description)
        counts["n"] += 1
        if example.code[:4] not in {w.steps[0].code for w in walks}:
            continue
        counts["heading_survived"] += 1
        pool = [w.code[:6] for w in walks]
        if example.code[:6] in pool:
            counts["subheading_in_pool"] += 1
            ranks.append(pool.index(example.code[:6]) + 1)
        counts["subheading_in_top5"] += example.code[:6] in pool[:5]
    n = max(counts["n"], 1)
    found = max(counts["subheading_in_pool"], 1)
    return {
        "n": counts["n"],
        "heading_survived_the_descent": counts["heading_survived"] / n,
        "subheading_in_the_pool": counts["subheading_in_pool"] / n,
        "subheading_in_the_top_5": counts["subheading_in_top5"] / n,
        "median_rank_when_in_the_pool": float(np.median(ranks)) if ranks else 0.0,
        "ranked_top_5_when_in_the_pool": sum(r <= 5 for r in ranks) / found,
    }


def main() -> int:
    settings = get_settings()
    revision = settings.require_revision()
    embedder = get_embedder(settings.embedding_backend, settings.embedding_model)
    levelled = VectorIndex.load(settings.processed_dir / "index-levels")

    started = time.time()
    lexical = LexicalIndex.build(levelled.candidates)
    print(
        f"lexical index: {len(lexical)} documents, {len(lexical.postings)} terms, "
        f"built in {time.time() - started:.1f}s",
        flush=True,
    )

    plain, fused_levels = LevelIndex(levelled), LevelIndex(levelled, lexical)
    rows = np.array(plain.headings)
    codes = [levelled.candidates[r].code for r in rows]

    official, _ = load_split(ATLAS / "test.jsonl")
    train, _ = load_split(ATLAS / "train.jsonl")
    held_out = random.Random(SEED).sample(train, min(HELD_OUT_SIZE, len(train)))
    sets = {"test-200": official, "heldout-2000": held_out}

    report: dict[str, dict] = {"revision": revision, "heading_recall": {},
                               "descent": {}, "fuse_levels": {}}

    for name, examples in sets.items():
        queries = embedder.encode([e.description for e in examples])
        terms = [terms_in(e.description) for e in examples]
        recall = heading_recall(
            examples, queries, levelled.vectors, rows, codes, lexical, terms
        )
        report["heading_recall"][name] = {
            method: {f"@{k}": round(v, 4) for k, v in cuts.items()}
            for method, cuts in recall.items()
        }
        print(f"\n{name} - heading recall", flush=True)
        for method, cuts in recall.items():
            print(f"  {method:<9} " + "  ".join(f"@{k}={cuts[k]:.1%}" for k in KS),
                  flush=True)

    print("\ndescent funnel, on the fused heading configuration", flush=True)
    classifier = HierarchicalClassifier(fused_levels, embedder, k=5)
    for name, examples in sets.items():
        funnel = descent_funnel(classifier, examples)
        report["descent"][name] = {k: round(v, 4) for k, v in funnel.items()}
        print(
            f"  {name:<13} heading survived={funnel['heading_survived_the_descent']:.1%} "
            f"subheading in pool={funnel['subheading_in_the_pool']:.1%} "
            f"in top-5={funnel['subheading_in_the_top_5']:.1%} "
            f"(top-5 when present={funnel['ranked_top_5_when_in_the_pool']:.1%}, "
            f"median rank {funnel['median_rank_when_in_the_pool']:.0f})",
            flush=True,
        )

    print("\nwhich levels should fuse, end to end", flush=True)
    out = Path(settings.reports_dir)
    for label, levels in FUSE_CONFIGURATIONS.items():
        classifier = (
            HierarchicalClassifier(plain, embedder, k=5)
            if levels is None
            else HierarchicalClassifier(fused_levels, embedder, k=5, fuse_levels=levels)
        )
        for name, examples in sets.items():
            result = run_eval(
                f"retrieval-{label}-{name}", classifier, as_dataset(examples),
                revision=revision, reports_dir=out / "retrieval",
            )
            report["fuse_levels"].setdefault(label, {})[name] = {
                "top1@6": result.metrics["top1@6"],
                "top5@6": result.metrics["top5@6"],
                "top1@10": result.metrics["top1@10"],
            }
            print(
                f"  {label:<24} {name:<13} top1@6={result.metrics['top1@6']:.2%} "
                f"top5@6={result.metrics['top5@6']:.2%} "
                f"top1@10={result.metrics['top1@10']:.2%}",
                flush=True,
            )

    path = out / "retrieval-measurements.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
