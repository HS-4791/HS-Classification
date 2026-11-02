"""Stage 4: can the explanation stage justify something the rules never decided?

The check was stated before the thing was built, and it is not an accuracy.
An explanation is correct when it is *faithful*: every rule, note and heading
it cites is one the decision actually holds. Three numbers, reported apart
because they answer different questions.

1. **Fabrications by the deterministic renderer.** This must be zero. It is
   not a score to improve - the renderer builds its prose out of the
   decision's own record, so a single fabrication means the citation grammar
   and the renderer disagree about what they are writing, and every number
   below rests on a gate that does not hold. This is the oracle, and it runs
   over every case for free.
2. **Fabrications the gate caught in the model's prose.** How often a fluent
   model, handed a decision and told to describe it, reached for an authority
   that was not there. This prices stage 4's risk, and it is the number the
   architecture's claim depends on.
3. **Completeness.** Of the rules the engine recorded, how many the
   explanation names. Omitting the deciding rule is not false, but it is
   useless as an audit record, and the two failures are worth separating.

What this cannot move, and must not be reported as moving: agreement with the
judgement oracle. That is a property of stage 3's rule path. Stage 4 only
describes the path, so if agreement changes, something is wired wrong.

Usage: ``python -u scripts/measure_explanations.py [--model] [test-200]``.
Without ``--model`` nothing is sent anywhere and the run is free.
"""

from __future__ import annotations

import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.config import get_settings  # noqa: E402
from hsclass.data.atlas import load_split  # noqa: E402
from hsclass.eval.explanation import gri_labels  # noqa: E402
from hsclass.obs.cost import CostLedger  # noqa: E402
from hsclass.pipeline.explain import (  # noqa: E402
    INSTRUCTIONS,
    REFUSAL_FIRST_INSTRUCTIONS,
    Explainer,
    quoted_text,
    render,
    unsupported,
)
from hsclass.pipeline.extract import OpenAITransport  # noqa: E402
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


def say(line: str) -> None:
    """Write without assuming the console encodes the schedule's em dashes."""
    sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()


def rules_recorded(rule_path: list[str]) -> set[str]:
    return gri_labels(" ".join(rule_path))


