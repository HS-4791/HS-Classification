"""Stage 1: the model reads, and is not believed about numbers.

The guard these are mostly about: Note 2(A) decides which material
predominates by weight, so a percentage the model invented would settle a
case under a legal rule with a citation attached. The engine can check that
60 beats 40; it cannot check that either was ever written down. That is the
one thing stage 1 has to check for itself.
"""

from __future__ import annotations

import pytest

from hsclass.obs.cost import Usage
from hsclass.pipeline.extract import (
    INSTRUCTIONS,
    Extraction,
    ModelExtractor,
    OpenAITransport,
    verify,
)

SHIRT = "a knitted shirt of 60% cotton and 40% polyester"


class StubTransport:
    """Returns a canned body, and records what it was asked."""

    def __init__(self, content: str, usage: Usage | None = None) -> None:
        self.content = content
        self.usage = usage or Usage("gpt-4o-mini", input_tokens=900, output_tokens=40)
        self.seen: list[tuple[str, str]] = []

    def complete(self, instructions: str, description: str):
        self.seen.append((instructions, description))
        return self.content, self.usage


# ------------------------------------------------- the verification guard

def test_a_percentage_the_description_states_is_kept():
    raw = {"materials": {"Cotton": 60, "Polyester": 40.0}}
    extraction = verify(raw, SHIRT)
    assert extraction.materials == {"cotton": 60.0, "polyester": 40.0}
    assert extraction.dropped == []


def test_a_percentage_the_description_does_not_state_is_dropped():
    """The failure this guard exists for.

    "a cotton and polyester shirt" states no percentages. A model that
    helpfully returns 60/40 has invented the fact Note 2(A) turns on, and
    the engine would decide the subheading on it under GRI 1.
    """
    raw = {"materials": {"cotton": 60, "polyester": 40}}
    extraction = verify(raw, "a shirt of cotton and polyester")
    assert extraction.materials == {}
    assert len(extraction.dropped) == 2
    assert "does not appear" in extraction.dropped[0]


def test_only_the_invented_half_is_dropped():
    """A partly-grounded extraction keeps the grounded part."""
    extraction = verify(
        {"materials": {"cotton": 60, "polyester": 40, "elastane": 5}}, SHIRT
    )
    assert extraction.materials == {"cotton": 60.0, "polyester": 40.0}
    assert [d for d in extraction.dropped if "elastane" in d]


def test_a_number_inside_a_larger_number_does_not_count():
    """A substring search would find "60" in "160 g/m2" and believe it."""
    extraction = verify({"materials": {"cotton": 60}}, "a shirt weighing 160 g/m2")
    assert extraction.materials == {}


def test_a_decimal_percentage_is_matched_as_written():
    extraction = verify(
        {"materials": {"butterfat": 5.5}}, "containing 5.5 percent by weight of butterfat"
    )
    assert extraction.materials == {"butterfat": 5.5}


@pytest.mark.parametrize("value", ["sixty", None, "", 0, -5, 101, 1000])
def test_a_value_that_is_not_a_percentage_is_refused(value):
    extraction = verify({"materials": {"cotton": value}}, SHIRT)
    assert extraction.materials == {}
    assert extraction.dropped


def test_materials_without_percentages_are_kept_separately():
    """They are not facts the rules can evaluate, and not nothing either.

    "a cotton t-shirt" names a material and states no weight, so Note 2(A)
    cannot fire on it. Recording the name keeps that visible instead of
    looking like an extraction failure.
    """
    extraction = verify(
        {"materials": {}, "named_materials": ["Cotton", " wool "]}, "a cotton and wool shirt"
    )
    assert extraction.materials == {}
    assert extraction.named_materials == ["cotton", "wool"]


# ------------------------------------------------------------ the extractor

def test_the_extractor_fills_facts_from_a_response():
    transport = StubTransport('{"materials": {"cotton": 60, "polyester": 40}}')
    facts = ModelExtractor(transport)(SHIRT)
    assert facts.materials == {"cotton": 60.0, "polyester": 40.0}
    assert facts.is_mixture
    assert facts.predominant() == ("cotton", 60.0)
    # The model is shown the description and the instructions, nothing else.
    instructions, description = transport.seen[0]
    assert description == SHIRT
    assert instructions == INSTRUCTIONS


def test_the_model_is_never_asked_for_a_code():
    """The architecture's claim, as a property of the prompt.

    Stage 1 reads attributes; stage 3 decides. An instruction that mentioned
    codes would hand the decision to the model.
    """
    lowered = INSTRUCTIONS.lower()
    assert "never choose a tariff code" in lowered
    for forbidden in ("hts", "heading", "subheading", "classify it as"):
        assert forbidden not in lowered


def test_prose_instead_of_json_extracts_nothing_rather_than_guessing():
    extraction = ModelExtractor(StubTransport("I think it is mostly cotton.")).extract(SHIRT)
    assert extraction.materials == {}
    assert extraction.dropped and "not JSON" in extraction.dropped[0]
    # Empty facts escalate at stage 3, which is the right outcome.
    assert not extraction.to_facts(SHIRT).is_mixture


def test_a_json_array_is_refused():
    extraction = ModelExtractor(StubTransport('["cotton", "polyester"]')).extract(SHIRT)
    assert extraction.materials == {}
    assert "not an object" in extraction.dropped[0]


def test_the_usage_is_carried_for_the_ledger():
    usage = Usage("gpt-4o-mini", input_tokens=912, output_tokens=38)
    extraction = ModelExtractor(StubTransport('{"materials": {}}', usage)).extract(SHIRT)
    assert extraction.usage == usage
    # And it is priced, which it would not be for an unknown model.
    assert extraction.usage.cost_usd() > 0


# ------------------------------------------------------------- the transport

def test_the_transport_refuses_to_start_without_a_key():
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAITransport(api_key="")


def test_the_request_is_deterministic_and_asks_for_json():
    sent: dict = {}

    class FakeClient:
        def post(self, url, json, headers, timeout):
            sent.update({"url": url, "json": json, "headers": headers})

            class Response:
                status_code = 200

                def raise_for_status(self): ...

                def json(self):
                    return {
                        "choices": [{"message": {"content": '{"materials": {}}'}}],
                        "usage": {
                            "prompt_tokens": 1000,
                            "completion_tokens": 20,
                            "prompt_tokens_details": {"cached_tokens": 800},
                        },
                    }

            return Response()

    transport = OpenAITransport(api_key="sk-test", client=FakeClient())
    content, usage = transport.complete("instructions", SHIRT)

    assert sent["json"]["temperature"] == 0.0
    assert sent["json"]["response_format"] == {"type": "json_object"}
    assert sent["json"]["model"] == "gpt-4o-mini"
    assert content == '{"materials": {}}'
    # Cached prompt tokens are reported inside prompt_tokens and priced
    # separately, so they are subtracted out instead of charged twice.
    assert (usage.input_tokens, usage.cache_read_tokens) == (200, 800)
    assert usage.output_tokens == 20


def test_the_key_is_not_in_the_payload_or_the_repr():
    transport = OpenAITransport(api_key="sk-secret-value")
    assert "sk-secret-value" not in repr(transport)
    assert "sk-secret-value" not in str(vars(transport).get("model", ""))


def test_an_extraction_round_trips_to_a_trace():
    extraction = Extraction(
        materials={"cotton": 60.0}, named_materials=["wool"], form="knitted"
    )
    recorded = extraction.to_dict()
    assert recorded["materials"] == {"cotton": 60.0}
    assert recorded["form"] == "knitted"
    assert recorded["dropped"] == []
