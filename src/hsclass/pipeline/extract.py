"""Stage 1: a model reading a product description into typed attributes.

Every other measurement in this project points here. The rule engine answers
2.5% of cases and 23 of its 24 held-out answers come from one note, Section
XI Note 2(A), which needs a composition by weight. The regex in
``pipeline/gri.py`` finds one in 24 of 200 descriptions. The KAG threshold
layer decides nothing because only 4 of 21 evaluable thresholds ask about a
material the description quantifies. The error analysis fails worst in
chapters decided by function rather than material. All four say the same
thing: ``Facts`` is nearly empty, and this is what fills it.

**The model does not decide.** It reads a description and returns
attributes; stage 3 applies the rules to them and is unchanged. That is the
architecture's claim and it survives a model here, because the model never
sees a tariff code.

**A number the description does not contain is a hallucination, and it
would be worse than a missing one.** Note 2(A) turns on which material
predominates by weight, so a fabricated "60" decides a case under a legal
rule with a citation attached - the confident-wrong failure this project
keeps finding. So every percentage the model returns is checked against the
source text, and one that does not appear there is dropped and recorded in
``dropped`` rather than passed to the engine. This is cheap, and it is the
only part of stage 1 that the deterministic stage cannot check for itself:
the engine can tell that 60 beats 40, not that either was ever written.

The transport is ``httpx``, which is already a core dependency, rather than
a provider SDK. One POST to one endpoint, with the same argument the project
makes for ONNX over PyTorch and exact numpy over faiss.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from hsclass.obs.cost import Usage
from hsclass.pipeline.gri import Facts

#: Deterministic decoding. The project's determinism test requires the same
#: input to give the same code 100 times, and an extraction that wanders
#: makes that untestable.
TEMPERATURE = 0.0

#: OpenAI's chat completions endpoint. Anthropic's messages endpoint has a
#: different shape; a second transport would be a second class, not a flag.
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

#: What the model is asked for. Deliberately narrow: the attributes the
#: rules can actually evaluate today, plus the form and use that the
#: function-decided chapters would need once rules exist for them. Asking
#: for a tariff code would hand stage 1 the decision.
INSTRUCTIONS = """You extract typed attributes from a product description for customs \
classification. You never choose a tariff code.

Return a JSON object with these keys:
  "materials": an object mapping each material named in the description to its \
percentage by weight, as a number, ONLY where the description states a percentage. \
Omit a material if no percentage is given for it.
  "named_materials": an array of materials the description names without a percentage.
  "form": the physical form if stated - "woven", "knitted", "sheet", "powder", \
"assembled", "unassembled" - otherwise null.
  "use": what the article is for, if stated, otherwise null.

