"""Selective prediction: when is the top candidate worth trusting?

The GRI engine refuses by rule - it answers when the rules resolve and
declines otherwise - and that refusal is legally meaningful, because what it
returns is a citation rather than a score. It is also a *single operating
point*: 2.5% coverage at 60% accuracy on the test split, and nothing to turn.

This module asks the question that point cannot answer. Ranked by some
confidence signal, how accurate is the top candidate over the most-confident
*x* per cent of cases? That curve is what makes "refuse when unsure"
measurable rather than aspirational.

Four things here exist because a cold read found the first version claiming
them rather than measuring them.

**A denominator travels with every number.** At the rules' own coverage the
test split has five cases, and three-of-five is not a rate. Every ``Point``
and every ``Agreement`` carries its ``n``.

**A difference is not a result until chance is ruled out.** ``Agreement``
reports the overlap a *random* slice of the same size would be expected to
have, because two disjoint small subsets of a large population are the
ordinary outcome of independence rather than evidence of complementarity: at
24 cases of 2,000 the expected overlap is 0.29 cases and P(disjoint) is
about 0.75. An earlier draft read zero overlap as a discovery and said so in
the README. ``informative_signals`` likewise gates on a Wilson lower bound
rather than a bare lift, because five correct answers out of twenty is a
lift of 1.67 and nothing at all.

**Ties are not broken silently.** A signal like "was a composition
extractable" takes two values, so asking for the top 10% of cases by it is
asking for something that does not exist. A requested coverage is widened to
the whole tied tier and says so; where coverage must be matched exactly and
a tier has to be cut, the result says that instead and withholds the overlap
it cannot honestly report.

**Non-finite scores are rejected rather than compared.** A NaN fails every
comparison, so it used to drop out of the selected slice while still
counting in the denominator, shrinking a requested coverage with no flag.
``top1_similarity`` is a dot product, and a zero-norm embedding from an
empty description returns NaN.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

#: Coverage levels the curve is reported at. Fine enough to see a shape,
#: coarse enough that each point rests on a stated number of cases.
COVERAGE_STEPS = (0.01, 0.025, 0.05, 0.10, 0.25, 0.50, 0.75, 1.00)

#: 1.96 standard deviations: a 95% Wilson interval.
Z = 1.96


class NonFiniteSignal(ValueError):
    """A signal value that cannot be ordered - a NaN or an infinity."""


@dataclass(frozen=True)
class Case:
    """One evaluated case: was the top candidate right, and what did we know?

    ``signals`` holds whatever was available *at decision time* - a signal
    computed from the gold code would make the curve a measure of nothing.
    """

    #: Whether the *proposer's top candidate* is right. This is what a
    #: confidence threshold would be betting on, since a threshold answers
    #: with the top candidate or not at all.
    correct: bool
    signals: Mapping[str, float]
    #: Whether the GRI engine answered this case rather than escalating.
    decided_by_rules: bool = False
    #: Whether the code the engine *decided* is right - not the same
    #: question as ``correct``. Scoring the engine on the proposer's top
    #: candidate put the rules at 0% in a report whose own operating point
    #: said 60%, three lines away.
    rules_correct: bool = False


@dataclass(frozen=True)
class Point:
    """Accuracy over the most-confident slice, with the slice's size."""

    requested_coverage: float
    coverage: float
    n: int
    accuracy: float
    threshold: float
    #: True when the requested coverage fell inside a run of equal scores
    #: and the slice was widened to take all of them.
    widened_by_ties: bool = False

    def to_dict(self) -> dict:
        return {
            "requested_coverage": round(self.requested_coverage, 4),
            "coverage": round(self.coverage, 4),
            "n": self.n,
            "accuracy": round(self.accuracy, 4),
            # Units differ by signal - a cosine, a word count, a boolean -
            # so this is readable per signal and meaningless across them.
            "threshold": round(self.threshold, 6),
            "widened_by_ties": self.widened_by_ties,
        }


