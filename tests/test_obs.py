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
