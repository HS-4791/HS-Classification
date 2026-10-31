"""Selective prediction: the curve, and what it refuses to pretend.

The engine's refusal is one operating point - 2.5% coverage at 60% - with
nothing to turn. These pin the two things the curve has to get right to be
worth reading: every point carries the number of cases under it, and a
requested coverage is never satisfied by cutting through a run of equal
scores.
"""

from __future__ import annotations

import pytest

from hsclass.eval.calibration import (
    Case,
    accuracy_at,
    base_rate,
    informative_signals,
    lift,
    risk_coverage,
)


def cases(*pairs: tuple[bool, float], name: str = "signal") -> list[Case]:
    return [Case(correct=ok, signals={name: score}) for ok, score in pairs]


#: Ten cases, three correct, and the signal ranks all three correct ones
#: first - a perfectly informative signal.
PERFECT = cases(
    (True, 0.9), (True, 0.8), (True, 0.7),
    (False, 0.6), (False, 0.5), (False, 0.4),
    (False, 0.3), (False, 0.2), (False, 0.1), (False, 0.05),
)

#: Four correct answers, split evenly either side of the halfway cut, so the
#: most-confident half is exactly as accurate as the whole. Four rather than
#: three because a lift of exactly 1.0 at 50% coverage needs an even count:
#: with three correct the top half holds one or two, never one and a half,
#: and the first version of this fixture scored a lift of 1.33 while
#: claiming to be uninformative.
USELESS = cases(
    (True, 0.9), (False, 0.8), (False, 0.7), (True, 0.6), (False, 0.5),
    (True, 0.4), (False, 0.3), (False, 0.2), (True, 0.1), (False, 0.05),
)


def test_the_base_rate_is_what_a_signal_has_to_beat():
    assert base_rate(PERFECT) == 0.3
    assert base_rate(USELESS) == 0.4
    assert base_rate([]) == 0.0


def test_an_informative_signal_concentrates_the_correct_answers():
    point = accuracy_at(PERFECT, "signal", 0.3)
    assert (point.n, point.accuracy) == (3, 1.0)
    assert lift(PERFECT, "signal", 0.3) > 3.0


def test_a_useless_signal_reports_a_lift_of_about_one():
    """The number that says "do not build a threshold on this"."""
    assert lift(USELESS, "signal", 0.5) == 1.0
    assert informative_signals(USELESS, coverage=0.5) == {}


def test_an_informative_lift_carries_the_coverage_it_was_measured_over():
    """A lift without its slice size is not a usable claim.

    ``heading_agreement`` takes too few distinct values to be cut to a
    tenth, so its "10% slice" is 63% of the held-out set. A lift of 1.28
    over 63% of the population is not a selector - it is the base rate with
    a rounding error - and the entry has to say so where it is read.
    """
    # Fifty cases tied at the top, half of them right, against a base of 25%.
    binary = cases(
        *[(i % 2 == 0, 1.0) for i in range(50)],
        *[(False, 0.0) for _ in range(50)],
    )
    found = informative_signals(binary, coverage=0.10)
    assert found["signal"]["widened_by_ties"]
    assert found["signal"]["coverage"] == 0.5   # not the 0.10 requested
    assert found["signal"]["n"] == 50


def test_a_small_slice_does_not_qualify_on_lift_alone():
    """The gate the 1.2 lift threshold failed to be.

    Five correct of twenty is a lift of 1.67 over a 15% base rate, and a
    permutation test puts it at p = 0.16. The first version of this gate
    waved exactly that through on the test split, while a real 1.28 over
    1,253 cases looked weaker beside it. The gate is now a Wilson lower
    bound above the base rate, which has the n in it.
    """
    twenty_good = cases(
        *[(i < 5, 1.0 - i / 100) for i in range(20)],
        *[(i < 25, 0.5 - i / 1000) for i in range(180)],
    )
    assert lift(twenty_good, "signal", 0.10) > 1.2
    assert "signal" not in informative_signals(twenty_good, coverage=0.10)


