"""The costs that are not tokens.

The dollar ceiling in ``obs/cost.py`` is built and unit-tested and has no
call site in the pipeline at all, because stages 1 to 4 make no model calls
- so it could not fire even if it wanted to, and it would have seen nothing
of this pipeline's two actual expenses either way: the cross-encoder
reranker spends zero tokens while taking a classification from 16ms to about
1,900ms, and it takes the process from 472MB to 2.7GB.
"""

from __future__ import annotations

import pytest

from hsclass.obs.resources import (
    Budget,
    LatencyBudgetExceeded,
    ResourceGauge,
    Stopwatch,
    process_memory_mb,
)


def test_a_latency_budget_raises_at_a_stage_boundary():
    budget = Budget(latency_ms=100.0)
    budget.check_latency(50.0, "propose")  # under, nothing happens
    with pytest.raises(LatencyBudgetExceeded) as caught:
        budget.check_latency(150.0, "propose")
    # The stage is in the error, because "it was slow" is not actionable and
    # "it was slow in propose" is.
    assert caught.value.stage == "propose"
    assert caught.value.spent_ms == 150.0


def test_no_budget_means_no_enforcement():
    """Unset by default: a budget that trips mid-evaluation turns a slow
    configuration into a crashed one."""
    Budget().check_latency(10_000.0, "propose")
    Budget(usd=0.05).check_latency(10_000.0, "propose")


def test_a_budget_covers_money_and_time_and_nothing_else():
    """Deliberate, and the reason is in the module docstring.

    The 2.7GB is an arena ONNX reserves within the first twenty
    classifications and holds for the life of the process. A per-request
    ceiling over it would fail on whichever request arrived first and pass
    for every one after.

    Asserted against the field set rather than with ``hasattr``, which was
    the first version: ``hasattr(Budget(), "anything_at_all")`` is also
    False, so that assertion was satisfied by any name and pinned nothing.
    This catches ``memory_mb``, ``memory_ceiling_mb`` and whatever else a
    future hand reaches for.
    """
    assert set(Budget.__dataclass_fields__) == {"usd", "latency_ms"}


def test_memory_is_read_or_reported_as_unknown_never_as_zero():
    """A zero reading is indistinguishable from a small process.

    The first version of this probe left ctypes' argtypes unset, so the
    process handle was truncated on a 64-bit build, the call failed, and the
    zeroed struct came back as 0.0MB - which looked like a measurement and
    was believed for an hour.
    """
    reading = process_memory_mb()
    assert reading is None or (reading[0] > 0 and reading[1] >= reading[0])


def test_the_gauge_names_its_readings_as_process_level():
    """The scope has to be unmistakable where it is read, not only here."""
    keys = ResourceGauge.sample().to_dict()
    assert set(keys) == {"process_working_set_mb", "process_peak_working_set_mb"}
    assert not any(k.startswith("request_") for k in keys)


def test_the_gauge_carries_no_second_request_duration():
    """It held a ``wall_ms`` once, which was two mistakes at once.

    It defaulted to 0.0 - recording a zero as a fact, which is what
    ``process_memory_mb`` returns None to avoid - and it put a second
    request duration in every trace beside ``Trace.latency_ms``, about a
    millisecond apart, with nothing saying which an auditor should believe.
    """
    assert "wall_ms" not in ResourceGauge.sample().to_dict()
    assert set(ResourceGauge.__dataclass_fields__) == {
        "working_set_mb", "peak_working_set_mb"
    }


def test_a_gauge_still_works_where_memory_cannot_be_read(monkeypatch):
    monkeypatch.setattr("hsclass.obs.resources.process_memory_mb", lambda: None)
    gauge = ResourceGauge.sample()
    assert gauge.working_set_mb is None
    assert gauge.to_dict()["process_working_set_mb"] is None


def test_the_stopwatch_measures_forward():
    watch = Stopwatch()
    first = watch.ms
    assert first >= 0.0
    assert watch.ms >= first