def wilson_lower_bound(successes: int, n: int, z: float = Z) -> float:
    """Lower end of a 95% interval on a proportion.

    Used instead of a bare lift so a slice of twenty cannot qualify as
    informative on five correct answers.
    """
    if n <= 0:
        return 0.0
    p = successes / n
    denominator = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denominator)


def base_rate(cases: Sequence[Case]) -> float:
    """Accuracy over everything - the line a signal has to beat."""
    return sum(c.correct for c in cases) / len(cases) if cases else 0.0


def _ordered(cases: Sequence[Case], signal: str) -> list[Case]:
    if any(not math.isfinite(c.signals[signal]) for c in cases):
        raise NonFiniteSignal(
            f"signal {signal!r} has non-finite values, which cannot be ranked"
        )
    return sorted(cases, key=lambda c: -c.signals[signal])


def accuracy_at(cases: Sequence[Case], signal: str, coverage: float) -> Point:
    """Accuracy over the most-confident ``coverage`` fraction, by ``signal``.

    The slice is never cut inside a run of equal scores. If the boundary
    falls in one, every case tied at that value is included and
    ``widened_by_ties`` is set, because the alternative is letting the sort's
    tie-break decide which cases count as confident.
    """
    if not cases:
        return Point(coverage, 0.0, 0, 0.0, 0.0)
    ranked = _ordered(cases, signal)

    wanted = max(1, round(coverage * len(ranked)))
    cut = ranked[wanted - 1].signals[signal]
    taken = [c for c in ranked if c.signals[signal] >= cut]
    return Point(
        requested_coverage=coverage,
        coverage=len(taken) / len(ranked),
        n=len(taken),
        accuracy=sum(c.correct for c in taken) / len(taken),
        threshold=cut,
        widened_by_ties=len(taken) > wanted,
    )


def risk_coverage(
    cases: Sequence[Case], signal: str, steps: Sequence[float] = COVERAGE_STEPS
) -> list[Point]:
    """The curve: accuracy at each coverage level, most confident first."""
    return [accuracy_at(cases, signal, step) for step in steps]


def lift(cases: Sequence[Case], signal: str, coverage: float) -> float:
    """How many times the base rate this signal buys at this coverage.

    A lift of 1.0 means the signal is worthless: the most-confident slice is
    no better than a random one. Below 1.0 means it is anti-correlated with
    being right, which is worth knowing and has happened in this project.

    A lift on its own is not evidence - see ``informative_signals``.
    """
    rate = base_rate(cases)
    return accuracy_at(cases, signal, coverage).accuracy / rate if rate else 0.0


def informative_signals(cases: Sequence[Case], coverage: float = 0.10) -> dict[str, dict]:
    """Signals whose confident slice beats the base rate, significantly.

    The gate is a Wilson lower bound above the base rate, not a lift above
    some round number. A lift threshold has no ``n`` in it: on the test
    split ``top1_similarity`` scores five correct of twenty, a lift of 1.67
    that a permutation test puts at p = 0.16, and a 1.2 gate waves it
    through - while ``heading_agreement``'s 1.28 over 1,253 cases, which is
    real, looks weaker.

    Each entry carries the coverage the slice actually reached. A lift of
    1.28 means little when the slice is 63% of the population, which is what
    ``heading_agreement`` does.
    """
    if not cases:
        return {}
    rate = base_rate(cases)
    found: dict[str, dict] = {}
    for signal in sorted(cases[0].signals):
        point = accuracy_at(cases, signal, coverage)
        low = wilson_lower_bound(round(point.accuracy * point.n), point.n)
        if low > rate:
            found[signal] = {
                "lift": round(point.accuracy / rate, 3) if rate else 0.0,
                "accuracy": round(point.accuracy, 4),
                "accuracy_95_low": round(low, 4),
                "coverage": round(point.coverage, 4),
                "n": point.n,
                "widened_by_ties": point.widened_by_ties,
            }
    return found