def test_an_anti_correlated_signal_reports_a_lift_below_one():
    """Worth knowing rather than hiding: it has happened in this project."""
    inverted = cases(
        (False, 0.9), (False, 0.8), (False, 0.7), (False, 0.6), (False, 0.5),
        (False, 0.4), (True, 0.3), (True, 0.2), (True, 0.1), (True, 0.05),
    )
    assert lift(inverted, "signal", 0.5) == 0.0


# ------------------------------------------------------------------- the ties

def test_a_slice_is_never_cut_inside_a_run_of_equal_scores():
    """Asking for the top 10% by a two-valued signal asks for nothing real.

    Four cases share a score of 1.0. A 25% slice of eight cases is two of
    them - but which two is decided by the sort's tie-break, which is
    storage order. Taking all four and saying so is the honest answer; the
    reranker lost real accuracy to exactly this, where 47% of a pool scored
    zero on BM25 and array order separated them.
    """
    binary = cases(
        (True, 1.0), (False, 1.0), (True, 1.0), (False, 1.0),
        (False, 0.0), (False, 0.0), (False, 0.0), (False, 0.0),
    )
    point = accuracy_at(binary, "signal", 0.25)
    assert point.widened_by_ties
    assert point.n == 4                      # the whole tied tier, not two of it
    assert point.coverage == 0.5             # and it says so
    assert point.requested_coverage == 0.25  # alongside what was asked for
    assert point.accuracy == 0.5


def test_a_slice_on_distinct_scores_is_not_widened():
    point = accuracy_at(PERFECT, "signal", 0.5)
    assert not point.widened_by_ties
    assert (point.n, point.coverage) == (5, 0.5)


def test_every_point_carries_its_denominator():
    """At the rules' own coverage the test split has five cases.

    An accuracy there is three right and two wrong, and a comparison
    between two methods on five cases is not a comparison. The n travels
    with the number so it cannot be quoted without it.
    """
    for point in risk_coverage(PERFECT, "signal"):
        assert point.n >= 1
        assert point.to_dict()["n"] == point.n


def test_the_curve_covers_every_requested_level_in_order():
    points = risk_coverage(PERFECT, "signal")
    assert [p.requested_coverage for p in points] == sorted(
        p.requested_coverage for p in points
    )
    assert points[-1].coverage == 1.0
    assert points[-1].accuracy == base_rate(PERFECT)


def test_the_smallest_slice_is_one_case_not_zero():
    """1% of 10 cases rounds to zero, and an empty slice has no accuracy."""
    assert accuracy_at(PERFECT, "signal", 0.01).n == 1


def test_no_cases_is_not_a_crash():
    assert accuracy_at([], "signal", 0.5).n == 0
    assert informative_signals([]) == {}


# ------------------------------------------------- the rules versus a signal

def test_the_signal_is_compared_at_the_rules_own_coverage():
    """Matched coverage, or the comparison flatters whichever is smaller.

    The rules answer a fixed set; the signal's slice is sized to that same
    number, so the two are weighed at equal coverage instead of at whichever
    coverage suits the argument.
    """
    from hsclass.eval.calibration import compare_with_rules

    population = [
        Case(True, {"s": 0.9}, decided_by_rules=False),
        Case(True, {"s": 0.8}, decided_by_rules=False),
        Case(False, {"s": 0.7}, decided_by_rules=True),
        Case(False, {"s": 0.6}, decided_by_rules=True),
        Case(False, {"s": 0.5}, decided_by_rules=False),
        Case(False, {"s": 0.4}, decided_by_rules=False),
    ]
    result = compare_with_rules(population, "s")
    assert result.rules_n == 2 and result.signal_n == 2
    assert result.rules_accuracy == 0.0     # the rules picked two wrong ones
    assert result.signal_accuracy == 1.0    # the signal picked the two right ones


def test_disjoint_slices_are_reported_as_complementary():
    """Dominating on accuracy is not the same as replacing.

    If the signal and the rules pick different cases, the system that
    answers both is the interesting one, and that only shows up in the
    overlap and the union.
    """
    from hsclass.eval.calibration import compare_with_rules

    population = [
        Case(True, {"s": 0.9}, decided_by_rules=False),
        Case(False, {"s": 0.1}, decided_by_rules=True, rules_correct=True),
        Case(False, {"s": 0.5}, decided_by_rules=False),
        Case(False, {"s": 0.4}, decided_by_rules=False),
    ]
    result = compare_with_rules(population, "s")
    assert result.overlap == 0.0            # nothing in common
    assert result.both_n == 0
    assert result.either_n == 2             # and together they answer twice as much
    # Each contributes what it actually returns: the signal's top candidate
    # was right, and the engine's decided code was right on a case whose top
    # candidate was wrong. That second half is the rules earning their keep.
    assert result.either_accuracy == 1.0


