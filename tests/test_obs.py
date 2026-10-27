import json

import pytest

from hsclass.obs.cost import CostCeilingExceeded, CostLedger, Usage, cost_per_1000
from hsclass.obs.trace import trace


def test_known_model_prices():
    usage = Usage("claude-opus-5-5", input_tokens=1_000_000, output_tokens=1_000_000)
    assert usage.cost_usd() == pytest.approx(24.0)


def test_unknown_model_fails_loudly():
    with pytest.raises(KeyError, match="no price"):
        Usage("some-model", input_tokens=10).cost_usd()


def test_cache_reads_are_cheaper_than_fresh_input():
    fresh = Usage("claude-opus-5-5", input_tokens=100_000).cost_usd()
    cached = Usage("claude-opus-5-5", cache_read_tokens=100_000).cost_usd()
    assert cached < fresh


def test_ceiling_is_enforced_per_request():
    ledger = CostLedger(ceiling_usd=0.01)
    ledger.record(Usage("claude-haiku-5-5", input_tokens=1000, output_tokens=100))
    with pytest.raises(CostCeilingExceeded):
        ledger.record(Usage("claude-opus-5-5", input_tokens=500_000))


def test_cost_per_1000():
    assert cost_per_1000(0.02, 1) == pytest.approx(20.0)
    assert cost_per_1000(1.0, 0) == 0.0


def test_a_classification_is_reconstructable_from_its_trace(tmp_path):
    sink = tmp_path / "traces.jsonl"
    with trace(schedule_version="REV1", input={"description": "knitted t-shirt"},
               sink=sink, cost_ceiling_usd=1.0) as t:
        with t.stage("propose") as s:
            s["candidates"] = ["6109", "6110"]
        with t.stage("adjudicate") as s:
            s["rules_fired"] = ["GRI 1", "GRI 6"]
            s["notes_consulted"] = ["Section XI Note 2(A)"]
        t.ledger.record(Usage("claude-opus-5-5", input_tokens=500, output_tokens=50))
        t.decide("6109.10.00", gri_path=["GRI 1", "GRI 6"],
                 notes_cited=["Section XI Note 2(A)"])

    record = json.loads(sink.read_text(encoding="utf-8").strip())
    assert record["schedule_version"] == "REV1"
    assert record["decision"]["code"] == "6109.10.00"
    assert [s["name"] for s in record["stages"]] == ["propose", "adjudicate"]
    assert record["stages"][0]["candidates"] == ["6109", "6110"]
    assert record["cost"]["total_usd"] > 0


def test_the_trace_is_written_even_when_the_request_fails(tmp_path):
    sink = tmp_path / "traces.jsonl"
    with pytest.raises(ValueError):
        with trace(schedule_version="REV1", input={}, sink=sink):
            raise ValueError("boom")
    assert sink.exists() and sink.read_text(encoding="utf-8").strip()


# ------------------------------------------- tracing wired into the pipeline

def _pipeline(tmp_path, **kwargs):
    """A minimal adjudicated pipeline with a stub proposer."""
    from hsclass.pipeline.adjudicated import AdjudicatedClassifier
    from hsclass.pipeline.gri import GRIEngine
    from hsclass.schedule.exclusions import build_exclusion_graph

    texts = {
        "6109100012": "T-shirts, knitted - Of cotton",
        "6109901000": "T-shirts, knitted - Of other textile materials - polyester",
    }
    return AdjudicatedClassifier(
        lambda d: (list(texts), False),
        GRIEngine(build_exclusion_graph([])),
        texts,
        schedule_version="REV1",
        **kwargs,
    )


def test_every_stage_that_ran_is_timed(tmp_path):
    """The first version timed the bookkeeping, not the work.

    Every stage reported 0.0ms - including propose, which is the expensive
    one. A zero duration is not a missing measurement; it reads as a fast
    stage, which is a wrong measurement.
    """
    sink = tmp_path / "traces.jsonl"
    _pipeline(tmp_path, sink=sink).decide("a cotton t-shirt of 60% cotton 40% polyester")

    record = json.loads(sink.read_text(encoding="utf-8").strip())
    assert [s["name"] for s in record["stages"]] == ["normalise", "propose", "adjudicate"]
    assert all(s["duration_ms"] >= 0 for s in record["stages"])
    assert sum(s["duration_ms"] for s in record["stages"]) > 0
    assert record["latency_ms"] > 0


