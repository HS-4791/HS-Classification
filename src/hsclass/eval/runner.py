from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from hsclass.eval.metrics import EvalResult, Prediction, evaluate, latency_summary

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
    # Per-classification, not a total divided by n. The spec asks for p50 and
    # p95, and a mean hides the tail that a latency budget is set against -
    # this pipeline's own cross-encoder configuration has a p95 far above its
    # mean because the first classifications pay for an ONNX arena.
    latencies_ms: list[float] = []
    for i, (text, gold) in enumerate(dataset):
        if limit is not None and i >= limit:
            break
        started = time.perf_counter()
        ranked, escalated = classifier(text)
        latencies_ms.append((time.perf_counter() - started) * 1000)
        preds.append(Prediction(gold=gold, ranked=tuple(ranked), escalated=escalated))

    result = evaluate(preds, name=name, revision=revision)
    result.latency = latency_summary(latencies_ms)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / f"{name}.json").write_text(
        json.dumps(result.to_dict(), indent=2), encoding="utf-8"
    )
    (reports_dir / f"{name}.md").write_text(result.to_markdown(), encoding="utf-8")
    return result