def test_identical_slices_report_full_overlap():
    from hsclass.eval.calibration import compare_with_rules

    population = [
        Case(True, {"s": 0.9}, decided_by_rules=True),
        Case(False, {"s": 0.1}, decided_by_rules=False),
    ]
    result = compare_with_rules(population, "s")
    assert result.overlap == 1.0
    assert result.either_n == result.rules_n == result.both_n == 1


def test_a_case_defaults_to_not_decided_by_the_rules():
    """So the curve tests, which say nothing about the rules, stay honest."""
    assert Case(True, {"s": 1.0}).decided_by_rules is False


def test_each_mechanism_is_scored_on_what_it_returns():
    """The bug this file exists to prevent recurring.

    ``correct`` is whether the proposer's top candidate is right - what a
    confidence threshold bets on. ``rules_correct`` is whether the code the
    engine decided is right. Scoring the engine on the top candidate put the
    rules at 0% on the test split while the same report's operating point
    said 60%, because on those five cases the engine overrides a top
    candidate that is wrong every time and gets three of them right. The
    mislabelled column hid the best result in the run.
    """
    from hsclass.eval.calibration import compare_with_rules

    overridden = [
        # The rules answer these two, correcting a wrong top candidate.
        Case(False, {"s": 0.1}, decided_by_rules=True, rules_correct=True),
        Case(False, {"s": 0.2}, decided_by_rules=True, rules_correct=True),
        Case(True, {"s": 0.9}, decided_by_rules=False),
        Case(False, {"s": 0.8}, decided_by_rules=False),
    ]
    result = compare_with_rules(overridden, "s")
    assert result.rules_accuracy == 1.0   # scored on the decided code
    assert result.signal_accuracy == 0.5  # scored on the top candidate


def test_a_comparison_cut_through_ties_says_so():
    """Matched coverage and non-arbitrary slices cannot both be had.

    To compare at the rules' coverage the slice must be exactly that many
    cases, which for a signal with few distinct values cuts through a run of
    equal scores - what ``accuracy_at`` refuses to do. The cut stands,
    because the alternative is comparing two mechanisms at different
    coverages, and the result carries the flag so nobody reads an average
    over an arbitrary subset of a tied tier as a measurement.
    """
    from hsclass.eval.calibration import compare_with_rules

    binary = [
        Case(True, {"s": 1.0}, decided_by_rules=False),
        Case(False, {"s": 1.0}, decided_by_rules=False),
        Case(False, {"s": 1.0}, decided_by_rules=False),
        Case(False, {"s": 0.0}, decided_by_rules=True, rules_correct=True),
    ]
    result = compare_with_rules(binary, "s")
    assert result.signal_n == 1
    assert result.signal_slice_cut_through_ties

    distinct = [
        Case(True, {"s": 0.9}, decided_by_rules=False),
        Case(False, {"s": 0.5}, decided_by_rules=False),
        Case(False, {"s": 0.1}, decided_by_rules=True, rules_correct=True),
    ]
    assert not compare_with_rules(distinct, "s").signal_slice_cut_through_ties


# ------------------------------------- what the cold read found unmeasured

def test_the_overlap_is_reported_beside_what_chance_would_give():
    """Zero overlap between two small slices is the ordinary outcome.

    At 24 cases of 2,000 the expected overlap under independence is 0.29
    cases and P(disjoint) is about 0.75, so "the overlap is zero" was read
    as complementarity when it was a coin landing the common way. The
    expectation now travels with the observation.
    """
    from hsclass.eval.calibration import compare_with_rules

    population = [
        Case(i < 10, {"s": 1.0 - i / 1000}, decided_by_rules=(i >= 990))
        for i in range(1000)
    ]
    result = compare_with_rules(population, "s")
    assert result.rules_n == 10
    assert result.both_n == 0
    # Ten of a thousand against ten of a thousand: a tenth of a case.
    assert result.expected_overlap_if_unrelated == 0.1
    assert result.to_dict()["expected_overlap_if_unrelated"] == 0.1


