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
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from hsclass.pipeline.gri import Decision, GRIEngine, extract_facts

#: Anything that turns a description into ranked candidate codes.
Proposer = Callable[[str], tuple[Iterable[str], bool]]


class AdjudicatedClassifier:
    """A proposer whose candidates are settled - or declined - by the rules."""

    def __init__(
        self,
        proposer: Proposer,
        engine: GRIEngine,
        texts: dict[str, str] | None = None,
        shortlist: int = 5,
    ) -> None:
        self.proposer = proposer
        self.engine = engine
        self.texts = texts or {}
        self.shortlist = shortlist

    def decide(self, description: str) -> tuple[Decision, list[str]]:
        candidates = [c for c in self.proposer(description)[0]]
        facts = extract_facts(description)
        return self.engine.adjudicate(facts, candidates, self.texts), candidates

    def __call__(self, description: str) -> tuple[Iterable[str], bool]:
        decision, candidates = self.decide(description)
        if decision.code is not None:
            return [decision.code], False
        # Declined: the ranked shortlist goes to a human, and the eval counts
        # the case as unanswered rather than quietly scoring the top guess.
        return (decision.shortlist or candidates)[: self.shortlist], True
