"""The chunking ablation, mapped honestly onto this architecture.

The spec asks for four chunking strategies - fixed-size, structural,
structural plus contextual, small-to-big - measured on the note-dependent
slice only. Three of those four do not apply here, and saying why is part of
the answer rather than a way around it:

* **The schedule is not chunked at all.** It is parsed into a tree, one node
  per tariff line, because chunking by token count would destroy the
  hierarchy and the hierarchy is the legal structure. There is no chunk size
  to vary.
* **The notes are not retrieved.** They are parsed one provision per chunk
  and applied by rule - the exclusion graph and Section XI Note 2(A). Since
  no note is ever fetched by similarity, fixed-size against small-to-big is
  a comparison between two things the pipeline does not do.

What *is* a chunking decision, and a load-bearing one, is the construction
the spec calls out in the same breath: **inherited context**. A tariff line
reads "Of cotton" on its own and is meaningless embedded that way, so every
node is embedded with its full inherited chain - "Section XI - Chapter 61 -
T-shirts, singlets ... - Of cotton". The spec states flatly that without it
"retrieval dies". That is a claim, it has never been measured here, and it
is measurable: build the same index from each node's own description alone
and run the same evaluation.

This rebuilds a 30,000-vector index, which takes about twenty minutes on
CPU. The index is cached, so a second run is cheap.
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
from hsclass.pipeline.traversal import (  # noqa: E402
    HierarchicalClassifier,
    LevelIndex,
    candidates_at_every_level,
)
from hsclass.retrieval.embedder import get_embedder  # noqa: E402
from hsclass.retrieval.index import Candidate, VectorIndex  # noqa: E402
from hsclass.retrieval.lexical import CHAIN_SEPARATOR, LexicalIndex  # noqa: E402
from hsclass.schedule.models import build_from_export, load_export  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ATLAS = ROOT / "data" / "raw" / "atlas"
HELD_OUT_SIZE = 2000
SEED = 20261017


def own_text_only(candidates: list[Candidate]) -> list[Candidate]:
    """The same provisions, stripped of their inherited context.

    Only the last segment of the chain, which is the line as the schedule
    prints it: "Of cotton", "Other", "Weighing not more than 400 g/m2".
    """
    return [
        Candidate(
            code=c.code,
            text=c.text.split(CHAIN_SEPARATOR)[-1].strip() or c.text,
            chapter=c.chapter,
            section=c.section,
        )
        for c in candidates
    ]


def main() -> int:
    settings = get_settings()
    revision = settings.require_revision()
    embedder = get_embedder(settings.embedding_backend, settings.embedding_model)
    tree = build_from_export(
        load_export(settings.processed_dir / "pdf-rows.json"), revision
    )

    current = candidates_at_every_level(tree)
    # Both arms from the same tree. They were 342 provisions apart when
    # the cached index predated a parser fix, so "the same index with
    # and without context" was comparing two different schedules.
    with_chain = VectorIndex.load(
        settings.processed_dir / "index-levels", expecting=current
    )
    stripped_dir = settings.processed_dir / "index-levels-own-text"
    if (stripped_dir / "vectors.npy").exists():
        stripped = VectorIndex.load(stripped_dir)
        print(f"loaded the own-text index: {len(stripped)} nodes", flush=True)
    else:
        candidates = own_text_only(current)
        print(f"embedding {len(candidates)} nodes without inherited context "
              f"(about twenty minutes)", flush=True)
        started = time.time()
        stripped = VectorIndex.build(candidates, embedder, revision, progress=True)
        stripped.save(stripped_dir)
        print(f"  built in {time.time() - started:.0f}s", flush=True)

    official, _ = load_split(ATLAS / "test.jsonl")
    train, _ = load_split(ATLAS / "train.jsonl")
    held_out = random.Random(SEED).sample(train, min(HELD_OUT_SIZE, len(train)))
    sets = {"test-200": official, "heldout-2000": held_out}

    configurations = {
        "inherited-chain": with_chain,
        "own-text-only": stripped,
    }
    report: dict[str, dict] = {"revision": revision, "configurations": {}}
    for label, index in configurations.items():
        classifier = HierarchicalClassifier(
            LevelIndex(index, LexicalIndex.build(index.candidates)), embedder, k=5
        )
        for name, examples in sets.items():
            result = run_eval(
                f"chunking-{label}-{name}", classifier, as_dataset(examples),
                revision=revision, reports_dir=settings.reports_dir / "chunking",
            )
            report["configurations"].setdefault(label, {})[name] = {
                "top1@6": result.metrics["top1@6"],
                "top5@6": result.metrics["top5@6"],
                "top1@10": result.metrics["top1@10"],
                "n": result.n,
            }
            print(f"  {label:<18} {name:<13} "
                  f"top1@6={result.metrics['top1@6']:.2%} "
                  f"top5@6={result.metrics['top5@6']:.2%} "
                  f"top1@10={result.metrics['top1@10']:.2%}", flush=True)

    path = settings.reports_dir / "chunking.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
