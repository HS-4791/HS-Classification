"""Score the deployed service, over HTTP, against the 200-case test split.

The check, stated before running: **the ablation numbers do not describe the
container.** Every accuracy in the README was measured on the adjudicated
pipeline, which proposes candidates from a 44 MB vector index. The image has no
vector index - precedent retrieval is BM25 over ruling text, which is why it is
14.7 MB and starts cold in a second - so it is a different configuration, and
"a number that was not measured is not a result" applies to it like anything
else.

The difference is not hypothetical: the pipeline settles 4 of the 200 test
cases, the container settles 3, and they are not the same 3 - the shortlist
stage 3 adjudicates over is built differently, and stage 2 bounds stage 3.
So the served path gets its own measurement.

What this run found, and what it did not. The precedent tier reproduces the
library's figures exactly - 107/200 at six digits, 95/200 at ten, the counts
behind the 0.535 and 0.475 in reports/precedent-bm25-test-200.json - so the
bundle build is faithful to the index it was made from. It is also what caught
the tie-break bug: this check compared 105 against 108 when the same bundle
ran on two machines, which is the only reason anyone looked. The
decided tier does not: 1 of 3 right where the pipeline's 4 were right 41.7%
of the time. Three cases cannot tell those apart, and the script says so
rather than printing a rate and leaving it to be read as one.

Reported separately, never averaged:

* **the decided tier** - how often the rules settled it, and were right. The
  coverage here is tiny, so the count is printed beside the rate and the rate
  means little on its own.
* **the precedent tier** - how often the suggestion was right, with and
  without the 40 test descriptions that appear verbatim in the training data.
  The leakage-free 160 is the honest column.
* **what the page shows** - the decided answer where the rules settled it, the
  suggestion otherwise. This is the user-visible accuracy and the denominator
  is every case, so a blank answer counts as a miss.

Usage: start the service, then

    python -u scripts/measure_served.py [base-url] [--out NAME.json]

defaulting to ``http://127.0.0.1:8000`` and ``served-test-200.json``. Point it
at the container to measure what actually ships; it takes a few seconds and
costs nothing.

``--out`` exists because a deployment is its own measured result and must not
quietly replace another. The container and the public service run the same
image, so their accuracy columns should agree - but they do not share hardware
or a network, so their latency rows are different numbers about different
things, and overwriting one with the other would lose the comparison that
proves the deployed thing is the thing that was measured.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.data.atlas import load_split  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ATLAS = ROOT / "data" / "raw" / "atlas"
REPORTS = ROOT / "reports"


def say(line: str) -> None:
    sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()


def digits(code: str | None) -> str:
    return "".join(c for c in (code or "") if c.isdigit())


def rate(hit: int, total: int) -> str:
    """A rate never without the count it rests on."""
    if not total:
        return "n/a (0 cases)"
    return f"{hit / total * 100:.1f}% ({hit}/{total})"


#: The library measurement this configuration should reproduce.
LIBRARY = REPORTS / "precedent-bm25-test-200.json"


def compare_with_library(health: dict, counts: dict, n: int) -> dict:
    """Check the reproduction claim instead of asserting it in a docstring.

    "The served path reproduces the library's figures count for count" was
    prose. Nothing opened the file, so nothing would have failed if the two
    drifted - the next person had to redo the comparison by hand. This makes
    the four counts an executable check, and **refuses across revisions**
    rather than quietly comparing, which is what the CI gate does and what
    CLAUDE.md requires: accuracy measured against one schedule revision is not
    comparable to another.
    """
    if not LIBRARY.exists():
        return {"checked": False, "why": f"{LIBRARY.name} is not present"}

    library = json.loads(LIBRARY.read_text(encoding="utf-8"))
    if library.get("revision") != health["schedule_revision"]:
        return {
            "checked": False,
            "why": (
                f"refusing to compare revisions: the library report is "
                f"{library.get('revision')}, the service is "
                f"{health['schedule_revision']}"
            ),
        }

    clean_n = library.get("leakage_free_n") or 0
    expected = {
        "right_6": round(library["results"]["top1@6"]["all"] * n),
        "right_10": round(library["results"]["top1@10"]["all"] * n),
        "clean_right_6": round(library["results"]["top1@6"]["clean"] * clean_n),
        "clean_right_10": round(library["results"]["top1@10"]["clean"] * clean_n),
    }
    actual = {
        "right_6": counts["suggested_6"],
        "right_10": counts["suggested_10"],
        "clean_right_6": counts["clean_6"],
        "clean_right_10": counts["clean_10"],
    }
    differences = {k: (expected[k], actual[k]) for k in expected if expected[k] != actual[k]}
    return {
        "checked": True,
        "source": LIBRARY.name,
        "expected": expected,
        "actual": actual,
        "matches": not differences,
        "differences": differences,
    }


def ask(base: str, description: str) -> tuple[dict, float]:
    """One classification, with the **client's** wall clock beside it.

    The script used to publish ``latency_ms`` from the response body - the
    number the handler timed about itself, started after FastAPI had parsed
    the request and stopped before the response was serialised. A cold read
    measured the gap: median 1.8 ms self-reported against 7.4 ms observed, and
    p95 3.4 ms against 33.1 ms. A script whose docstring says it scores the
    service "over HTTP" has no business reporting the cheaper number alone.
    """
    body = json.dumps({"description": description}).encode("utf-8")
    request = urllib.request.Request(
        base.rstrip("/") + "/classify",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=60) as response:
        answer = json.load(response)
    return answer, (time.perf_counter() - started) * 1000


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile, or None when there is nothing to report.

    ``values[len(values) // 2]`` raised IndexError on an empty split - the one
    arithmetic in this script without a guard - and an index of
    ``int(n * 0.95)`` is simply ``n - 1`` for any n under 20, which reports the
    maximum while calling it a p95.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(int(round(q * (len(ordered) - 1))), len(ordered) - 1))
    # Rounded, because the committed report is the record and a figure like
    # 6.007999996654689 ms implies a precision wall-clock timing does not have.
    return round(ordered[rank], 1)


def show(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} ms"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    flags = [a for a in sys.argv[1:] if a.startswith("-")]
    # A non-URL argument used to be ignored silently, so passing "8100"
    # measured port 8000 and reported it as though that had been asked for.
    if args and not args[0].startswith("http"):
        say(f"not a base URL: {args[0]!r}")
        say("usage: python -u scripts/measure_served.py [http://host:port] [--out NAME.json]")
        return 2
    base = args[0] if args else "http://127.0.0.1:8000"

    # Unknown flags are rejected rather than dropped, for the reason above: a
    # silently ignored argument makes the run report something other than what
    # was asked for.
    report_name = "served-test-200.json"
    for flag in flags:
        if flag.startswith("--out="):
            report_name = flag.split("=", 1)[1]
        else:
            say(f"unknown option: {flag!r}")
            say("usage: python -u scripts/measure_served.py [http://host:port] [--out NAME.json]")
            return 2
    if not report_name.endswith(".json") or "/" in report_name or "\\" in report_name:
        say(f"--out wants a bare .json filename inside reports/, got {report_name!r}")
        return 2

    try:
        with urllib.request.urlopen(base.rstrip("/") + "/health", timeout=10) as resp:
            health = json.load(resp)
    except (urllib.error.URLError, OSError) as problem:
        say(f"no service at {base}: {problem}")
        say("start one first: uvicorn --factory hsclass.serve.app:create_app")
        return 1

    train, _ = load_split(ATLAS / "train.jsonl")
    test, _ = load_split(ATLAS / "test.jsonl")
    # The same leakage definition the other measurements use, so the columns
    # are comparable with reports/precedent-*.json.
    leaked = {e.description.strip() for e in train}

    say(
        f"{base} - revision {health['schedule_revision']}, "
        f"{health['rulings_indexed']} rulings indexed, "
        f"{health['provisions']} provisions"
    )
    say(f"{len(test)} test cases, {len(train)} training rulings")

    counts = dict.fromkeys(
        (
            "decided",
            "decided_6",
            "decided_10",
            "suggested",
            "suggested_6",
            "suggested_10",
            "clean",
            "clean_6",
            "clean_10",
            "leaked",
            "shown",
            "shown_6",
            "shown_10",
            "stale_suffix",
            "stale_and_right_6",
        ),
        0,
    )
    observed: list[float] = []
    self_reported: list[float] = []
    # Why each flagged code is not in the revision. Counted rather than
    # assumed: the page used to assert "renumbered statistical suffix" for all
    # of them, which held for 22 of 35.
    reasons: dict[str, int] = {}
    decided_cases: list[dict] = []

    for example in test:
        answer, wall_clock = ask(base, example.description)
        observed.append(wall_clock)
        self_reported.append(float(answer["latency_ms"]))
        gold = digits(example.code)
        is_leaked = example.description.strip() in leaked
        counts["leaked"] += 1 if is_leaked else 0
        counts["clean"] += 0 if is_leaked else 1

        if answer["decided"]:
            code = digits(answer["code"])
            counts["decided"] += 1
            counts["decided_6"] += 1 if code[:6] == gold[:6] else 0
            counts["decided_10"] += 1 if code == gold else 0
            # Listed individually, not just totalled: identical totals once hid
            # a dress classified as footwear in this project.
            decided_cases.append(
                {
                    "description": example.description[:120],
                    "code": answer["code"],
                    "gold": example.code,
                    "right_6": code[:6] == gold[:6],
                    "right_10": code == gold,
                    "rule_path": answer["rule_path"],
                    "leaked": is_leaked,
                }
            )

        if answer["suggested"]:
            code = digits(answer["suggested"])
            right_6 = code[:6] == gold[:6]
            right_10 = code == gold
            counts["suggested"] += 1
            counts["suggested_6"] += 1 if right_6 else 0
            counts["suggested_10"] += 1 if right_10 else 0
            if not is_leaked:
                counts["clean_6"] += 1 if right_6 else 0
                counts["clean_10"] += 1 if right_10 else 0
            # How often the suggested code is not a line in the pinned
            # revision at all. The rulings are historical and the statistical
            # suffixes get renumbered, so this is schedule drift, not a bug -
            # but a code offered under a banner naming the revision has to say
            # when the revision does not contain it.
            if answer.get("suggested_in_revision") is False:
                counts["stale_suffix"] += 1
                counts["stale_and_right_6"] += 1 if right_6 else 0
                why = answer.get("suggested_reason") or "unstated"
                reasons[why] = reasons.get(why, 0) + 1

        # What the reader is actually shown: the decided answer where the rules
        # settled it, the suggestion otherwise. Adding the two tiers' hits
        # would double count the cases where both are right.
        shown = answer["code"] if answer["decided"] else answer["suggested"]
        if shown:
            code = digits(shown)
            counts["shown"] += 1
            counts["shown_6"] += 1 if code[:6] == gold[:6] else 0
            counts["shown_10"] += 1 if code == gold else 0

    n = len(test)
    wall = {q: percentile(observed, q) for q in (0.5, 0.95)}
    inner = {q: percentile(self_reported, q) for q in (0.5, 0.95)}

    say("")
    say("LATENCY - both, because they are not the same claim")
    say(
        f"  observed over HTTP   median {show(wall[0.5])}, "
        f"p95 {show(wall[0.95])}"
    )
    say(
        f"  server self-reported median {show(inner[0.5])}, "
        f"p95 {show(inner[0.95])}"
    )
    say("  the first is what a caller waits; the second excludes request")
    say("  parsing and response serialisation, so quote it as that or not at all")
    say("")
    say("DECIDED TIER - the rules settled it")
    say(f"  coverage         {rate(counts['decided'], n)}")
    say(f"  right 6 digits   {rate(counts['decided_6'], counts['decided'])}")
    say(f"  right 10 digits  {rate(counts['decided_10'], counts['decided'])}")
    if counts["decided"] < 20:
        say(
            f"  CAVEAT: {counts['decided']} cases. That is far too few for a "
            "rate; read the counts, not the percentage."
        )
    for case in decided_cases:
        mark = "ok " if case["right_6"] else "WRONG"
        say(
            f"    {mark} {case['code']} vs gold {case['gold']}  "
            f"{'>'.join(case['rule_path'])}"
        )
        say(f"         {case['description']}")
    say("")
    say("PRECEDENT TIER - nearest CBP ruling")
    say(f"  coverage         {rate(counts['suggested'], n)}")
    say(f"  right 6 digits   {rate(counts['suggested_6'], n)}")
    say(f"  right 10 digits  {rate(counts['suggested_10'], n)}")
    say(f"  leaked cases     {counts['leaked']} of {n} verbatim in train")
    say(f"  clean 6 digits   {rate(counts['clean_6'], counts['clean'])}")
    say(f"  clean 10 digits  {rate(counts['clean_10'], counts['clean'])}")
    say(
        f"  not in revision  {rate(counts['stale_suffix'], n)}"
        " - the ruling cites a code this revision does not carry"
    )
    for why, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        say(f"    {why}: {count}")
    say(
        "    of those, still right at six digits: "
        f"{rate(counts['stale_and_right_6'], counts['stale_suffix'])}"
        " - the subheading is stable, the US suffix is not"
    )
    say("")
    say("WHAT THE PAGE SHOWS - decided answer if settled, else the suggestion")
    say(f"  answered at all  {rate(counts['shown'], n)}")
    say(f"  right 6 digits   {rate(counts['shown_6'], n)}")
    say(f"  right 10 digits  {rate(counts['shown_10'], n)}")
    say("  escalations and blanks count as misses: the denominator is every case")

    report = {
        "revision": health["schedule_revision"],
        "set": "test-200",
        "configuration": "served - BM25 precedent retrieval, no vector index",
        "base_url": base,
        "n": n,
        "rulings_indexed": health["rulings_indexed"],
        "latency_ms": {
            "observed_over_http": {"median": wall[0.5], "p95": wall[0.95]},
            "server_self_reported": {"median": inner[0.5], "p95": inner[0.95]},
        },
        "decided": {
            "coverage": counts["decided"] / n,
            "n": counts["decided"],
            "right_6": counts["decided_6"],
            "right_10": counts["decided_10"],
            "cases": decided_cases,
        },
        "precedent": {
            "n": counts["suggested"],
            "right_6": counts["suggested_6"],
            "right_10": counts["suggested_10"],
            "leaked": counts["leaked"],
            "clean_n": counts["clean"],
            "clean_right_6": counts["clean_6"],
            "clean_right_10": counts["clean_10"],
            "not_in_revision": counts["stale_suffix"],
            "not_in_revision_right_6": counts["stale_and_right_6"],
            "not_in_revision_reasons": reasons,
        },
        "shown": {
            "n": counts["shown"],
            "right_6": counts["shown_6"],
            "right_10": counts["shown_10"],
        },
        "reproduces_library": compare_with_library(health, counts, n),
    }
    check = report["reproduces_library"]
    say("")
    say("REPRODUCTION CHECK against the library measurement")
    if not check["checked"]:
        say(f"  not checked - {check['why']}")
    elif check["matches"]:
        say(f"  matches {check['source']} count for count: {check['actual']}")
    else:
        say(f"  DOES NOT MATCH {check['source']}")
        for key, (want, got) in check["differences"].items():
            say(f"    {key}: library {want}, served {got}")

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / report_name
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    say("")
    say(f"wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
