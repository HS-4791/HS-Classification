"""Score the reasoning, not just the code.

A classifier can produce the right code by the wrong route: retrieval ranks
one candidate, the engine resolves at GRI 1, the answer matches the gold, and
the ruling says the case was actually decided under GRI 3(b) on essential
character. Accuracy cannot tell those apart, and a rule engine that is right
by accident is the failure the whole architecture exists to prevent.

The rulings make this checkable. Each carries the officer's reasoning, and
that text names the rules applied: 970 of 18,253 name a GRI, and **703 name
something beyond GRI 1** - the slice where the case needed more than the
terms of the heading.

Agreement on GRI 1 alone is not evidence. It is recited as boilerplate -
"According to GRI 1, classification is determined based on the terms of the
headings and any relevant section and chapter notes" - in 404 rulings, so a
system that always answered "GRI 1" would score well on it while knowing
nothing. The discriminating slice is the only one reported as a headline.

**This is a derived oracle, and the distinction matters.** The ATLAS reasoning
reads as rewritten for fine-tuning rather than copied from the ruling letter,
so a disagreement is a signal worth investigating, not proof the engine is
wrong. The authoritative text is the CROSS letter itself.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from hsclass.eval.metrics import at_digits
from hsclass.pipeline.gri import Decision

#: "GRI 1", "GRI 3(b)", "General Rule of Interpretation (GRI) 2(a)".
GRI_RE = re.compile(
    r"\b(?:GRIs?|General Rules? of Interpretation)\s*\)?\s*\(?(\d)\)?\s*(\([a-c]\))?",
    re.I,
)

#: A note cited with a number - "Additional U.S. Note 4", "Chapter 61 Note 5",
#: "Note 2 to Section XI". A bare "section and chapter notes" is the GRI 1
#: recital and names nothing, so it is deliberately not matched.
NOTE_RE = re.compile(
    r"\b(?:(chapter|section|subheading)\s+(\w+)\s+)?"
    r"(?:(additional\s+u\.?\s?s\.?)\s+)?note\s+(\d+(?:\([a-zA-Z]+\))*)",
    re.I,
)

#: Rulings write a full code as "7307.91.3000" - four digits, then two, then
#: a four-digit statistical suffix. Allowing only two-digit groups truncates
#: it to "7307.91.30" and the comparison then runs against a code that does
#: not exist.
HEADING_RE = re.compile(r"\b(?:sub)?heading\s+(\d{4}(?:\.\d{2,4})*)", re.I)

#: The order the rules are applied in. The deciding rule is the furthest the
#: ruling had to go; reaching Rule 3 where Rule 1 answered is itself an error,
#: so "how far did it go" is the comparable quantity.
#: The bare forms are in here deliberately: 103 rulings say "GRI 3" without a
#: letter, and leaving them out made `deciding_rule` fall through to GRI 1 or
#: to nothing at all - so a case the ruling plainly took to Rule 3 was scored
#: as though it had been settled on the terms of the heading.
RULE_ORDER = (
    "1",
    "2", "2(a)", "2(b)",
    "3", "3(a)", "3(b)", "3(c)",
    "4",
    "5", "5(a)", "5(b)",
)

#: GRI 6 governs the choice of subheading once the heading is settled. It is
#: not a step in the 1-to-5 sequence, so it is tracked separately rather than
#: competing with them for "furthest reached".
SUBHEADING_RULE = "6"


@dataclass(frozen=True)
class Authorities:
    """What a piece of reasoning explicitly cites."""

    gris: frozenset[str] = frozenset()
    notes: frozenset[str] = frozenset()
    headings: frozenset[str] = frozenset()

    @property
    def beyond_gri_1(self) -> frozenset[str]:
        """The rules that make a case discriminating to score against."""
        return frozenset(g for g in self.gris if g not in {"1", SUBHEADING_RULE})

    @property
    def deciding_rule(self) -> str | None:
        """How far along the sequence the reasoning had to go."""
        ranked = [g for g in self.gris if g in RULE_ORDER]
        return max(ranked, key=RULE_ORDER.index) if ranked else None


def _normalise_gri(number: str, letter: str | None) -> str:
    return f"{number}{(letter or '').lower()}"


def authorities_in(reasoning: str) -> Authorities:
    """Read the rules, notes and headings a piece of reasoning names."""
    gris = {_normalise_gri(m.group(1), m.group(2)) for m in GRI_RE.finditer(reasoning)}

    notes: set[str] = set()
    for match in NOTE_RE.finditer(reasoning):
        scope, where, us, number = match.groups()
        label = "Additional U.S. Note" if us else "Note"
        if scope and where:
            notes.add(f"{scope.title()} {where.upper()} {label} {number}")
        else:
            notes.add(f"{label} {number}")

    headings = {m.group(1).replace(".", "") for m in HEADING_RE.finditer(reasoning)}
    return Authorities(frozenset(gris), frozenset(notes), frozenset(headings))


@dataclass
class ExplanationScore:
    """How often the engine reached the rule the ruling says decided the case."""

    n: int = 0
    #: Rulings naming a GRI beyond 1 - the only slice worth a headline.
    discriminating: int = 0
    #: Of those, the ones the engine actually answered rather than escalating.
    decided: int = 0
    agreed: int = 0
    #: Right code, wrong route: the class accuracy cannot see.
    right_code_wrong_path: int = 0
    #: Rules the rulings reach that the engine never does.
    unreached: dict[str, int] = field(default_factory=dict)

    @property
    def agreement(self) -> float:
        return self.agreed / self.decided if self.decided else 0.0

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "discriminating": self.discriminating,
            "decided_of_discriminating": self.decided,
            "agreed_on_deciding_rule": self.agreed,
            "agreement": round(self.agreement, 4),
            "right_code_wrong_path": self.right_code_wrong_path,
            "rules_the_engine_never_reaches": dict(
                sorted(self.unreached.items(), key=lambda kv: -kv[1])
            ),
        }

    def to_markdown(self) -> str:
        lines = [
            "# Explanation quality",
            "",
            f"{self.n} cases scored. {self.discriminating} rulings name a GRI beyond 1 -"
            " the slice where agreement means something, since GRI 1 alone is recited"
            " as boilerplate.",
            "",
            "| | |",
            "| --- | ---: |",
            f"| discriminating cases | {self.discriminating} |",
            f"| of those, the engine decided | {self.decided} |",
            f"| agreed on the deciding rule | {self.agreed} |",
            f"| right code by a route the ruling did not use | {self.right_code_wrong_path} |",
        ]
        if self.unreached:
            lines += ["", "## Rules the rulings reach and the engine does not", "",
                      "| rule | rulings |", "| --- | ---: |"]
            for rule, count in sorted(self.unreached.items(), key=lambda kv: -kv[1]):
                lines.append(f"| GRI {rule} | {count} |")
        return "\n".join(lines) + "\n"


def _rules_in_path(decision: Decision) -> set[str]:
    """The GRI numbers the engine recorded, in the oracle's vocabulary."""
    found: set[str] = set()
    for step in decision.rule_path:
        for match in GRI_RE.finditer(step):
            found.add(_normalise_gri(match.group(1), match.group(2)))
    return found


def score_explanations(
    cases: Iterable[tuple[Decision, str, str]], digits: int = 6
) -> ExplanationScore:
    """Compare each decision's rule path against the ruling's own reasoning.

    Takes (decision, gold code, reasoning) and reports how often the engine
    reached the rule the ruling says decided the case - and how often it got
    the code right by a route the ruling never used.
    """
    score = ExplanationScore()
    for decision, gold, reasoning in cases:
        score.n += 1
        cited = authorities_in(reasoning)
        if not cited.beyond_gri_1:
            continue
        score.discriminating += 1

        deciding = cited.deciding_rule
        ours = _rules_in_path(decision)
        if decision.escalated or decision.code is None:
            # The engine declined, so it reached no rule to agree with. The
            # rules it never reaches are what this column is for.
            if deciding:
                score.unreached[deciding] = score.unreached.get(deciding, 0) + 1
            continue

        score.decided += 1
        if deciding and deciding in ours:
            score.agreed += 1
        elif deciding:
            score.unreached[deciding] = score.unreached.get(deciding, 0) + 1
            if at_digits(decision.code, digits) == at_digits(gold, digits):
                score.right_code_wrong_path += 1
    return score
