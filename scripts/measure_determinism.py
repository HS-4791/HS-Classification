"""Determinism: the same input, many times, and what varies where.

The spec is specific. "Same input 100 times at temperature 0 **and** at
default temperature - code variance must be zero." That cannot be answered as
one number, because this pipeline has a model in stage 1 and a deterministic
adjudicator in stage 3, and lumping them together would let the second launder
the first. So it is measured in three parts, each with its own claim.

**A. Retrieval (stages 1-2, no model).** The same description must produce the
same candidate list. Embeddings run through ONNX and the search is an exact
matmul, so this should hold exactly; it is checked rather than assumed,
because a non-deterministic proposer would make everything below meaningless.

**B. Adjudication (stage 3).** The same candidates must produce the same code,
the same ``rule_path``, the same citations and the same shortlist - 100 times.
This is the property the architecture asserts, and it is checked on the real
candidate sets rather than on constructed ones. It complements the existing
permutation test, which exhausts all 120 orderings of the top-5 on 30 cases:
that one says order cannot change the answer, this one says repetition cannot.

**C. The model, and whether its variance reaches the code.** `gpt-4o-mini` is
observably not deterministic at temperature 0 - the same 200 descriptions gave
54 then 55 distinct forms, and the same 200 explanations were refused 76 then
84 times. So the question is not whether the model varies but whether the
*code* does. 97.5% of cases escalate, and the rules that do fire turn on which
material predominates by weight, which is coarse next to extraction noise. If
the architecture absorbs the variance, that is the strongest statement this
project can make about having a model in a legal pipeline; if it does not, the
determinism claim has to be narrowed in public.

Part C runs at temperature 0 and with the temperature field omitted entirely,
which tests the provider's own default rather than a guess at what it is.

Usage: ``python -u scripts/measure_determinism.py [--model] [--only-model]
[repeats]``. Without ``--model``, parts A and B run and cost nothing.
``--only-model`` skips them, which matters because they take about ten minutes
of CPU and the first ``--model`` run spent that recomputing a result already
on disk.

Redirect to a file rather than piping: ``> run.log 2>&1``. Piping through
``tail`` buffers until the process exits, which makes a working run
indistinguishable from a hung one.
"""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.config import get_settings  # noqa: E402
from hsclass.data.atlas import load_split  # noqa: E402
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

#: The same seed every other held-out measurement uses, so "the 17 deciding
#: cases" means the same 17 cases as in the ablation reports.
SEED = 20261017

#: Retrieval is the slow part, so it is repeated fewer times than
#: adjudication. Ten identical candidate lists over 200 descriptions is 2,000
#: proposals; a proposer that wandered would show it long before ten.
RETRIEVAL_REPEATS = 10

#: How many descriptions part C sends. Small because each costs 2 x repeats
#: calls, and chosen rather than sampled: see ``interesting``.
MODEL_CASES = 10


def say(line: str) -> None:
    sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()


def decision_fingerprint(decision) -> str:
    """Everything stage 3 committed to, as one comparable string."""
    return json.dumps(
        {
            "code": decision.code,
            "rule_path": decision.rule_path,
            "notes_cited": decision.notes_cited,
            "conditions_applied": decision.conditions_applied,
            "escalated": decision.escalated,
            "shortlist": decision.shortlist,
            "reason": decision.reason,
        },
        sort_keys=True,
    )


def interesting(
    examples, engine, proposer, texts, *, decided_only: bool = False
) -> list:
    """Descriptions where a rule actually fires, plus near misses.

    Testing determinism on escalated cases alone would be vacuous: they return
    None whatever the model says, so zero code variance there is arithmetic
    rather than evidence. The cases that *decide* come first, then cases whose
    descriptions state a composition, where stage 1's variance has somewhere
    to land.

    ``decided_only`` keeps just the deciding cases, and takes as many as it
    finds rather than stopping at ``MODEL_CASES``. The first run mixed 4
    deciding cases with 6 escalating ones and could not say whether the
    variance it saw landed on a case that answered - which is the only place
    the claim means anything.
    """
    decided, with_shares = [], []
    for example in examples:
        facts = extract_facts(example.description)
        candidates = list(proposer(example.description)[0])
        decision = engine.adjudicate(facts, candidates, texts)
        if decision.code is not None:
            decided.append(example)
        elif len(facts.materials) > 1:
            with_shares.append(example)
        if not decided_only and len(decided) + len(with_shares) >= MODEL_CASES * 3:
            break
    if decided_only:
        return decided
    return (decided + with_shares)[:MODEL_CASES]


