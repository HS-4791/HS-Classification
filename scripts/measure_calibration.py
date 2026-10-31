"""Is the refusal selective, and could anything else refuse better?

The GRI engine answers 2.5% of the test split at 60% accuracy. That is one
operating point with nothing to turn. This asks whether the signals the
pipeline already computes could pick a confident slice of their own, and how
the rules' point compares with them.

Every signal here is available at decision time. A signal computed from the
gold code would make the curve a measure of nothing.
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
from hsclass.eval.calibration import (  # noqa: E402
    COVERAGE_STEPS,
    Case,
    base_rate,
    compare_with_rules,
    forced_answer_test,
    informative_signals,
    risk_coverage,
)
from hsclass.eval.metrics import at_digits  # noqa: E402
from hsclass.pipeline.adjudicated import AdjudicatedClassifier  # noqa: E402
from hsclass.pipeline.gri import GRIEngine, extract_facts  # noqa: E402
from hsclass.pipeline.traversal import (  # noqa: E402
    HierarchicalClassifier,
    LevelIndex,
)
from hsclass.retrieval.embedder import get_embedder  # noqa: E402
from hsclass.retrieval.index import VectorIndex  # noqa: E402
from hsclass.retrieval.lexical import LexicalIndex  # noqa: E402
from hsclass.schedule.exclusions import build_exclusion_graph  # noqa: E402
from hsclass.schedule.notes import load_notes  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ATLAS = ROOT / "data" / "raw" / "atlas"
HELD_OUT_SIZE = 2000
SEED = 20261017


def signals_for(walks, facts) -> dict[str, float]:
    """What is knowable about a classification before seeing the answer."""
    best = walks[0]
    prefixes: list[str] = []
    scores: list[float] = []
    for walk in walks:
        prefix = walk.code[:6]
        if prefix not in prefixes:
            prefixes.append(prefix)
            scores.append(walk.score)
    headings = [w.steps[0].code for w in walks[:20]]
    return {
        # How well the winning line matched at all.
        "top1_similarity": best.score,
        # How far clear of the runner-up it was. A near-tie between two
        # subheadings is the shape of a case the rules should decide, not
        # one retrieval should call.
        #
        # A pool with no runner-up is *maximally* confident, so it scores
        # infinity rather than the zero it used to: zero sorted the one case
        # nobody could disagree about to the very bottom of the ranking.
        # Unreachable on both evaluation sets - the minimum distinct-prefix
        # count is 80 - but reachable at beam=1.
        "margin_to_second": (scores[0] - scores[1]) if len(scores) > 1 else float("inf"),
        # How many of the top twenty walks share the winning heading.
        #
        # Capped at ``per_heading / 20``, which is 0.25, because the beam
        # keeps at most five walks per heading - so this takes five values
        # and is as much a measurement of a tuning constant as of agreement.
        # That cap, not any property of the goods, is why its smallest
        # possible slice is 1,253 of 2,000 cases. Kept because it is what
        # was measured, and labelled so nobody reads the ceiling as a
        # finding about the schedule.
        "top_heading_share_of_20": (
            headings.count(best.steps[0].code) / len(headings) if headings else 0.0
        ),
        # Did stage 1 find anything typed to reason over? The KAG funnel
        # says this is where the engine starves.
        "has_composition": 1.0 if facts.materials else 0.0,
        # A short description carries less to retrieve on.
        "description_terms": float(len(facts.description.split())),
    }


def main() -> int:
    settings = get_settings()
    revision = settings.require_revision()
    embedder = get_embedder(settings.embedding_backend, settings.embedding_model)
    levelled = VectorIndex.load(settings.processed_dir / "index-levels")
    lexical = LexicalIndex.build(levelled.candidates)
    texts = {c.code: c.text for c in levelled.candidates}
    engine = GRIEngine(build_exclusion_graph(load_notes(settings.processed_dir / "notes.json")))
    proposer = HierarchicalClassifier(LevelIndex(levelled, lexical), embedder, k=5)
    adjudicated = AdjudicatedClassifier(proposer, engine, texts, schedule_version=revision)

    official, _ = load_split(ATLAS / "test.jsonl")
    train, _ = load_split(ATLAS / "train.jsonl")
    held_out = random.Random(SEED).sample(train, min(HELD_OUT_SIZE, len(train)))

    report: dict[str, dict] = {"revision": revision, "sets": {}}
    for name, examples in (("test-200", official), ("heldout-2000", held_out)):
        started = time.time()
        cases: list[Case] = []
        rules_decided = rules_correct = 0
        for example in examples:
            gold = at_digits(example.code, 6)
            if gold is None:
                continue
            walks = proposer.walks(example.description)
            if not walks:
                continue
            facts = extract_facts(example.description)
            correct = at_digits(walks[0].code, 6) == gold
            decision, _ = adjudicated.decide(example.description)
            answered = decision.code is not None
            decided_right = answered and at_digits(decision.code, 6) == gold
            if answered:
                rules_decided += 1
                rules_correct += decided_right
            cases.append(Case(
                correct=correct,
                signals=signals_for(walks, facts),
                decided_by_rules=answered,
                rules_correct=decided_right,
            ))

        rate = base_rate(cases)
        entry: dict = {
            "n": len(cases),
            "base_rate_top1@6": round(rate, 4),
            "rules_operating_point": {
                "coverage": round(rules_decided / len(cases), 4) if cases else 0.0,
                "n": rules_decided,
                "accuracy": round(rules_correct / rules_decided, 4) if rules_decided else 0.0,
                # The claim that the rules earn their keep by overriding a
                # wrong top candidate rests on this number, so it belongs in
                # the artefact rather than only in the write-up: the README
                # was quoting a figure the committed report could not
                # reproduce.
                "top1_accuracy_on_the_same_cases": round(
                    sum(c.correct for c in cases if c.decided_by_rules) / rules_decided, 4
                ) if rules_decided else 0.0,
            },
            # The spec's own definition of this measurement, which is
            # simpler than any curve and was missing from the first version.
            "forced_answer_test": forced_answer_test(cases).to_dict(),
            "curves": {},
            "informative_at_10pc": informative_signals(cases),
            "versus_the_rules": {},
            "seconds": round(time.time() - started, 1),
        }
        for signal in sorted(cases[0].signals) if cases else []:
            entry["curves"][signal] = [p.to_dict() for p in risk_coverage(cases, signal)]
            entry["versus_the_rules"][signal] = compare_with_rules(cases, signal).to_dict()
        report["sets"][name] = entry

        print(f"\n=== {name} (n={len(cases)}) ===", flush=True)
        print(f"  base rate, top-1 at six digits: {rate:.2%}", flush=True)
        point = entry["rules_operating_point"]
        print(f"  the rules: {point['coverage']:.2%} coverage, "
              f"{point['accuracy']:.2%} on {point['n']} cases "
              f"(the top candidate on those same cases: "
              f"{point['top1_accuracy_on_the_same_cases']:.2%})", flush=True)
        forced = entry["forced_answer_test"]
        print(f"  the spec's test: answered {forced['answered_accuracy']:.1%} "
              f"[95% low {forced['answered_95_low']:.1%}] on {forced['answered_n']}, "
              f"forced on the declined {forced['forced_accuracy']:.1%} "
              f"[{forced['forced_95_low']:.1%}] on {forced['declined_n']}, "
              f"ratio {forced['ratio']:.2f}", flush=True)
        # Accuracy *and* n in every cell. A single shared "n at each level"
        # row was the first version and it lied about the tied signals: for
        # a binary one like has_composition the smallest possible slice is
        # every case carrying a composition, so its "1%" column is 24 cases
        # on the test split, not 2. Printing one n per row implied all five
        # signals could be cut to the same sizes, and two of them cannot.
        print("  requested coverage    "
              + "".join(f"{s:>12.0%}" for s in COVERAGE_STEPS), flush=True)
        for signal, points in entry["curves"].items():
            row = "".join(f"{p['accuracy']:>7.1%}({p['n']:>3})" for p in points)
            print(f"  {signal:<22}{row}", flush=True)
        print("  (accuracy(n); n larger than the column asks for means the "
              "slice hit a run of equal scores)", flush=True)
        print("  informative at a requested 10% coverage (lift >= 1.2):", flush=True)
        print(f"    "
              f"{entry['informative_at_10pc'] or 'none'}", flush=True)
        print("")
        print("  at the rules' own coverage, which cases does each pick?", flush=True)
        print("  (rules scored on the code they decided; signals on the "
              "proposer's top candidate)", flush=True)
        print("  signal                 rules   signal    both   either   overlap  "
              "chance  tier", flush=True)
        for signal, a in entry["versus_the_rules"].items():
            overlap = ("   tie-cut" if a["overlap"] is None
                       else f"{a['overlap']:>8.1%}")
            print(f"  {signal:<22}"
                  f"{a['rules_accuracy']:>6.1%}  {a['signal_accuracy']:>6.1%}  "
                  f"{a['both_accuracy']:>6.1%}  {a['either_accuracy']:>6.1%}  "
                  f"{overlap}  {a['expected_overlap_if_unrelated']:>6.2f}  "
                  f"{a['rules_in_full_tier']:>2}/{a['full_tier_n']}", flush=True)
        print(f"  (rules answered {entry['rules_operating_point']['n']} cases; each "
              f"signal's slice is the same size. 'chance' is the overlap two "
              f"unrelated slices", flush=True)
        print("   of those sizes would be expected to have; 'tier' is how many of "
              "the rules' cases lie", flush=True)
        print("   inside the signal's whole tied tier, which is the honest figure "
              "where the slice cut one.)", flush=True)

    path = settings.reports_dir / "calibration.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