@dataclass(frozen=True)
class Agreement:
    """How a signal's confident slice overlaps the one the rules answer.

    Dominating the rules on accuracy is not the same as replacing them. If
    the two pick different cases at similar accuracy they may be
    complementary - but "may be" is the operative phrase, which is why
    ``expected_overlap_if_unrelated`` sits beside the observed overlap. Two
    small subsets of a large population are usually disjoint whether or not
    they are related, and an earlier draft read that as a finding.
    """

    rules_n: int
    #: Scored on the code the engine decided.
    rules_accuracy: float
    signal_n: int
    #: Scored on the proposer's top candidate, which is what a threshold
    #: would answer with. The two columns are different bets, not two
    #: measurements of one thing.
    signal_accuracy: float
    both_n: int
    #: Of the cases both pick, how often the engine's decided code is right.
    both_accuracy: float
    either_n: int
    #: Accuracy of a policy answering either slice, taking the engine's code
    #: where the rules fired and the top candidate otherwise. That
    #: preference is a policy choice, declared because it changes the number
    #: whenever the slices overlap.
    either_accuracy: float
    #: Overlap a random slice of the same size would be expected to have.
    #: The observed overlap means nothing without it.
    expected_overlap_if_unrelated: float
    #: How many of the rules' cases fall inside the signal's whole tied
    #: tier, when the slice had to cut one. For a two-valued signal this is
    #: the honest version of the overlap.
    rules_in_full_tier: int = 0
    full_tier_n: int = 0
    #: True when the signal's slice had to be cut inside a run of equal
    #: scores to match the rules' coverage.
    signal_slice_cut_through_ties: bool = False

    @property
    def overlap(self) -> float | None:
        """Share of the rules' answers the signal also picked.

        None when the slice was cut through a tied tier: the number would be
        an average over an arbitrary subset of equal scores. For
        ``has_composition`` a published 0.083 came from taking 24 cases out
        of a 239-case tier that in fact contains 23 of the rules' 24.
        """
        if self.signal_slice_cut_through_ties:
            return None
        return self.both_n / self.rules_n if self.rules_n else 0.0

    def to_dict(self) -> dict:
        return {
            "rules_n": self.rules_n,
            "rules_accuracy": round(self.rules_accuracy, 4),
            "signal_n": self.signal_n,
            "signal_accuracy": round(self.signal_accuracy, 4),
            "both_n": self.both_n,
            "both_accuracy": round(self.both_accuracy, 4),
            "either_n": self.either_n,
            "either_accuracy": round(self.either_accuracy, 4),
            "overlap": None if self.overlap is None else round(self.overlap, 4),
            "expected_overlap_if_unrelated": round(self.expected_overlap_if_unrelated, 4),
            "rules_in_full_tier": self.rules_in_full_tier,
            "full_tier_n": self.full_tier_n,
            "signal_slice_cut_through_ties": self.signal_slice_cut_through_ties,
        }


def _accuracy(chosen: Sequence[Case]) -> float:
    """Accuracy of the proposer's top candidate - what a threshold bets on."""
    return sum(c.correct for c in chosen) / len(chosen) if chosen else 0.0


def _rules_accuracy(chosen: Sequence[Case]) -> float:
    """Accuracy of the code the engine decided - what the rules return."""
    return sum(c.rules_correct for c in chosen) / len(chosen) if chosen else 0.0


