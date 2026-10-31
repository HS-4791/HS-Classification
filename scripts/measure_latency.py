"""Latency per configuration: repeated, spread published, floor checked.

The spec asks for p50 and p95, and getting them honestly on this machine
took three attempts. Each failure is kept here because the design is the
result.

**First attempt - take them from the ablation sweep.** Eight configurations
back to back in one process gave the sixth a p50 of 102ms and the seventh
17ms, two configurations sharing one retrieval object and differing only in
the rule engine. Plainly an artefact.

**Second attempt - one fresh process per configuration.** That is what this
script used to do, and its own output falsified the reasoning: the same
configuration measured in two fresh processes forty minutes apart differed
by six times. A fresh interpreter does not isolate a measurement from the
machine it runs on.

**What was actually happening.** The *slow* numbers were the artefact.
Configuration 1 is a single 27,000 x 384 matmul and takes about 11ms; the
sweep that reported it at 42ms was measuring whole-machine contention from
the first classification onward, before any of the long-run effects the
first attempt blamed. So the diagnosis was wrong as well as the number.

**The design that follows.** Repeat each configuration ``REPEATS`` times in
separate processes, publish the median of the medians *and the spread
across runs*, and check the cheapest configuration against a known floor -
if the matmul is not near 11ms the machine is busy and the whole sweep is
discarded rather than published. A spread that is wide relative to the
differences being measured is itself the finding, and it is reported.

Usage: ``python scripts/measure_latency.py`` runs everything, or
``--one <name>`` times a single configuration, which is how the first form
works internally.
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.config import get_settings  # noqa: E402
from hsclass.data.atlas import load_split  # noqa: E402
from hsclass.eval.metrics import latency_summary  # noqa: E402
from hsclass.pipeline.adjudicated import AdjudicatedClassifier  # noqa: E402
from hsclass.pipeline.baseline import VectorOnlyClassifier  # noqa: E402
from hsclass.pipeline.gri import GRIEngine, without  # noqa: E402
from hsclass.pipeline.traversal import HierarchicalClassifier, LevelIndex  # noqa: E402
from hsclass.retrieval.embedder import get_embedder  # noqa: E402
from hsclass.retrieval.index import VectorIndex  # noqa: E402
from hsclass.retrieval.lexical import LexicalIndex  # noqa: E402
from hsclass.schedule.exclusions import build_exclusion_graph  # noqa: E402
from hsclass.schedule.notes import load_notes  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ATLAS = ROOT / "data" / "raw" / "atlas"

#: Discarded, and not re-timed afterwards. The first classifications in a
#: process pay for lazily built structures and are not what a steady-state
#: p50 is asking about.
WARM_UP = 20

#: Separate processes per configuration. Three, because the point is to see
#: the spread: one run cannot tell a 3ms difference between configurations
#: from a 6x difference in how busy the machine was.
REPEATS = 3

#: Configuration 1 is one 27,000 x 384 matmul and nothing else. If it is not
#: near this, the machine is contended and no number from the sweep means
#: anything - a published sweep once had it at 42ms.
FLOOR_CONFIGURATION = "1-vector-only"
FLOOR_P50_MS = 25.0

CONFIGURATIONS = (
    "1-vector-only",
    "2-hierarchical",
    "4-hybrid-retrieval",
    "5-hybrid-adjudicated",
    # Both halves of the pair the first attempt's 6x story was about. The
    # script built to settle 6-against-7 could not measure 6.
    "6-without-note-chain",
    "7-without-kag-conditions",
)


def build(name: str):
    """One configuration, and only the indexes it needs."""
    settings = get_settings()
    embedder = get_embedder(settings.embedding_backend, settings.embedding_model)

    if name == "1-vector-only":
        # The prebuilt index carries its own candidates, so the tree is not
        # needed - and loading it would put 35,000 nodes of unrelated work
        # inside a latency measurement.
        return VectorOnlyClassifier(
            VectorIndex.load(settings.processed_dir / "index"), embedder, k=5
        )

    levelled = VectorIndex.load(settings.processed_dir / "index-levels")
    texts = {c.code: c.text for c in levelled.candidates}
    if name == "2-hierarchical":
        return HierarchicalClassifier(LevelIndex(levelled), embedder, k=5)

    lexical = LexicalIndex.build(levelled.candidates)
    hybrid = HierarchicalClassifier(LevelIndex(levelled, lexical), embedder, k=5)
    if name == "4-hybrid-retrieval":
        return hybrid

    exclusions = build_exclusion_graph(load_notes(settings.processed_dir / "notes.json"))
    if name == "5-hybrid-adjudicated":
        return AdjudicatedClassifier(hybrid, GRIEngine(exclusions), texts)
    if name == "6-without-note-chain":
        return AdjudicatedClassifier(
            hybrid, GRIEngine(exclusions, enabled=without("note_chain")), texts
        )
    if name == "7-without-kag-conditions":
        return AdjudicatedClassifier(
            hybrid, GRIEngine(exclusions, enabled=without("conditions")), texts
        )
    raise SystemExit(f"unknown configuration {name!r}")


def time_one(name: str) -> dict:
    classifier = build(name)
    examples, _ = load_split(ATLAS / "test.jsonl")
    for example in examples[:WARM_UP]:
        classifier(example.description)

    # The warm-up cases are not timed again. Re-timing them put 10% of the
    # sample in twice.
    timings: list[float] = []
    for example in examples[WARM_UP:]:
        started = time.perf_counter()
        classifier(example.description)
        timings.append((time.perf_counter() - started) * 1000)
    return latency_summary(timings) | {"timed_cases": len(timings)}


def run_child(name: str) -> dict | None:
    """Time one configuration in its own interpreter."""
    result = subprocess.run(
        [sys.executable, "-u", __file__, "--one", name],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    if result.returncode != 0:
        tail = (result.stderr.strip().splitlines() or ["no stderr"])[-1]
        print(f"  {name:<26} FAILED: {tail}", flush=True)
        return None
    # The last line *starting with a brace*, not simply the last line: a
    # model-download banner or a shutdown message on stdout would otherwise
    # make json.loads raise and take the whole sweep down with it.
    for line in reversed(result.stdout.splitlines()):
        if line.startswith("{"):
            return json.loads(line)[name]
    print(f"  {name:<26} FAILED: no JSON on stdout", flush=True)
    return None


def main() -> int:
    if "--one" in sys.argv:
        name = sys.argv[sys.argv.index("--one") + 1]
        print(json.dumps({name: time_one(name)}))
        return 0

    settings = get_settings()
    print(f"{REPEATS} fresh processes per configuration, {WARM_UP} warm-up cases "
          f"discarded and not re-timed", flush=True)
    print("  configuration                p50  (spread)       p95      mean   n",
          flush=True)

    report: dict[str, dict] = {
        "revision": settings.require_revision(),
        "repeats": REPEATS,
        "configurations": {},
    }
    for name in CONFIGURATIONS:
        runs = [run for run in (run_child(name) for _ in range(REPEATS)) if run]
        if not runs:
            continue
        p50s = sorted(run["p50"] for run in runs)
        entry = {
            "p50": statistics.median(p50s),
            "p50_lowest": p50s[0],
            "p50_highest": p50s[-1],
            "p95": statistics.median(run["p95"] for run in runs),
            "mean": statistics.median(run["mean"] for run in runs),
            "runs": len(runs),
            "timed_cases": runs[0]["timed_cases"],
        }
        report["configurations"][name] = entry
        print(f"  {name:<26} {entry['p50']:>5.1f}  ({p50s[0]:.1f}-{p50s[-1]:.1f})  "
              f"{entry['p95']:>8.1f}  {entry['mean']:>8.1f}  {entry['timed_cases']:>3}",
              flush=True)

    floor = report["configurations"].get(FLOOR_CONFIGURATION, {}).get("p50")
    report["floor_check"] = {
        "configuration": FLOOR_CONFIGURATION,
        "expected_at_most_ms": FLOOR_P50_MS,
        "measured_ms": floor,
        "trustworthy": floor is not None and floor <= FLOOR_P50_MS,
    }
    print("")
    if not report["floor_check"]["trustworthy"]:
        print(f"DISCARD THIS SWEEP: {FLOOR_CONFIGURATION} measured {floor}ms "
              f"against a floor of {FLOOR_P50_MS}ms. One matmul does not take that "
              f"long; the machine is busy and every row above is contention, "
              f"not cost.", flush=True)
    else:
        print(f"floor check passed: {FLOOR_CONFIGURATION} at {floor:.1f}ms "
              f"(at most {FLOOR_P50_MS}ms)", flush=True)

    path = settings.reports_dir / "latency.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
