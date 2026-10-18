from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path

from hsclass.eval.metrics import EvalResult, Prediction, evaluate

#: A classifier takes a product description and returns ranked candidate codes
#: plus whether it declined to pick one.
Classifier = Callable[[str], tuple[Iterable[str], bool]]


def run_eval(
    name: str,
    classifier: Classifier,
    dataset: Iterable[tuple[str, str]],
    *,
    revision: str,
    reports_dir: Path,
    limit: int | None = None,
) -> EvalResult:
    """Run a classifier over (description, gold_code) pairs and write the report.

    Both artefacts are stamped with the schedule version, because an accuracy
    number measured against one revision is not comparable to another.
    """
    preds: list[Prediction] = []
    for i, (text, gold) in enumerate(dataset):
        if limit is not None and i >= limit:
            break
        ranked, escalated = classifier(text)
        preds.append(Prediction(gold=gold, ranked=tuple(ranked), escalated=escalated))

    result = evaluate(preds, name=name, revision=revision)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / f"{name}.json").write_text(
        json.dumps(result.to_dict(), indent=2), encoding="utf-8"
    )
    (reports_dir / f"{name}.md").write_text(result.to_markdown(), encoding="utf-8")
    return result