def main() -> int:
    args = [a for a in sys.argv[1:]]
    use_model = "--model" in args
    # The second prompt, so the trade-off it shows can be re-measured rather
    # than taken on trust. See REFUSAL_FIRST_INSTRUCTIONS.
    refusal_first = "--refusal-first" in args
    wanted = next((a for a in args if not a.startswith("--")), "test-200")

    settings = get_settings()
    revision = settings.require_revision()

    embedder = get_embedder(settings.embedding_backend, settings.embedding_model)
    levelled = VectorIndex.load(settings.processed_dir / "index-levels")
    texts = {c.code: c.text for c in levelled.candidates}
    engine = GRIEngine(
        build_exclusion_graph(load_notes(settings.processed_dir / "notes.json"))
    )
    proposer = HierarchicalClassifier(
        LevelIndex(levelled, LexicalIndex.build(levelled.candidates)), embedder, k=5
    )

    official, _ = load_split(ATLAS / "test.jsonl")
    train, _ = load_split(ATLAS / "train.jsonl")
    examples = (
        official if wanted == "test-200" else random.Random(SEED).sample(train, 2000)
    )

    explainer = None
    ledger = CostLedger()
    if use_model:
        if not settings.openai_api_key:
            raise SystemExit("set OPENAI_API_KEY in .env (which is gitignored)")
        explainer = Explainer(
            OpenAITransport(api_key=settings.openai_api_key, model=settings.llm_model),
            texts,
            instructions=(
                REFUSAL_FIRST_INSTRUCTIONS if refusal_first else INSTRUCTIONS
            ),
        )
    how = f"{settings.llm_model} prose, gated" if use_model else "deterministic only"
    if refusal_first:
        how += ", refusal-first prompt"
    say(f"{wanted}: {len(examples)} cases, {how}")

    rendered_fabrications: list[dict] = []
    model_fabrications: list[dict] = []
    rejected_authorities: Counter[str] = Counter()
    model_completeness: list[float] = []
    sources = Counter()
    unreachable: list[str] = []
    decided = escalated = 0
    started = time.time()

    for index, example in enumerate(examples, start=1):
        candidates = list(proposer(example.description)[0])
        decision = engine.adjudicate(
            extract_facts(example.description), candidates, texts
        )
        decided += decision.code is not None
        escalated += decision.escalated

        # 1. The oracle. Every case, free, and it must come out at zero.
        text = render(decision, texts)
        invented = unsupported(
            text, decision, texts, quoted=quoted_text(decision, texts)
        )
        if invented:
            rendered_fabrications.append(
                {"description": example.description[:70], "cited": invented}
            )

        # 3. Completeness. Measured on the model's prose only. The same
        # number for the renderer was published once at 100% and meant
        # nothing: render joins ``rule_path`` into the text verbatim, so it
        # can only fall below 1.0 if the regex fails to re-parse a label the
        # renderer just copied. It measured the grammar round-tripping, and
        # it sat in the report beside a real measurement looking like one.
        recorded = rules_recorded(decision.rule_path)

        # 2. The model, gated. A network failure costs this one case and is
        # counted, not the whole run: two 200-case runs were lost to a
        # transient DNS outage, and a measurement that throws away 199 good
        # cases because of the 200th is a worse measurement than one that
        # says how many it could not reach.
        if explainer is not None:
            try:
                result = explainer.explain(decision, example.description)
            except httpx.HTTPError as failure:
                unreachable.append(f"{type(failure).__name__}: {failure}"[:120])
                continue
            sources[result.source] += 1
            if result.usage is not None:
                ledger.record(result.usage)
            if result.rejected:
                model_fabrications.append({
                    "description": example.description[:70],
                    "cited": result.rejected,
                })
                rejected_authorities.update(result.rejected)
            if recorded:
                model_completeness.append(
                    len(recorded & gri_labels(result.text)) / len(recorded)
                )

        if index % 50 == 0:
            say(f"  {index}/{len(examples)}  spent ${ledger.total_usd:.4f}")

    n = len(examples)
    report = {
        "revision": revision,
        "set": wanted,
        "n": n,
        "model": settings.llm_model if use_model else None,
        "prompt": ("refusal-first" if refusal_first else "default") if use_model else None,
        "decided": decided,
        "escalated": escalated,
        "deterministic_renderer": {
            "fabrications": len(rendered_fabrications),
            "examples": rendered_fabrications[:5],
            # The denominator that matters. render's escalated branch
            # quotes no schedule text, so the surface that can fabricate is
            # exercised once per DECIDED case, not once per case. Reporting
            # "0 over 200" hid that it was 0 over 4.
            "decided_cases_exercising_the_quoting_branch": decided,
        },
        "model_prose": {
            "attempted": sum(sources.values()),
            "accepted": sources["model"],
            "refused": sources["rules"],
            "refusal_rate": round(sources["rules"] / sum(sources.values()), 4)
            if sources else None,
            "authorities_invented": dict(rejected_authorities.most_common(10)),
            "examples": model_fabrications[:5],
            "mean_rule_completeness": round(
                sum(model_completeness) / len(model_completeness), 4
            ) if model_completeness else None,
            # Reported beside every rate above, because those rates are over
            # what was reached, not over n.
            "unreachable": len(unreachable),
            "unreachable_examples": unreachable[:3],
        } if use_model else None,
        "cost": ledger.summary() | {
            "per_1000_explanations": round(ledger.total_usd / max(n, 1) * 1000, 4)
        } if use_model else None,
        "seconds": round(time.time() - started, 1),
    }

    say(f"\n{decided}/{n} decided, {escalated} escalated")
    det = report["deterministic_renderer"]
    say("\nthe deterministic renderer, over every case:")
    say(f"  fabricated citations          {det['fabrications']:>4}/{n}"
        f"   {'(must be zero)' if det['fabrications'] else '<- zero, as required'}")
    say(f"  of which quote schedule text  {decided:>4}/{n}"
        "   <- the only branch that can fabricate")
    for row in det["examples"]:
        say(f"      {row['description']!r} cited {row['cited']}")


    if report["model_prose"]:
        mp = report["model_prose"]
        say(f"\n{settings.llm_model} prose, gated against the decision:")
        say(f"  accepted                      {mp['accepted']:>4}/{mp['attempted']}")
        if mp["unreachable"]:
            say(f"  never reached (network)       {mp['unreachable']:>4}/{n}"
                f"   every rate below is over {mp['attempted']}, not {n}")
            for line in mp["unreachable_examples"]:
                say(f"      {line}")
        say(f"  refused and replaced          {mp['refused']:>4}/{mp['attempted']}"
            f"   ({mp['refusal_rate']:.1%})")
        if mp["mean_rule_completeness"] is not None:
            say(f"  rules named, of rules applied {mp['mean_rule_completeness']:>7.1%}")
        if mp["authorities_invented"]:
            say("  authorities it reached for that the decision did not hold:")
            for authority, count in mp["authorities_invented"].items():
                say(f"      {authority:<28}{count:>4}")
        for row in mp["examples"][:3]:
            say(f"      {row['description']!r} cited {row['cited']}")
        say(f"\ncost ${ledger.total_usd:.4f} over {n} explanations "
            f"= ${report['cost']['per_1000_explanations']:.4f} per 1,000")

    out = ROOT / "reports" / f"explanations-{wanted}.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    say(f"\nwrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