def main() -> int:
    args = sys.argv[1:]
    decided_only = "--decided-only" in args
    only_model = "--only-model" in args or decided_only
    use_model = "--model" in args or only_model
    repeats = next((int(a) for a in args if a.isdigit()), 100)

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
    examples, _ = load_split(ATLAS / "test.jsonl")
    if decided_only:
        # The test split yields only 4 deciding cases; the held-out sample
        # yields 17, which is still small and said to be small.
        train, _ = load_split(ATLAS / "train.jsonl")
        examples = random.Random(SEED).sample(train, 2000)

    say(f"revision {revision}, {repeats} repeats")

    # ---------------------------------------------------- A. retrieval
    say(f"\nA. retrieval, {RETRIEVAL_REPEATS} repeats over {len(examples)} "
        "descriptions")
    candidate_sets: dict[str, list[str]] = {}
    retrieval_varied = []
    for index, example in enumerate(examples, start=1):
        repeat = 1 if only_model else RETRIEVAL_REPEATS
        seen = set()
        for _ in range(repeat):
            proposed = tuple(proposer(example.description)[0])
            seen.add(proposed)
        candidate_sets[example.description] = list(next(iter(seen)))
        if len(seen) > 1:
            retrieval_varied.append(example.description[:60])
        if index % 500 == 0:
            say(f"   {index}/{len(examples)}")
    if only_model:
        # One sample cannot detect variance. Saying "0 varied" here would
        # read as a verification over every description when nothing was
        # repeated at all; the candidate sets were gathered, not checked.
        say(f"   candidate sets gathered for {len(examples)} descriptions; "
            "NOT checked for variance in this mode")
    else:
        say(f"   descriptions whose candidate list varied   "
            f"{len(retrieval_varied)}/{len(examples)}")
        for row in retrieval_varied[:5]:
            say(f"      {row}")

    # ---------------------------------------------- B. adjudication
    if only_model:
        say("\nB. adjudication  SKIPPED in this mode")
    else:
        say(f"\nB. adjudication, {repeats} repeats over {len(examples)} candidate sets")
    adjudication_varied = []
    for index, example in enumerate(examples, start=1):
        if only_model:
            break
        facts = extract_facts(example.description)
        prints = {
            decision_fingerprint(
                engine.adjudicate(facts, list(candidate_sets[example.description]), texts)
            )
            for _ in range(repeats)
        }
        if len(prints) > 1:
            adjudication_varied.append(example.description[:60])
        if index % 50 == 0:
            say(f"   {index}/{len(examples)}")
    if not only_model:
        say(f"   descriptions whose decision varied         "
            f"{len(adjudication_varied)}/{len(examples)}")
        for row in adjudication_varied[:5]:
            say(f"      {row}")

    report = {
        "revision": revision,
        "repeats": repeats,
        # None for "varied" where nothing was repeated, rather than 0. A zero
        # reads as a verification that passed; this mode gathered candidate
        # sets and checked nothing, and a report that cannot tell those apart
        # publishes a result nobody measured.
        "retrieval": {
            "repeats": 1 if only_model else RETRIEVAL_REPEATS,
            "n": len(examples),
            "varied": None if only_model else len(retrieval_varied),
            "checked": not only_model,
        },
        "adjudication": {
            "repeats": repeats,
            "n": 0 if only_model else len(examples),
            "varied": None if only_model else len(adjudication_varied),
            "checked": not only_model,
        },
        "model": None,
    }

    # ------------------------------------------------------ C. the model
    if use_model:
        if not settings.openai_api_key:
            raise SystemExit("set OPENAI_API_KEY in .env (which is gitignored)")
        cases = interesting(
            examples, engine, proposer, texts, decided_only=decided_only
        )
        say(f"\nC. {settings.llm_model} in stage 1, {repeats} repeats over "
            f"{len(cases)} descriptions, two temperature settings")

        ledger = CostLedger()
        rows = {}
        for label, temperature in (("temperature 0", 0.0), ("provider default", None)):
            facts_varied = codes_varied = 0
            distinct_facts: Counter[int] = Counter()
            examples_of_drift: list[str] = []
            per_case: list[dict] = []
            extractor = ModelExtractor(
                OpenAITransport(
                    api_key=settings.openai_api_key,
                    model=settings.llm_model,
                    temperature=temperature,
                )
            )
            for position, case in enumerate(cases, start=1):
                say(f"      case {position}/{len(cases)}, "
                    f"${ledger.total_usd:.4f} spent")
                seen_facts, seen_codes = set(), set()
                for _ in range(repeats):
                    extraction = extractor.extract(case.description)
                    if extraction.usage is not None:
                        ledger.record(extraction.usage)
                    facts = extraction.to_facts(case.description)
                    seen_facts.add(
                        json.dumps(
                            {
                                "materials": facts.materials,
                                "named": sorted(facts.named_materials),
                            },
                            sort_keys=True,
                        )
                    )
                    decision = engine.adjudicate(
                        facts, list(candidate_sets[case.description]), texts
                    )
                    seen_codes.add(decision.code)
                distinct_facts[len(seen_facts)] += 1
                facts_varied += len(seen_facts) > 1
                # Per case, not just totals: zero code variance means nothing
                # on a case that escalates, so the pairing of "facts varied"
                # with "this case answered" is the whole evidence.
                per_case.append({
                    "description": case.description[:60],
                    "distinct_fact_sets": len(seen_facts),
                    "codes_seen": sorted(str(c) for c in seen_codes),
                    "answered": any(c is not None for c in seen_codes),
                })
                if len(seen_codes) > 1:
                    codes_varied += 1
                    examples_of_drift.append(
                        f"{case.description[:48]} -> {sorted(map(str, seen_codes))}"
                    )
            rows[label] = {
                "descriptions": len(cases),
                "facts_varied": facts_varied,
                "codes_varied": codes_varied,
                "distinct_fact_sets_histogram": dict(sorted(distinct_facts.items())),
                "examples": examples_of_drift[:5],
                "per_case": per_case,
            }
            answered = [c for c in per_case if c["answered"]]
            both = [c for c in answered if c["distinct_fact_sets"] > 1]
            say(f"   {label}:")
            say(f"      extracted facts varied   {facts_varied}/{len(cases)}")
            say(f"      the CODE varied          {codes_varied}/{len(cases)}"
                f"   {'<- spec requires zero' if codes_varied else '<- zero'}")
            say(f"      cases that answered      {len(answered)}/{len(cases)}")
            say(f"      answered AND facts varied {len(both)}"
                "   <- where zero code variance is evidence, not arithmetic")
            for row in both[:4]:
                say(f"         {row['distinct_fact_sets']} fact sets, one code "
                    f"{row['codes_seen']}: {row['description'][:46]}")
            for row in examples_of_drift[:3]:
                say(f"         DRIFT {row}")
        report["model"] = {
            "model": settings.llm_model,
            "settings": rows,
            "cost": ledger.summary() | {"calls_made": len(ledger.entries)},
        }
        say(f"\n   real spend ${ledger.total_usd:.4f} over "
            f"{len(ledger.entries)} calls")

    # A separate file, because this mode skips parts A and B and writing it to
    # the same path would replace their verification with a null.
    name = "determinism-decided" if decided_only else "determinism"
    out = ROOT / "reports" / f"{name}.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    say(f"\nwrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
