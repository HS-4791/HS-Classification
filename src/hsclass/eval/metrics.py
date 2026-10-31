from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

#: Six digits is the internationally identical level and the primary claim.
#: Ten is US-only and harder. They are different claims and are never merged.
DIGIT_LEVELS = (6, 10)
TOP_KS = (1, 5)


def normalise(code: str | None) -> str:
    return "".join(ch for ch in (code or "") if ch.isdigit())


def at_digits(code: str | None, n: int) -> str | None:
    """Truncate a code to n digits, or None if it is not that long.

    A 6-digit gold cannot be scored at 10 digits. Returning None keeps those
    rows out of the denominator instead of silently counting them as wrong.
    """
    d = normalise(code)
    return d[:n] if len(d) >= n else None


@dataclass(frozen=True)
class Prediction:
    gold: str
    ranked: tuple[str, ...] = ()
    escalated: bool = False

    @property
    def top1(self) -> str | None:
        return self.ranked[0] if self.ranked else None


@dataclass
class EvalResult:
    name: str
    revision: str
    n: int
    metrics: dict[str, float] = field(default_factory=dict)
    scored: dict[str, int] = field(default_factory=dict)
    coverage: dict[str, float] = field(default_factory=dict)
    per_chapter: dict[str, dict] = field(default_factory=dict)
    #: p50, p95 and p99 per classification. A mean would hide the tail, and
    #: the tail is what a latency budget is set against.
    latency: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "schedule_version": self.revision,
            "n": self.n,
            "metrics": {k: round(v, 4) for k, v in self.metrics.items()},
            "scored": self.scored,
            "coverage": {k: round(v, 4) for k, v in self.coverage.items()},
            "latency_ms": {k: round(v, 2) for k, v in self.latency.items()},
            "per_chapter": self.per_chapter,
        }

    def to_markdown(self) -> str:
        lines = [
            f"# {self.name}",
            "",
            f"Schedule version `{self.revision}` - n = {self.n}",
            "",
            "| metric | value | scored on |",
            "| --- | ---: | ---: |",
        ]
        for key, value in self.metrics.items():
            lines.append(f"| {key} | {value:.2%} | {self.scored.get(key, self.n)} |")
        lines += ["", "## Coverage", "", "| metric | value |", "| --- | ---: |"]
        for key, value in self.coverage.items():
            shown = f"{value:.2%}" if "accuracy" in key or key.endswith("rate") else f"{value:g}"
            lines.append(f"| {key} | {shown} |")
        if self.latency:
            lines += ["", f"## Latency per classification (n = {self.n})", "",
                      "| statistic | ms |", "| --- | ---: |"]
            for key, value in self.latency.items():
                lines.append(f"| {key} | {value:.1f} |")
        if self.per_chapter:
            worst = sorted(
                (c for c in self.per_chapter.items() if c[1]["n"] >= 20),
                key=lambda kv: kv[1]["top1@6"],
            )[:10]
            if worst:
                lines += ["", "## Weakest chapters (n >= 20)", "",
                          "| chapter | n | top1@6 |", "| --- | ---: | ---: |"]
                for ch, d in worst:
                    lines.append(f"| {ch} | {d['n']} | {d['top1@6']:.2%} |")
        return "\n".join(lines) + "\n"


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile, no interpolation and no numpy.

    Nearest-rank because an interpolated p95 of 40 samples is a number that
    exists in no run, and these are latencies somebody will compare against
    a budget.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def latency_summary(values: Sequence[float]) -> dict[str, float]:
    """p50, p95, p99 and the mean, with the mean last for a reason."""
    if not values:
        return {}
    # No sample count in here. It lived alongside the percentiles once, in
    # a dict serialised as "latency_ms" under a table header that says every
    # row is milliseconds - so n = 2000 rendered as a two-second statistic
    # in the one table a reviewer reads for the latency story. The count is
    # ``EvalResult.n``, which is the same number.
    return {
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "mean": sum(values) / len(values),
    }


def _hits(preds: Sequence[Prediction], n_digits: int, k: int) -> tuple[int, int]:
    hit = scored = 0
    for p in preds:
        gold = at_digits(p.gold, n_digits)
        if gold is None:
            continue
        scored += 1
        if p.escalated:
            continue
        if gold in {at_digits(c, n_digits) for c in p.ranked[:k]}:
            hit += 1
    return hit, scored


def evaluate(
    preds: Sequence[Prediction],
    *,
    name: str,
    revision: str,
    digit_levels: Sequence[int] = DIGIT_LEVELS,
    ks: Sequence[int] = TOP_KS,
) -> EvalResult:
    """Headline accuracy, coverage, and a per-chapter breakdown.

    An escalated case counts as a miss in the headline numbers - the system
    did not answer. Its value shows up in the coverage block instead, which
    is where a 92%-over-80% system beats an 85%-over-everything one.
    """
    result = EvalResult(name=name, revision=revision, n=len(preds))
    base = min(digit_levels)

    for d in digit_levels:
        for k in ks:
            if k > 1 and d != base:
                continue  # top-5 is only claimed at the jurisdiction-neutral level
            hit, scored = _hits(preds, d, k)
            key = f"top{k}@{d}"
            result.metrics[key] = hit / scored if scored else 0.0
            result.scored[key] = scored

    covered = [p for p in preds if not p.escalated]
    escalated = [p for p in preds if p.escalated]
    hit_cov, scored_cov = _hits(covered, base, 1)
    forced = [Prediction(p.gold, p.ranked, escalated=False) for p in escalated]
    hit_forced, scored_forced = _hits(forced, base, 1)

    result.coverage = {
        "escalation_rate": len(escalated) / len(preds) if preds else 0.0,
        "covered_n": float(len(covered)),
        f"accuracy_on_covered@{base}": hit_cov / scored_cov if scored_cov else 0.0,
        # If forcing an answer on the declined cases scores close to the
        # covered slice, the refusal threshold is too cautious.
        f"accuracy_if_forced@{base}": hit_forced / scored_forced if scored_forced else 0.0,
    }

    buckets: dict[str, list[Prediction]] = defaultdict(list)
    for p in preds:
        chapter = normalise(p.gold)[:2]
        if chapter:
            buckets[chapter].append(p)
    for chapter, group in sorted(buckets.items()):
        hit, scored = _hits(group, base, 1)
        result.per_chapter[chapter] = {
            "n": len(group),
            f"top1@{base}": round(hit / scored, 4) if scored else 0.0,
        }
    return result