Rules you must follow:
- Copy percentages exactly as written. Never infer, round, normalise or complete them. \
If a description says only "mostly cotton", that is a named material with no percentage.
- Do not add materials, forms or uses that the description does not mention.
- Return only the JSON object."""

#: "60% cotton" -> the model may return 60, 60.0 or "60". Any of those has
#: to be found in the description before it is believed.
NUMBER_IN_TEXT = "{:g}"


class Transport(Protocol):
    """Sends one chat completion request and returns the parsed response.

    A protocol so the tests can run the whole extractor without a network
    call or a key, and so a second provider is a second implementation
    rather than a branch inside this one.
    """

    def complete(self, instructions: str, description: str) -> tuple[str, Usage]:
        ...


@dataclass
class Extraction:
    """What stage 1 read, and what it was not allowed to keep."""

    materials: dict[str, float] = field(default_factory=dict)
    named_materials: list[str] = field(default_factory=list)
    form: str | None = None
    use: str | None = None
    usage: Usage | None = None
    #: Percentages the model returned that do not appear in the description.
    #: Recorded rather than silently discarded: a model that invents numbers
    #: is a fact about the extraction worth seeing in a trace.
    dropped: list[str] = field(default_factory=list)

    def to_facts(self, description: str) -> Facts:
        """The shape stage 3 consumes. Only verified percentages reach it."""
        return Facts(description=description, materials=dict(self.materials))

    def to_dict(self) -> dict:
        return {
            "materials": self.materials,
            "named_materials": self.named_materials,
            "form": self.form,
            "use": self.use,
            "dropped": self.dropped,
        }


def _states_number(description: str, value: float) -> bool:
    """Does the description actually contain this number?

    Written as the schedule and shippers write it: 60, 60.0, 60.5. A bare
    substring search would accept the "60" inside "160", so the match is
    bounded by non-digits.
    """
    rendered = NUMBER_IN_TEXT.format(value)
    pattern = re.escape(rendered).replace(r"\.", r"\.")
    return re.search(rf"(?<!\d){pattern}(?!\d)", description) is not None


def verify(raw: dict, description: str) -> Extraction:
    """Keep what the description supports; record what it does not.

    Separated from the transport so it can be tested against a fabricated
    response without a model, which is the only way to test the guard that
    matters.
    """
    extraction = Extraction()
    for name, value in (raw.get("materials") or {}).items():
        try:
            share = float(value)
        except (TypeError, ValueError):
            extraction.dropped.append(f"{name}={value!r} is not a number")
            continue
        if not 0 < share <= 100:
            extraction.dropped.append(f"{name}={share:g} is not a percentage")
            continue
        if not _states_number(description, share):
            extraction.dropped.append(f"{name}={share:g} does not appear in the description")
            continue
        extraction.materials[str(name).lower().strip()] = share

    named = raw.get("named_materials") or []
    extraction.named_materials = [
        str(m).lower().strip() for m in named if isinstance(m, str) and m.strip()
    ]
    for key in ("form", "use"):
        value = raw.get(key)
        setattr(extraction, key, str(value).lower().strip() if isinstance(value, str) else None)
    return extraction


class ModelExtractor:
    """Fills ``Facts`` from a description, and charges the ledger for it."""

    def __init__(self, transport: Transport, instructions: str = INSTRUCTIONS) -> None:
        self.transport = transport
        self.instructions = instructions

    def extract(self, description: str) -> Extraction:
        content, usage = self.transport.complete(self.instructions, description)
        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            # A model that returns prose instead of JSON has not extracted
            # anything. Empty facts escalate; a guess would not.
            return Extraction(usage=usage, dropped=[f"response was not JSON: {content[:80]!r}"])
        if not isinstance(raw, dict):
            return Extraction(usage=usage, dropped=[f"response was not an object: {type(raw)}"])
        extraction = verify(raw, description)
        extraction.usage = usage
        return extraction

    def __call__(self, description: str) -> Facts:
        return self.extract(description).to_facts(description)


class OpenAITransport:
    """One POST to the chat completions endpoint, with deterministic settings."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        url: str = OPENAI_URL,
        timeout: float = 30.0,
        client: object | None = None,
    ) -> None:
        if not api_key:
            raise ValueError(
                "no API key; set OPENAI_API_KEY in .env (which is gitignored)"
            )
        self._key = api_key
        self.model = model
        self.url = url
        self.timeout = timeout
        self._client = client

    def complete(self, instructions: str, description: str) -> tuple[str, Usage]:
        import httpx

        client = self._client
        payload = {
            "model": self.model,
            "temperature": TEMPERATURE,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": description},
            ],
        }
        headers = {"Authorization": f"Bearer {self._key}"}
        if client is None:
            response = httpx.post(
                self.url, json=payload, headers=headers, timeout=self.timeout
            )
        else:
            response = client.post(
                self.url, json=payload, headers=headers, timeout=self.timeout
            )
        response.raise_for_status()
        body = response.json()
        tokens = body.get("usage") or {}
        cached = (tokens.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        return (
            body["choices"][0]["message"]["content"],
            Usage(
                model=self.model,
                # Cached prompt tokens are reported inside prompt_tokens and
                # priced differently, so they are subtracted out rather than
                # charged twice.
                input_tokens=max(0, tokens.get("prompt_tokens", 0) - cached),
                output_tokens=tokens.get("completion_tokens", 0),
                cache_read_tokens=cached,
            ),
        )
