"""Retrieval over past rulings, with and without a cross-encoder reranking it.

The check, stated before running. Precedent retrieval by BM25 alone takes
top-1 at six digits from 15.00% - the schedule path - to 52.50% on the
official 200-case test split. The gold code is somewhere in the depth-50
neighbour pool for **83.50%** of cases, so a third of the available accuracy
sits in the order rather than the recall, and that is what a reranker is for.

Expected: a gain much larger than the +4.5 points the same reranker managed
over the schedule pool, because the pairing is different. Over the schedule it
scored a product description against a nomenclature chain - "T-shirts — Of
cotton — Men's (338)" - which is not what a ms-marco cross-encoder was trained
on. Here it scores a description against another *description*, query against
passage, both in the same register.

Not expected: anything above the 83.50% ceiling, and no change to recall at
depth - reranking reorders a pool, it does not enlarge one.

**Everything is reported twice.** 40 of the 200 test descriptions appear
verbatim in the training set, 31 of them with the same ten-digit code, so any
method that retrieves from train collects those free. The leakage-free 160 is
the honest column. The published state of the art on this split, a fine-tuned
LLaMA-3.3-70B, is 57.5% at six digits and benefits from the same duplicates.

Usage: ``python -u scripts/measure_precedent.py [--rerank] [depth]``.
Without ``--rerank`` it costs nothing and takes seconds.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.config import get_settings  # noqa: E402
from hsclass.data.atlas import load_split  # noqa: E402
from hsclass.retrieval.precedent import Precedent, PrecedentIndex  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ATLAS = ROOT / "data" / "raw" / "atlas"

CUTS = (1, 5, 10, 20)

#: The schedule-retrieval figures this is measured against, from
#: reports/ablation-4-hybrid-retrieval-test-200.json.
SCHEDULE = {"top1@6": 0.1500, "top5@6": 0.2250}

#: The published fine-tuned baseline on this same split, for scale.
ATLAS_PAPER = {"top1@6": 0.575, "top1@10": 0.400}


def say(line: str) -> None:
    sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()


def digits(code: str) -> str:
    return "".join(c for c in code if c.isdigit())


def main() -> int:
    args = sys.argv[1:]
    rerank = "--rerank" in args
    depth = next((int(a) for a in args if a.isdigit()), 50)

    settings = get_settings()
    revision = settings.require_revision()
    train, _ = load_split(ATLAS / "train.jsonl")
    test, _ = load_split(ATLAS / "test.jsonl")

    leaked = {e.description.strip() for e in train}
    index = PrecedentIndex(
        [Precedent(e.description, e.code) for e in train], depth=depth
    )

    reranker = None
    if rerank:
        from hsclass.pipeline.rerank import CrossEncoderReranker

        reranker = CrossEncoderReranker(depth=depth)
        say(f"reranking depth {depth} with {reranker.model_name}")

    say(f"revision {revision}: {len(train)} rulings indexed, {len(test)} test cases")

    hits = {
        width: {slice_: dict.fromkeys(CUTS, 0) for slice_ in ("all", "clean")}
        for width in (6, 10)
    }
    ceiling = {"all": 0, "clean": 0}
    clean_n = 0
    latencies: list[float] = []

    for position, example in enumerate(test, start=1):
        is_clean = example.description.strip() not in leaked
        clean_n += is_clean
        slices = ("all", "clean") if is_clean else ("all",)

        started = time.perf_counter()
        for width in (6, 10):
            gold = digits(example.code)[:width]
            ranked = index.codes(
                example.description, digits=width, reranker=reranker
            )
            for slice_ in slices:
                for cut in CUTS:
                    hits[width][slice_][cut] += gold in ranked[:cut]
            if width == 6:
                for slice_ in slices:
                    ceiling[slice_] += gold in ranked
        latencies.append((time.perf_counter() - started) * 1000)
        if rerank and position % 25 == 0:
            say(f"   {position}/{len(test)}")

    n, clean = len(test), max(clean_n, 1)
    say(f"\nleakage-free cases: {clean_n}/{n}"
        f"   (40 descriptions appear verbatim in train)")
    label = "BM25 + cross-encoder" if rerank else "BM25 only"
    say(f"\n{label}        all {n}            clean {clean}")
    for width in (6, 10):
        for cut in CUTS:
            a = hits[width]["all"][cut] / n
            c = hits[width]["clean"][cut] / clean
            say(f"  top{cut}@{width:<3}        {a:>7.2%}  ({hits[width]['all'][cut]:>3})"
                f"      {c:>7.2%}  ({hits[width]['clean'][cut]:>3})")
    say(f"  pool ceiling@6   {ceiling['all'] / n:>7.2%}"
        f"            {ceiling['clean'] / clean:>7.2%}")
    say(f"\n  latency per case  p50 {statistics.median(latencies):.0f}ms")
    say(f"\n  for scale: schedule retrieval top1@6 {SCHEDULE['top1@6']:.2%}, "
        f"published fine-tuned baseline {ATLAS_PAPER['top1@6']:.2%}")

    report = {
        "revision": revision,
        "rulings_indexed": len(train),
        "n": n,
        "leakage_free_n": clean_n,
        "reranked": rerank,
        "depth": depth,
        "model": reranker.model_name if reranker else None,
        "results": {
            f"top{cut}@{width}": {
                "all": round(hits[width]["all"][cut] / n, 4),
                "clean": round(hits[width]["clean"][cut] / clean, 4),
            }
            for width in (6, 10)
            for cut in CUTS
        },
        "pool_ceiling_at_6": {
            "all": round(ceiling["all"] / n, 4),
            "clean": round(ceiling["clean"] / clean, 4),
        },
        "latency_ms_p50": round(statistics.median(latencies), 1),
        "compared_with": {"schedule_retrieval": SCHEDULE, "atlas_paper": ATLAS_PAPER},
    }
    name = "precedent-reranked" if rerank else "precedent-bm25"
    out = ROOT / "reports" / f"{name}-test-200.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    say(f"\nwrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
