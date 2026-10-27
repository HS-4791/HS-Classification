"""Retrieval proposes, the rule engine disposes.

This is the architecture's load-bearing claim in one class: a probabilistic
proposer generates candidates, and a deterministic adjudicator either settles
them under the GRI or refuses. The refusal is the point. A classifier that
always answers cannot be deployed against a legal procedure, because the
cases it gets wrong are indistinguishable from the ones it gets right.

What this changes in the numbers is deliberate and should be read as a pair:
headline accuracy falls, because an escalated case counts as a miss, while
accuracy on the slice the system *did* answer is what a broker would
actually experience. 92% over 80% of cases beats 85% over everything.

**Tracing lives here rather than in a wrapper.** A second, traced classifier
would be a second code path, and the untraced one is what every ablation
measures - so the traced path would be the one nobody checked. With no sink
configured the cost is one ``if`` per classification.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from hsclass.obs.resources import Budget, Stopwatch
from hsclass.obs.trace import Trace, trace
from hsclass.pipeline.gri import Decision, GRIEngine, extract_facts

#: Anything that turns a description into ranked candidate codes.
Proposer = Callable[[str], tuple[Iterable[str], bool]]


@contextmanager
def _stage(recording: Trace | None, name: str) -> Iterator[dict]:
    """Time a stage when tracing, and get out of the way when not.

    The alternative is an ``if`` around each stage with the work duplicated
    in both arms, which is how the stages came to be timed at zero: the
    traced arm wrapped the bookkeeping instead of the work.
    """
    if recording is None:
        yield {}
        return
    with recording.stage(name) as payload:
        yield payload


class AdjudicatedClassifier:
    """A proposer whose candidates are settled - or declined - by the rules."""

    def __init__(
        self,
        proposer: Proposer,
        engine: GRIEngine,
        texts: dict[str, str] | None = None,
        shortlist: int = 5,
        schedule_version: str = "",
        sink: Path | None = None,
        budget: Budget | None = None,
    ) -> None:
        self.proposer = proposer
        self.engine = engine
        self.texts = texts or {}
        self.shortlist = shortlist
        self.schedule_version = schedule_version
        #: Where traces are appended, one JSON line each. None disables
        #: tracing entirely rather than writing to a bin.
        self.sink = sink
        self.budget = budget or Budget()

    def decide(self, description: str) -> tuple[Decision, list[str]]:
        # Only the sink decides whether to trace. The budget is checked
        # inside ``_decide`` regardless, so including it here bought nothing
        # and cost everything: a classifier with a budget and no sink - a
        # plausible production shape, traces going elsewhere - paid for a
        # uuid, a timestamp, three timed stages and a memory syscall, and
        # then dropped all of it because there was nowhere to write it.
        if self.sink is None:
            return self._decide(description)
        with trace(
            schedule_version=self.schedule_version,
            input={"description": description},
            sink=self.sink,
            cost_ceiling_usd=self.budget.usd,
        ) as recording:
            return self._decide(description, recording)

    def _decide(
        self, description: str, recording: Trace | None = None
    ) -> tuple[Decision, list[str]]:
        """The three stages that exist, each timed and recorded.

        Stage 4, explanation, is not built. It is absent from the trace
        rather than present and empty, because a stage that always reports
        nothing reads as a stage that ran and found nothing to say.
        """
        clock = Stopwatch()

        # Each stage's work happens *inside* its timed block. Recording the
        # payload afterwards, outside the block, was the first version of
        # this and it timed only the assignment: every stage reported
        # 0.0ms, including the one that takes 1.9 seconds with a reranker
        # attached. A duration of zero is not a missing measurement, it is a
        # wrong one that reads like a fast stage.

        # Stage 1 - normalise. Deterministic extraction for now; this is
        # where model-based attribute extraction will attach.
        with _stage(recording, "normalise") as payload:
            facts = extract_facts(description)
            payload["materials"] = facts.materials
            payload["is_mixture"] = facts.is_mixture
        self.budget.check_latency(clock.ms, "normalise")

        # Stage 2 - propose.
        with _stage(recording, "propose") as payload:
            # [0] only: the proposer's own escalate flag is deliberately
            # dropped. Stage 2 proposes and stage 3 adjudicates, so a
            # proposer does not get a vote on whether the rules settle a case.
            candidates = list(self.proposer(description)[0])
            payload["candidates"] = candidates
        self.budget.check_latency(clock.ms, "propose")

        # Stage 3 - adjudicate. The only stage with no model in it.
        with _stage(recording, "adjudicate") as payload:
            decision = self.engine.adjudicate(facts, candidates, self.texts)
            payload["rule_path"] = decision.rule_path
            payload["notes_cited"] = decision.notes_cited
            payload["conditions_applied"] = decision.conditions_applied
        if recording is not None:
            recording.decide(
                decision.code,
                gri_path=decision.rule_path,
                notes_cited=decision.notes_cited,
                escalated=decision.escalated,
                shortlist=decision.shortlist,
            )
        self.budget.check_latency(clock.ms, "adjudicate")
        return decision, candidates

    def __call__(self, description: str) -> tuple[Iterable[str], bool]:
        decision, candidates = self.decide(description)
        if decision.code is not None:
            return [decision.code], False
        # Declined: the ranked shortlist goes to a human, and the eval counts
        # the case as unanswered rather than quietly scoring the top guess.
        return (decision.shortlist or candidates)[: self.shortlist], True
