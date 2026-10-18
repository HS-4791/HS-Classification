"""Fail the build when accuracy falls below the committed floor.

``reports/baseline.json`` is the floor: the numbers published in the README.
``reports/latest.json`` is what the most recent eval run produced. Until a
baseline exists this is a no-op, so the gate can be wired into CI from day one
and starts biting the moment week 1 commits a number.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOLERANCE = 0.005  # half a point of noise is allowed; a real drop is not


def main() -> int:
    baseline_path = ROOT / "reports" / "baseline.json"
    latest_path = ROOT / "reports" / "latest.json"

    if not baseline_path.exists():
        print("no reports/baseline.json yet - nothing to regress against")
        return 0
    if not latest_path.exists():
        print("no reports/latest.json - run the eval before this gate")
        return 0

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    latest = json.loads(latest_path.read_text(encoding="utf-8"))

    if baseline.get("schedule_version") != latest.get("schedule_version"):
        print(
            f"schedule version changed: baseline {baseline.get('schedule_version')} "
            f"vs latest {latest.get('schedule_version')}. Re-baseline deliberately; "
            "accuracy across revisions is not comparable.",
            file=sys.stderr,
        )
        return 1

    failures = []
    for metric, floor in baseline.get("metrics", {}).items():
        now = latest.get("metrics", {}).get(metric)
        if now is None:
            failures.append(f"{metric}: missing from latest run")
        elif now < floor - TOLERANCE:
            failures.append(f"{metric}: {now:.4f} < {floor:.4f} floor")
        else:
            print(f"ok  {metric}: {now:.4f} (floor {floor:.4f})")

    if failures:
        print("\nregression:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
