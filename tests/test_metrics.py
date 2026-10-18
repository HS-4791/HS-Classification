from hsclass.eval.metrics import Prediction, at_digits, evaluate


def test_at_digits_refuses_to_invent_precision():
    assert at_digits("6109.10.00.12", 6) == "610910"
    # A 6-digit gold cannot be scored at 10 digits; it leaves the denominator.
    assert at_digits("610910", 10) is None


def test_six_and_ten_digit_accuracy_are_separate_claims():
    preds = [
        Prediction("6109.10.00.12", ("6109.10.00.99",)),  # right at 6, wrong at 10
        Prediction("6109.10.00.12", ("6109.10.00.12",)),  # right at both
    ]
    r = evaluate(preds, name="t", revision="REV")
    assert r.metrics["top1@6"] == 1.0
    assert r.metrics["top1@10"] == 0.5


def test_short_golds_are_excluded_from_the_ten_digit_denominator():
    preds = [
        Prediction("610910", ("6109.10.00.12",)),
        Prediction("6109.10.00.12", ("6109.10.00.12",)),
    ]
    r = evaluate(preds, name="t", revision="REV")
    assert r.scored["top1@10"] == 1
    assert r.scored["top1@6"] == 2


def test_escalated_cases_are_misses_in_the_headline():
    preds = [Prediction("610910", ("6109.10.00.12",), escalated=True)]
    r = evaluate(preds, name="t", revision="REV")
    assert r.metrics["top1@6"] == 0.0
    assert r.coverage["escalation_rate"] == 1.0


def test_coverage_separates_what_the_system_answered():
    preds = [
        Prediction("610910", ("610910",)),
        Prediction("620462", ("610910",)),
        Prediction("392410", ("392410",), escalated=True),
    ]
    r = evaluate(preds, name="t", revision="REV")
    assert r.coverage["accuracy_on_covered@6"] == 0.5
    # Forcing an answer on the declined case would have been right: the
    # threshold is too cautious, and the number says so.
    assert r.coverage["accuracy_if_forced@6"] == 1.0


def test_top5_counts_a_hit_anywhere_in_the_shortlist():
    preds = [Prediction("610910", ("620462", "611020", "610910"))]
    r = evaluate(preds, name="t", revision="REV")
    assert r.metrics["top1@6"] == 0.0
    assert r.metrics["top5@6"] == 1.0


def test_per_chapter_breakdown():
    preds = [Prediction("610910", ("610910",)), Prediction("390410", ("620462",))]
    r = evaluate(preds, name="t", revision="REV")
    assert r.per_chapter["61"]["top1@6"] == 1.0
    assert r.per_chapter["39"]["top1@6"] == 0.0


def test_empty_eval_does_not_divide_by_zero():
    r = evaluate([], name="t", revision="REV")
    assert r.metrics["top1@6"] == 0.0 and r.n == 0
