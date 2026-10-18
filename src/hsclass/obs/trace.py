from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from hsclass.obs.cost import CostLedger


@dataclass
class Stage:
    name: str
    duration_ms: float
    data: dict

    def to_dict(self) -> dict:
        return {"name": self.name, "duration_ms": round(self.duration_ms, 2), **self.data}


@dataclass
class Trace:
    """One reconstructable classification.

    An auditor asking "why this code?" must be able to read back the candidates
    considered, the rules that fired, the notes consulted and the schedule
    version used. That is what this records.
    """

    request_id: str
    schedule_version: str
    input: dict
    stages: list[Stage] = field(default_factory=list)
    decision: dict | None = None
    ledger: CostLedger | None = None
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    _t0: float = field(default_factory=time.perf_counter, repr=False)

    @contextmanager
    def stage(self, name: str, **data: object) -> Iterator[dict]:
        """Time a pipeline stage. Mutate the yielded dict to record what it did."""
        payload: dict = dict(data)
        t0 = time.perf_counter()
        try:
            yield payload
        finally:
            self.stages.append(Stage(name, (time.perf_counter() - t0) * 1000, payload))

    def decide(
        self,
        code: str | None,
        *,
        gri_path: list[str] | None = None,
        notes_cited: list[str] | None = None,
        escalated: bool = False,
        shortlist: list[str] | None = None,
    ) -> None:
        self.decision = {
            "code": code,
            "gri_path": gri_path or [],
            "notes_cited": notes_cited or [],
            "escalated": escalated,
            "shortlist": shortlist or [],
        }

    @property
    def latency_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "started_at": self.started_at,
            "schedule_version": self.schedule_version,
            "latency_ms": round(self.latency_ms, 2),
            "input": self.input,
            "stages": [s.to_dict() for s in self.stages],
            "decision": self.decision,
            "cost": self.ledger.summary() if self.ledger else None,
        }


@contextmanager
def trace(
    *,
    schedule_version: str,
    input: dict,
    sink: Path | None = None,
    cost_ceiling_usd: float | None = None,
) -> Iterator[Trace]:
    """Open a trace; append it as one JSON line on exit, success or failure."""
    t = Trace(
        request_id=uuid.uuid4().hex[:16],
        schedule_version=schedule_version,
        input=input,
        ledger=CostLedger(ceiling_usd=cost_ceiling_usd),
    )
    try:
        yield t
    finally:
        if sink is not None:
            sink.parent.mkdir(parents=True, exist_ok=True)
            with sink.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(t.to_dict(), ensure_ascii=False) + "\n")