def test_the_trace_reconstructs_the_decision(tmp_path):
    sink = tmp_path / "traces.jsonl"
    _pipeline(tmp_path, sink=sink).decide("a t-shirt of 60% cotton and 40% polyester")

    record = json.loads(sink.read_text(encoding="utf-8").strip())
    assert record["schedule_version"] == "REV1"
    assert record["decision"]["gri_path"][0] == "GRI 1"
    assert record["stages"][0]["materials"] == {"cotton": 60.0, "polyester": 40.0}
    assert record["stages"][1]["candidates"]
    # Process-level, and labelled as such rather than charged to the request.
    assert "process_working_set_mb" in record["resources"]


def test_no_sink_and_no_budget_writes_nothing(tmp_path):
    """Tracing off is the default for evaluation runs: 2,200 cases would
    otherwise write 2,200 records nobody reads."""
    classifier = _pipeline(tmp_path)
    decision, _ = classifier.decide("a cotton t-shirt")
    assert decision is not None
    assert not list(tmp_path.iterdir())


def test_tracing_does_not_change_the_decision(tmp_path):
    """Instrumentation that alters the result is worse than none."""
    description = "a t-shirt of 60% cotton and 40% polyester"
    traced, _ = _pipeline(tmp_path, sink=tmp_path / "t.jsonl").decide(description)
    plain, _ = _pipeline(tmp_path).decide(description)
    assert (traced.code, traced.rule_path, traced.escalated) == (
        plain.code, plain.rule_path, plain.escalated,
    )


def test_a_latency_budget_stops_a_request(tmp_path):
    from hsclass.obs.resources import Budget, LatencyBudgetExceeded

    classifier = _pipeline(tmp_path, budget=Budget(latency_ms=-1.0))
    with pytest.raises(LatencyBudgetExceeded):
        classifier.decide("a cotton t-shirt")


def test_a_budget_breach_still_records_the_memory_gauge(tmp_path):
    """The traces of a breach are the ones the gauge is for.

    Sampling it after stage 3 meant a request that blew its budget at
    normalise or propose wrote "process_working_set_mb": null - so the
    reading was present only in the traces that completed, which are the
    ones that do not need it. A breach because the machine went into swap is
    exactly when it is worth having.
    """
    from hsclass.obs.resources import Budget, LatencyBudgetExceeded

    sink = tmp_path / "traces.jsonl"
    classifier = _pipeline(tmp_path, sink=sink, budget=Budget(latency_ms=-1.0))
    with pytest.raises(LatencyBudgetExceeded):
        classifier.decide("a cotton t-shirt")

    record = json.loads(sink.read_text(encoding="utf-8").strip())
    assert record["resources"] is not None
    assert "process_working_set_mb" in record["resources"]
    # The decision is absent because none was reached - that part is honest.
    assert record["decision"] is None


def test_the_obs_modules_import_without_windows():
    """`import ctypes.wintypes` at module scope broke the ubuntu CI.

    It raises ValueError on a non-Windows interpreter at *import* time, so
    it took obs.resources, obs.trace and pipeline.adjudicated down with it,
    and with them most of the suite - past a sys.platform guard that never
    got to run. The struct is built from plain ctypes types now.

    Checked in a subprocess, which is both what CI does and the only way to
    do it safely: an earlier version reloaded the modules in-process, which
    rebound ``LatencyBudgetExceeded`` to a new class object and broke an
    unrelated test's ``pytest.raises`` in a different file.
    """
    import subprocess
    import sys
    import textwrap

    probe = textwrap.dedent(
        """
        import builtins
        real = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name.endswith("wintypes"):
                raise ValueError("_type_ 'v' not supported")
            return real(name, *args, **kwargs)

        builtins.__import__ = blocked
        import hsclass.obs.resources  # noqa: F401
        import hsclass.obs.trace  # noqa: F401
        import hsclass.pipeline.adjudicated  # noqa: F401

        # Only that the imports survive. Blocking wintypes no longer stops
        # the reader working here, because it does not need wintypes any
        # more - which is the fix. Degradation where the reader is genuinely
        # unavailable is covered by test_resources.py.
        print("ok")
        """
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
