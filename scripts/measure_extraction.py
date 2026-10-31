"""Stage 1: what a model reading the description is worth to the engine.

The claim being tested is narrow and the prediction was recorded before the
run: a model will find material *names* far more often than the regex does,
but the *weight percentages* Section XI Note 2(A) turns on can only come
from descriptions that state them. No model can derive "60%" from "a cotton
t-shirt". Since Note 2(A) decides 23 of the engine's 24 held-out answers,
coverage should rise much less than the facts do - and if it does, the
binding constraint moves from the facts to the rules.

Measured here, against the regex in ``pipeline.gri.extract_facts``:

* descriptions yielding any percentage, and yielding a mixture of two or
  more, which is what Note 2(A) needs;
* the adjudicated engine's coverage and accuracy on what it answers;
* what the model invented and the verification guard dropped;
* real spend from the ledger, not an estimate.

Usage: ``python scripts/measure_extraction.py [test-200 | heldout-2000]``.
The held-out set is 2,000 API calls; the test split is 200 and costs about
a cent.
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.config import get_settings  # noqa: E402
from hsclass.data.atlas import load_split  # noqa: E402
from hsclass.eval.metrics import at_digits  # noqa: E402
from hsclass.obs.cost import CostLedger  # noqa: E402
from hsclass.pipeline.extract import ModelExtractor, OpenAITransport  # noqa: E402
from hsclass.pipeline.gri import GRIEngine, extract_facts  # noqa: E402
from hsclass.pipeline.traversal import HierarchicalClassifier, LevelIndex  # noqa: E402
from hsclass.retrieval.embedder import get_embedder  # noqa: E402
from hsclass.retrieval.index import VectorIndex  # noqa: E402
from hsclass.retrieval.lexical import LexicalIndex  # noqa: E402
from hsclass.schedule.exclusions import build_exclusion_graph  # noqa: E402
from hsclass.schedule.notes import load_notes  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ATLAS = ROOT / "data" / "raw" / "atlas"
SEED = 20261017


def summarise(name: str, rows: list[dict], total: int) -> dict:
    """Counts, each with its denominator."""
    with_any = sum(1 for r in rows if r["materials"])
    mixtures = sum(1 for r in rows if len(r["materials"]) > 1)
    return {
        "source": name,
        "n": total,
        "with_a_percentage": with_any,
        "with_a_mixture": mixtures,
        "share_with_a_percentage": round(with_any / total, 4) if total else 0.0,
        "share_with_a_mixture": round(mixtures / total, 4) if total else 0.0,
    }


def main() -> int:
    wanted = sys.argv[1] if len(sys.argv) > 1 else "test-200"
    settings = get_settings()
    revision = settings.require_revision()
    if not settings.openai_api_key:
        raise SystemExit("set OPENAI_API_KEY in .env (which is gitignored)")

    embedder = get_embedder(settings.embedding_backend, settings.embedding_model)
    levelled = VectorIndex.load(settings.processed_dir / "index-levels")
    texts = {c.code: c.text for c in levelled.candidates}
    engine = GRIEngine(build_exclusion_graph(load_notes(settings.processed_dir / "notes.json")))
    proposer = HierarchicalClassifier(
        LevelIndex(levelled, LexicalIndex.build(levelled.candidates)), embedder, k=5
    )

    official, _ = load_split(ATLAS / "test.jsonl")
    train, _ = load_split(ATLAS / "train.jsonl")
    examples = official if wanted == "test-200" else random.Random(SEED).sample(train, 2000)
    print(f"{wanted}: {len(examples)} descriptions through {settings.llm_model}", flush=True)

    extractor = ModelExtractor(
        OpenAITransport(api_key=settings.openai_api_key, model=settings.llm_model)
    )
    ledger = CostLedger()
    regex_rows: list[dict] = []
    model_rows: list[dict] = []
    outcomes = {"regex": {"decided": 0, "right": 0}, "model": {"decided": 0, "right": 0}}
    dropped: list[str] = []
    started = time.time()

    for index, example in enumerate(examples, start=1):
        gold = at_digits(example.code, 6)
        if gold is None:
            continue
        candidates = list(proposer(example.description)[0])

        regex_facts = extract_facts(example.description)
        extraction = extractor.extract(example.description)
        if extraction.usage is not None:
            ledger.record(extraction.usage)
        dropped.extend(extraction.dropped)
        model_facts = extraction.to_facts(example.description)

        regex_rows.append({"materials": regex_facts.materials})
        model_rows.append({
            "materials": model_facts.materials,
            "named_materials": extraction.named_materials,
            "form": extraction.form,
        })

        for label, facts in (("regex", regex_facts), ("model", model_facts)):
            decision = engine.adjudicate(facts, candidates, texts)
            if decision.code is not None:
                outcomes[label]["decided"] += 1
                outcomes[label]["right"] += at_digits(decision.code, 6) == gold

        if index % 50 == 0:
            print(f"  {index}/{len(examples)}  spent ${ledger.total_usd:.4f}", flush=True)

    n = len(regex_rows)
    report = {
        "revision": revision,
        "set": wanted,
        "model": settings.llm_model,
        "facts": [summarise("regex", regex_rows, n), summarise("model", model_rows, n)],
        "named_materials_only": sum(
            1 for r in model_rows if not r["materials"] and r["named_materials"]
        ),
        "form_extracted": sum(1 for r in model_rows if r["form"]),
        "adjudicated": {
            label: {
                "decided": counts["decided"],
                "right": counts["right"],
                "accuracy_on_decided": round(counts["right"] / counts["decided"], 4)
                if counts["decided"] else 0.0,
            }
            for label, counts in outcomes.items()
        },
        "verification": {
            "values_dropped": len(dropped),
            "examples": dropped[:10],
        },
        "cost": ledger.summary() | {"per_1000_classifications": round(
            ledger.total_usd / max(n, 1) * 1000, 4
        )},
        "seconds": round(time.time() - started, 1),
    }

    print("\nfacts extracted:", flush=True)
    for row in report["facts"]:
        print(f"  {row['source']:<6} a percentage: {row['with_a_percentage']:>4}/{row['n']} "
              f"({row['share_with_a_percentage']:.1%})   a mixture: "
              f"{row['with_a_mixture']:>4}/{row['n']} ({row['share_with_a_mixture']:.1%})",
              flush=True)
    print(f"  model also named a material with no percentage in "
          f"{report['named_materials_only']} cases, and a form in "
          f"{report['form_extracted']}", flush=True)
    print("\nthe engine, on the same candidates:", flush=True)
    for label, row in report["adjudicated"].items():
        print(f"  {label:<6} decided {row['decided']:>3}/{n}  "
              f"right {row['right']:>3}  on-decided {row['accuracy_on_decided']:.2%}", flush=True)
    print(f"\nverification dropped {report['verification']['values_dropped']} values", flush=True)
    for example in report["verification"]["examples"][:5]:
        print(f"    {example}", flush=True)
    print(f"\ncost ${ledger.total_usd:.4f} over {n} classifications "
          f"= ${report['cost']['per_1000_classifications']:.2f} per 1,000; "
          f"{ledger.total_tokens} tokens", flush=True)

    path = settings.reports_dir / f"extraction-{wanted}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