def compare_with_rules(cases: Sequence[Case], signal: str) -> Agreement:
    """Match the signal to the rules' coverage, then compare case by case.

    The signal's slice is sized to the number of cases the rules answered,
    so the two are compared at equal coverage rather than at whichever
    coverage flatters one of them.

    Cases are de-duplicated by identity first. ``both`` was a list over the
    input while the union was a set of ids, so one object appearing twice
    gave ``both_n`` greater than ``either_n`` and an ``either_accuracy`` of
    2.0, which the report printed as 200%.
    """
    unique = list({id(c): c for c in cases}.values())
    if not unique:
        return Agreement(0, 0.0, 0, 0.0, 0, 0.0, 0, 0.0, 0.0)

    by_rules = [c for c in unique if c.decided_by_rules]
    ranked = _ordered(unique, signal)
    by_signal = ranked[: len(by_rules)]

    # Matching the rules' coverage exactly means taking exactly this many
    # cases, which for a signal with few distinct values cuts through a run
    # of equal scores - the thing ``accuracy_at`` refuses to do. There is a
    # real tension and no clean way out: widen and the two are no longer
    # compared at equal coverage, cut and the tie-break picks which tied
    # cases count. So the cut stands, the result says it happened, and
    # ``overlap`` is withheld in favour of the whole tier's figures.
    cut_ties = bool(
        by_signal
        and len(ranked) > len(by_signal)
        and ranked[len(by_signal) - 1].signals[signal]
        == ranked[len(by_signal)].signals[signal]
    )
    boundary = by_signal[-1].signals[signal] if by_signal else 0.0
    full_tier = [c for c in ranked if c.signals[signal] >= boundary]

    rules_ids = {id(c) for c in by_rules}
    signal_ids = {id(c) for c in by_signal}
    both = [c for c in unique if id(c) in rules_ids & signal_ids]
    either = [c for c in unique if id(c) in rules_ids | signal_ids]
    return Agreement(
        rules_n=len(by_rules),
        rules_accuracy=_rules_accuracy(by_rules),
        signal_n=len(by_signal),
        signal_accuracy=_accuracy(by_signal),
        both_n=len(both),
        both_accuracy=_rules_accuracy(both),
        either_n=len(either),
        either_accuracy=(
            sum(c.rules_correct if c.decided_by_rules else c.correct for c in either)
            / len(either)
            if either else 0.0
        ),
        expected_overlap_if_unrelated=len(by_rules) * len(by_signal) / len(unique),
        rules_in_full_tier=sum(1 for c in full_tier if c.decided_by_rules),
        full_tier_n=len(full_tier),
        signal_slice_cut_through_ties=cut_ties,
    )


@dataclass(frozen=True)
class ForcedAnswer:
    """The spec's own calibration test, which is simpler than a curve.

    ``docs/HS-PROJECT-CONTEXT.md`` defines escalation calibration as:
    force answers on the declined cases, and if accuracy is close to the
    auto-classified slice the threshold is too cautious. A refusal earns its
    keep when what it answers is substantially more accurate than what it
    declines would have been. This was named in the source document and was
    missing from the first version of this module.
    """

    answered_n: int
    answered_accuracy: float
    declined_n: int
    forced_accuracy: float

    @property
    def ratio(self) -> float:
        """Forced over answered. Near 1.0 means the refusal is too cautious."""
        if not self.answered_accuracy:
            return 0.0
        return self.forced_accuracy / self.answered_accuracy

    def to_dict(self) -> dict:
        return {
            "answered_n": self.answered_n,
            "answered_accuracy": round(self.answered_accuracy, 4),
            "answered_95_low": round(
                wilson_lower_bound(
                    round(self.answered_accuracy * self.answered_n), self.answered_n
                ), 4,
            ),
            "declined_n": self.declined_n,
            "forced_accuracy": round(self.forced_accuracy, 4),
            "forced_95_low": round(
                wilson_lower_bound(
                    round(self.forced_accuracy * self.declined_n), self.declined_n
                ), 4,
            ),
            "ratio": round(self.ratio, 4),
        }


def forced_answer_test(cases: Sequence[Case]) -> ForcedAnswer:
    """Compare what the rules answered against what they declined.

    The declined cases are scored on the proposer's top candidate, because
    that is what forcing an answer would return.
    """
    answered = [c for c in cases if c.decided_by_rules]
    declined = [c for c in cases if not c.decided_by_rules]
    return ForcedAnswer(
        answered_n=len(answered),
        answered_accuracy=_rules_accuracy(answered),
        declined_n=len(declined),
        forced_accuracy=_accuracy(declined),
    )
