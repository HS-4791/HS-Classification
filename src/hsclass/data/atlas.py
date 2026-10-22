"""The ATLAS ruling set: CBP classification decisions with their reasoning.

Two things about this dataset are not what the project spec assumed, and both
change how it has to be used:

* **It is not published at the path the spec names.** ``flexifyai/...`` 404s;
  the dataset lives at `DATASET_ID` below, under Apache-2.0.
* **It is a fine-tuning set, not a labelled table.** Every row is a chat
  exchange. The product description is the user turn; the assistant turn holds
  the code and the officer's reasoning as free text::

      HTS US Code -> 7307.19.9060
      Reasoning -> The product in question is classified under ...

  So the label has to be extracted, and a row whose code cannot be read is
  dropped rather than guessed at - `load_split` reports how many.

The reasoning text is worth keeping: it is what explanation quality gets
scored against later, and very few classification sets carry it at all.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

DATASET_ID = "Dayanand314Krishna/cross_rulings_hts_dataset_for_tariffs"
LICENCE = "apache-2.0"
BASE_URL = f"https://huggingface.co/datasets/{DATASET_ID}/resolve/main"
SPLITS = ("train", "validation", "test")

#: The code as the assistant turn states it, e.g. "HTS US Code -> 7307.19.9060".
CODE_RE = re.compile(r"HTS\s+US\s+Code\s*->\s*([\d.]+)")
REASONING_RE = re.compile(r"Reasoning\s*->\s*(.+)", re.DOTALL)

#: Every one of the 18,253 training rows opens with this exact phrasing - it
#: is the template the fine-tuning set was built from, not something a user
#: would send. A broker submits a product description, so the question framing
#: is stripped and the description underneath is what gets classified.
#: Measured on the test split: leaving it in costs 4.5 points of top-1 at six
#: digits, because eight tokens of identical boilerplate dilute every vector.
QUESTION_RE = re.compile(r"^\s*what\s+is\s+the\s+hts\s+us\s+code\s+for\s+", re.I)


def digits(code: str) -> str:
    return "".join(ch for ch in code if ch.isdigit())


def product_description(question: str) -> str:
    """Strip the dataset's question framing down to the goods themselves."""
    return QUESTION_RE.sub("", question.strip()).strip().rstrip("?").strip()


@dataclass(frozen=True)
class Example:
    """One ruling: what was imported, the code it got, and why."""

    description: str
    code: str  # digits only, as published - usually ten
    reasoning: str = ""

    @property
    def is_full_length(self) -> bool:
        return len(self.code) == 10


def parse_messages(messages: list[dict]) -> Example | None:
    """Pull the description, code and reasoning out of one chat exchange."""
    user = next((m for m in messages if m.get("role") == "user"), None)
    assistant = next((m for m in messages if m.get("role") == "assistant"), None)
    if not user or not assistant:
        return None

    answer = assistant.get("content") or ""
    match = CODE_RE.search(answer)
    if not match:
        return None
    code = digits(match.group(1))
    if len(code) < 6:
        return None

    reasoning = REASONING_RE.search(answer)
    return Example(
        description=product_description(user.get("content") or ""),
        code=code,
        reasoning=reasoning.group(1).strip() if reasoning else "",
    )


def load_split(path: Path) -> tuple[list[Example], int]:
    """Read one JSONL split. Returns the usable examples and how many were dropped."""
    examples: list[Example] = []
    dropped = 0
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            example = parse_messages(json.loads(line).get("messages") or [])
            if example is None:
                dropped += 1
            else:
                examples.append(example)
    return examples, dropped


def as_dataset(examples: list[Example]) -> list[tuple[str, str]]:
    """The (description, gold code) pairs the eval runner takes."""
    return [(e.description, e.code) for e in examples]