def test_the_overlap_is_withheld_when_the_slice_cut_a_tied_tier():
    """The number would be an average over an arbitrary subset.

    A published 0.083 for has_composition came from taking 24 cases out of a
    239-case tier that actually contained 23 of the rules' 24 answers - so
    the figure said "unrelated" about the one signal that predicts the
    rules' set best.
    """
    from hsclass.eval.calibration import compare_with_rules

    tier = [Case(False, {"s": 1.0}, decided_by_rules=(i < 2)) for i in range(10)]
    tier += [Case(False, {"s": 0.0}) for _ in range(10)]
    result = compare_with_rules(tier, "s")
    assert result.signal_slice_cut_through_ties
    assert result.overlap is None
    assert result.to_dict()["overlap"] is None
    # The honest figure for a tied signal: the whole tier, and how much of
    # the rules' set is inside it.
    assert (result.full_tier_n, result.rules_in_full_tier) == (10, 2)


def test_a_duplicated_case_cannot_make_accuracy_exceed_one():
    """``both`` was a list over the input and the union a set of ids.

    One object appearing twice gave both_n=2, either_n=1 and an
    either_accuracy of 2.0, which the report formatted as 200.0%.
    """
    from hsclass.eval.calibration import compare_with_rules

    duplicated = Case(True, {"s": 0.9}, decided_by_rules=True, rules_correct=True)
    other = Case(False, {"s": 0.1})
    result = compare_with_rules([duplicated, duplicated, other], "s")
    assert result.both_n <= result.either_n
    assert 0.0 <= result.either_accuracy <= 1.0


def test_a_non_finite_signal_is_refused_rather_than_dropped():
    """A NaN fails ``>= cut`` and vanishes from the slice silently.

    It kept counting in the denominator, so a requested 100% coverage came
    back as 33% with no flag - and ``top1_similarity`` is a dot product,
    which returns NaN for the zero-norm embedding of an empty description.
    """
    import math

    import pytest

    from hsclass.eval.calibration import NonFiniteSignal

    broken = [
        Case(True, {"s": 0.9}),
        Case(False, {"s": math.nan}),
        Case(False, {"s": 0.1}),
    ]
    with pytest.raises(NonFiniteSignal, match="non-finite"):
        accuracy_at(broken, "s", 1.0)


def test_the_specs_own_calibration_test_is_the_simple_one():
    """Force answers on the declined cases and compare.

    Named in docs/HS-PROJECT-CONTEXT.md and missing from the first version
    of this module: if the declined cases would have scored about what the
    answered ones did, the refusal is too cautious. Measured, they do not -
    the ratio is 0.26 on the test split and 0.47 on the held-out 2,000.
    """
    from hsclass.eval.calibration import forced_answer_test

    population = [
        # Answered, and right three times in five.
        Case(False, {"s": 0.1}, decided_by_rules=True, rules_correct=True),
        Case(False, {"s": 0.1}, decided_by_rules=True, rules_correct=True),
        Case(False, {"s": 0.1}, decided_by_rules=True, rules_correct=True),
        Case(False, {"s": 0.1}, decided_by_rules=True, rules_correct=False),
        Case(False, {"s": 0.1}, decided_by_rules=True, rules_correct=False),
        # Declined, and the top candidate is right one time in five.
        Case(True, {"s": 0.1}),
        Case(False, {"s": 0.1}),
        Case(False, {"s": 0.1}),
        Case(False, {"s": 0.1}),
        Case(False, {"s": 0.1}),
    ]
    result = forced_answer_test(population)
    assert (result.answered_n, result.answered_accuracy) == (5, 0.6)
    assert (result.declined_n, result.forced_accuracy) == (5, 0.2)
    assert result.ratio == pytest.approx(1 / 3)
    # The interval is carried too, because 3-of-5 is not a rate.
    assert result.to_dict()["answered_95_low"] < 0.6
