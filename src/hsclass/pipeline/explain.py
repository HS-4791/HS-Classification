"""Stage 4: write the reasoning from a decision already made.

The architecture's last claim. Stages 1 and 2 are allowed to be wrong because
stage 3 checks them; stage 3 is deterministic. Stage 4 is where that could all
be given away, because a fluent model handed a code and asked why will produce
a reason - and it will produce one whether or not the rules ever reached it.
A fabricated "under GRI 3(b), essential character" attached to a code the
engine resolved at GRI 1 is worse than no explanation: it is a legal citation
for reasoning that never happened, and it reads exactly like the real thing.

So the model does not get to say what decided the case. It gets the decision,
the authorities the decision already holds, and the job of writing them as
prose - and **every citation in what it writes is checked back against the
decision before the text is returned**. An explanation naming an authority the
decision does not hold is refused, recorded, and replaced by the deterministic
rendering. Same discipline as ``verify`` in stage 1, where a percentage the
description does not contain is dropped rather than passed to the engine: the
guard is a gate, not a score.

Two consequences worth stating plainly.

**The deterministic renderer is the product; the model is an enhancement.**
``render`` alone is a complete, faithful, citable explanation of any decision,
and it needs no key, no network and no budget. If the model is unavailable the
system still explains itself. The model buys readability, nothing else.

**One citation parser, not two.** The gate reads the generated text with
``authorities_in`` - the same function that reads the CBP officer's reasoning
for the judgement oracle. Two grammars would be free to disagree, and the gate
would then wave through a citation the oracle scores as something else. It also
means both sides of the comparison normalise identically: "Section XI
Subheading Note 2" reduces to "Note 2" through that regex, and because the
decision's own citations go through it too, the mismatch cancels instead of
becoming a false rejection of the renderer's own output.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from hsclass.eval.explanation import Authorities, authorities_in
from hsclass.obs.cost import Usage
from hsclass.pipeline.gri import Decision

#: What the model is asked for. It is given the decision and told to describe
#: it; it is never asked which rule applies, and never shown the gold answer.
#: The prohibition is spelled out because the gate's rejections are expensive
#: to nobody but the reader - a refused explanation costs a request and buys
#: the deterministic text that was available for free.
INSTRUCTIONS = """You write the official reasoning for a customs classification that has \
already been decided. You never decide anything, and you never question the decision.

You will be given the goods, the code decided, the legal rules that were applied, the \
notes consulted, and the finding that resolved the case.

Write two or three sentences of plain prose explaining the classification to an importer.

Rules you must follow:
- Cite ONLY the rules, notes and headings given to you. Naming any other authority is a \
serious error, even if it seems relevant.
- Never introduce a General Rule of Interpretation that is not listed. In particular, do \
not mention essential character, GRI 3 or GRI 4 unless they appear in the rules applied.
- Do not restate the code's full description; refer to the goods as the importer would.
- Return JSON: {"explanation": "your prose"}"""

#: A second prompt, measured and **not adopted**, kept so the finding is
#: reproducible rather than only described.
#:
#: Under ``INSTRUCTIONS`` the gate refuses 38% of this model's prose, almost
#: all of it for explaining an escalated case as though a code had been
#: chosen. The obvious suspicion is that the prompt causes it, by opening
#: with "a classification that has already been decided" - so this version
#: says what to do when nothing was decided.
#:
#: It works, and it is worse. Refusals fall to **0 of 200**; the share of the
#: engine's own applied rules that the prose names falls from **88.5% to
#: 1.5%**. Told not to say which heading applies, the model stops giving
#: legal reasoning at all and writes a bare refusal. An explanation that
#: cites nothing cannot be audited, so faithfulness bought that way is not
#: worth having.
#:
#: The pair is the useful result: at this model size the two properties trade
#: against each other, and the deterministic renderer has both - it names
#: every rule in ``rule_path`` because it is built from ``rule_path``.
REFUSAL_FIRST_INSTRUCTIONS = INSTRUCTIONS.replace(
    "Rules you must follow:",
    'If the code decided is "none", the rules did NOT settle this case. Say so '
    "plainly: state that the goods could not be classified automatically and that "
    "the case is referred to a classifier. Do not say which heading applies, do "
    'not say the classification "will be determined by" anything, and do not '
    "suggest how it would come out.\n\nRules you must follow:",
)


def _digits(code: str) -> str:
    return "".join(ch for ch in code if ch.isdigit())


def authorities_of(
    decision: Decision, texts: dict[str, str] | None = None
) -> Authorities:
    """What this decision *relied on*, which is what prose may rely on.

    Read with the same parser the gate uses on the generated text, so the two
    sides cannot disagree about what a citation is.

    The schedule text of the candidate codes is deliberately **not** in here,
    though an earlier version included it so that the renderer could quote a
    chain verbatim. The nomenclature cross-references itself, and that turned
    every cross-reference into a licence: 6,703 of 29,861 provisions name a
    heading other than their own somewhere in their chain, so a decision on
    0207.11.0020 - chicken cuts, whose text reads "of the poultry of heading
    0105" - would let an explanation say the goods are classified in heading
    0105, which is live poultry. Quoting a heading is not relying on it.
    Verbatim quotation is handled by ``unsupported``'s ``quoted`` argument,
    which excuses what was copied rather than entitling what was not.

    ``texts`` is kept in the signature because callers pass it and a future
    rule may need it; it is unused today and says so rather than pretending.
    """
    codes = [c for c in [decision.code, *decision.shortlist] if c]
    operative = [
        *decision.rule_path,
        *decision.notes_cited,
        *decision.conditions_applied,
        decision.reason,
    ]
    held = authorities_in(" . ".join(operative))
    return Authorities(
        gris=held.gris,
        notes=held.notes,
        headings=held.headings | {_digits(code) for code in codes},
    )


#: "Section XI Note 2(A)" -> "Note 2(A)", keeping "Additional U.S.", which
#: names a different instrument rather than a scope.
SCOPE_PREFIX_RE = re.compile(r"^(?:Chapter|Section)\s+\S+\s+", re.I)


def _bare(note: str) -> str:
    """A note label stripped of its scope, but not of what it is."""
    return SCOPE_PREFIX_RE.sub("", note)


def _note_supported(cited: str, held: Authorities) -> bool:
    """Shorthand for a note the decision holds is not a fabrication.

    One-directional on purpose. "Note 2(A) to Section XI" and "under note
    2(A)" are how an importer writes the authority the engine recorded as
    "Section XI Note 2(A)", and refusing them counted correct paraphrase as
    invention - inflating the one rate the architecture's claim rests on.
    The reverse is still refused: holding a scope-free "Note 4" does not
    entitle prose to "Chapter 61 Note 4", because naming the wrong chapter
    is naming the wrong authority.
    """
    if cited in held.notes:
        return True
    return "Note" in cited and cited == _bare(cited) and any(
        _bare(note) == cited for note in held.notes
    )


@dataclass(frozen=True)
class ImpliedTest:
    """A legal test the prose can apply without ever citing it.

    The citation gate has one blind spot, and the first run found it: a model
    that writes "classified on its essential character" has applied GRI 3(b)
    to a case the engine resolved at GRI 1, and named no authority for the
    gate to catch. The reasoning is fabricated even though the citations are
    clean - and an importer reads the reasoning.

    So a phrase that *is* a rule counts as citing it.
    """

    pattern: re.Pattern[str]
    label: str
    gris: frozenset[str] = frozenset()
    note_contains: str = ""

    def satisfied_by(self, held: Authorities) -> bool:
        if self.gris & held.gris:
            return True
        return bool(self.note_contains) and any(
            self.note_contains in note for note in held.notes
        )


#: Only tests whose wording is unambiguous.
#:
#: "Last in numerical order" was omitted entirely at first, on the grounds
#: that Section XI Note 2(A) borrows GRI 3(c)'s principle without reaching
#: GRI 3(c), so mapping the phrase to 3(c) would make the renderer fail its
#: own gate on every tie-broken case. That was the wrong fix for a real
#: problem: ``note_contains`` already expresses "satisfied by the note that
#: borrows it", and omitting the phrase let prose assert GRI 3(c)'s operative
#: test - goods that "equally merit consideration" - on a case resolved at
#: GRI 1. Ten of the engine's twenty-one decided cases come from that
#: tie-break, so the phrase is live in the renderer's own output and needs
#: the conditional form rather than the hole.
#:
#: The bare "3" and "2" are accepted alongside "3(b)" and "2(a)" because 103
#: rulings in the oracle corpus write "GRI 3" with no letter, so prose naming
#: the rule without its subparagraph is citing it, not evading it.
IMPLIED_TESTS: tuple[ImpliedTest, ...] = (
    ImpliedTest(
        re.compile(
            r"essential character|fundamental character|essential nature"
            r"|character of the (?:goods|article)",
            re.I,
        ),
        "essential character, which is GRI 3(b)",
        frozenset({"3", "3(b)"}),
    ),
    ImpliedTest(
        re.compile(r"\b(?:incomplete|unfinished|unassembled)\b", re.I),
        "incomplete or unassembled goods, which is GRI 2(a)",
        frozenset({"2", "2(a)"}),
    ),
    # "The most appropriate heading" is GRI 3(a) in everything but name, and
    # that is the form the model actually produced: the first run over real
    # cases caught no implied test at all, because the model does not reach
    # for the textbook phrase. It reaches for a paraphrase.
    ImpliedTest(
        re.compile(r"\b(?:most|more) (?:specific|appropriate|apt|suitable)\b", re.I),
        "specificity, which is GRI 3(a)",
        frozenset({"3", "3(a)"}),
    ),
    # Both orders, and the bare "predominant fibre" form: requiring "weight"
    # within 40 characters let "The predominant fibre decides the subheading"
    # through, which asserts the weight rule in the words a shipper uses.
    ImpliedTest(
        re.compile(
            r"predomin\w*[^.]{0,40}weight|weight[^.]{0,40}predomin\w*"
            r"|predomin\w*\s+(?:fib(?:re|er)|material|textile|component)",
            re.I,
        ),
        "predominance by weight, which is Section XI Note 2(A)",
        note_contains="Note 2(A)",
    ),
    ImpliedTest(
        re.compile(
            r"last in numerical order|equally merit\w*\s+consideration", re.I
        ),
        "the last-in-numerical-order tie-break, which is GRI 3(c)",
        frozenset({"3", "3(c)"}),
        note_contains="Note 2(A)",
    ),
    # A rule named by its ordinal rather than its number. "Applying the third
    # General Rule" cites GRI 3 in words that carry no digit, so the citation
    # grammar - which needs one - sees nothing at all.
    ImpliedTest(
        re.compile(r"\bsecond\s+(?:general\s+)?(?:interpretative\s+)?rule\b", re.I),
        "the second General Rule, which is GRI 2",
        frozenset({"2", "2(a)", "2(b)"}),
    ),
    ImpliedTest(
        re.compile(r"\bthird\s+(?:general\s+)?(?:interpretative\s+)?rule\b", re.I),
        "the third General Rule, which is GRI 3",
        frozenset({"3", "3(a)", "3(b)", "3(c)"}),
    ),
    ImpliedTest(
        re.compile(r"\bfourth\s+(?:general\s+)?(?:interpretative\s+)?rule\b", re.I),
        "the fourth General Rule, which is GRI 4",
        frozenset({"4"}),
    ),
    # GRI 4 - goods most akin - is a rule this engine deliberately does not
    # implement, so prose reaching for it is always reaching outside the
    # decision. It is also the easiest rule to state without naming: "placed
    # with the goods they most closely resemble" is the whole of it.
    ImpliedTest(
        re.compile(
            r"most closely resemble|most akin|by analogy"
            r"|goods to which (?:they|it) (?:are|is) most similar",
            re.I,
        ),
        "the goods-most-akin rule, which is GRI 4",
        frozenset({"4"}),
    ),
)


def _heading_supported(cited: str, held: Authorities) -> bool:
    """A heading may be cited at any level above the code it belongs to.

    "Heading 6110" is the correct name for the heading of 6110.30.3020, so a
    cited code is supported when a held code sits beneath it. The test is
    one-directional on purpose: 6110.30.3025 is not supported by
    6110.30.3020, and inventing a neighbouring statistical suffix is exactly
    the error worth catching.
    """
    return cited in held.headings or any(
        code.startswith(cited) for code in held.headings
    )


def unsupported(
    text: str,
    decision: Decision,
    texts: dict[str, str] | None = None,
    quoted: str = "",
) -> list[str]:
    """Authorities the text cites that the decision does not hold.

    Empty means the explanation is faithful. Anything else is a fabrication,
    and the caller must not ship the text.

    ``quoted`` is schedule text the writer copied verbatim - the inherited
    chain the renderer prints after the code. Authorities inside it are
    excused, because the schedule saying "other than those of heading 6109",
    or chapter 30 describing goods "in which starch or other foodstuff
    predominates by weight", is the nomenclature's own wording and not a
    claim by the explanation. Model prose passes nothing here, so nothing it
    writes is excused.
    """
    held = authorities_of(decision, texts)
    cited = authorities_in(text)
    excused = authorities_in(quoted) if quoted else Authorities()

    found = [f"GRI {g}" for g in sorted(cited.gris - held.gris - excused.gris)]
    found += sorted(
        note
        for note in cited.notes - excused.notes
        if not _note_supported(note, held)
    )
    found += [
        f"heading {h}"
        for h in sorted(cited.headings - excused.headings)
        if not _heading_supported(h, held)
    ]
    # A rule applied in words but not named is still a rule applied - unless
    # the words are the schedule's own, quoted.
    found += [
        test.label
        for test in IMPLIED_TESTS
        if test.pattern.search(text)
        and not test.satisfied_by(held)
        and not (quoted and test.pattern.search(quoted))
    ]
    if (decision.code is None or decision.escalated) and not REFUSAL_RE.search(text):
        found.append("a refusal written as though the goods had been classified")
    return found


#: Wording that admits the case was not settled. An explanation of a refusal
#: that does not refuse is unfaithful however clean its citations are: asked
#: about an escalated apron, the model wrote that "the classification will be
#: determined by the most appropriate heading", which presents an unresolved
#: case as all but resolved. The refusal is this system's product, and prose
#: that softens it gives away the thing being sold.
#: Written loosely on purpose. The first version demanded "referred to" with
#: nothing between the words and "further determination" rather than "further
#: determine", so prose that plainly handed the case over - "it was necessary
#: to refer the case to a classifier for a final decision" - was counted as
#: though it had answered. A quarter of the refusals in the first sample were
#: that error, which inflated the one rate this stage is judged on.
#:
#: Naming the person who will decide is the surest sign of a genuine refusal,
#: so ``classifier`` and its colleagues stand alone. Over-accepting here is
#: cheap: the citation and implied-test clauses still run on the same text, so
#: prose that both refuses and fabricates is still caught by them.
REFUSAL_RE = re.compile(
    r"not (?:been |be )?(?:automatically )?classif"
    r"|no(?:t)? (?:be )?(?:automatically )?(?:classif|determin|resolv)"
    r"|could not|cannot|unable|undetermined|unresolved|inconclusive"
    r"|\bclassifier\b|human review|customs (?:officer|specialist|broker|authority)"
    r"|escalat|refer\w*[^.]{0,40}\b(?:classifier|review|determination|decision|expert)"
    r"|further (?:review|determin\w+|evaluat\w+|consideration|decision|assessment)"
    r"|left (?:open|undecided)|did not (?:resolve|settle)",
    re.I,
)


def quoted_text(decision: Decision, texts: dict[str, str] | None = None) -> str:
    """The schedule wording ``render`` copies verbatim, for the gate to excuse."""
    if decision.code is None or decision.escalated:
        return ""
    return (texts or {}).get(decision.code, "")


def _sentence(parts: list[str]) -> str:
    return " ".join(p for p in parts if p)


def render(decision: Decision, texts: dict[str, str] | None = None) -> str:
    """The decision as prose, built only from what the decision records.

    Faithful by construction: every authority it names is read straight off
    the decision. This is what ships when there is no model, and what ships
    when the model's version is refused.
    """
    texts = texts or {}
    rules = ", ".join(decision.rule_path) or "no rule"
    notes = "; ".join(decision.notes_cited)

    if decision.code is None or decision.escalated:
        body = _sentence([
            "These goods were not classified automatically.",
            f"The rules were applied as far as {rules},",
            f"and the case stopped there because {decision.reason}."
            if decision.reason
            else "and no rule resolved the case.",
        ])
        if decision.shortlist:
            body += (
                " The competing provisions are "
                + ", ".join(decision.shortlist)
                + ", and the choice between them is referred to a classifier."
            )
    else:
        described = texts.get(decision.code, "")
        body = _sentence([
            f"The goods are classified in subheading {decision.code}"
            + (f", {described}." if described else "."),
            f"The rules applied, in order: {rules}.",
            f"That resolved the case because {decision.reason}."
            if decision.reason
            else "",
        ])

    if notes:
        body += f" Notes consulted: {notes}."
    if decision.conditions_applied:
        body += (
            " Provisions excluded on their own terms: "
            + "; ".join(f'"{c}"' for c in decision.conditions_applied)
            + "."
        )
    return body


class Transport(Protocol):
    """Sends one completion request. Same shape as stage 1's, deliberately.

    Stage 1 already owns an ``httpx`` transport with the deterministic
    settings and the cached-token accounting worked out. A second one here
    would be a second thing to keep right.
    """

    def complete(self, instructions: str, description: str) -> tuple[str, Usage]:
        ...


@dataclass
class Explanation:
    """Prose for a decision, and where it came from.

    ``text`` is always faithful. When the model's version was refused,
    ``text`` is the deterministic rendering and ``rejected`` says what the
    model tried to cite - which belongs in the trace, because a model
    reaching for GRI 3(b) on a case resolved at GRI 1 is a fact about the
    explanation stage worth seeing.
    """

    text: str
    source: str = "rules"
    rejected: list[str] = field(default_factory=list)
    #: The prose that was refused, kept for diagnosis and never shipped.
    #: Discarding it made the refusal rate unauditable: recovering the text
    #: by asking the model again returns a *different* generation, because
    #: this model is not deterministic at temperature 0, so the thing judged
    #: and the thing read were never the same text.
    refused_text: str = ""
    usage: Usage | None = None

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "source": self.source,
            "rejected": self.rejected,
            "refused_text": self.refused_text,
            # Tokens belong in the stage that spent them. Omitting them left
            # the one traced stage with a model in it carrying no cost.
            "input_tokens": self.usage.input_tokens if self.usage else 0,
            "output_tokens": self.usage.output_tokens if self.usage else 0,
        }


def _prompt(decision: Decision, description: str, texts: dict[str, str]) -> str:
    """What the model is shown. The decision, and nothing it could decide with."""
    lines = [
        f"Goods: {description}",
        f"Code decided: {decision.code or 'none - the case was referred to a classifier'}",
    ]
    if decision.code and texts.get(decision.code):
        lines.append(f"That code reads: {texts[decision.code]}")
    lines.append("Rules applied, in order: " + (", ".join(decision.rule_path) or "none"))
    if decision.notes_cited:
        lines.append("Notes consulted: " + "; ".join(decision.notes_cited))
    if decision.conditions_applied:
        lines.append(
            "Provisions excluded on their own terms: "
            + "; ".join(decision.conditions_applied)
        )
    if decision.reason:
        lines.append(f"The finding that resolved it: {decision.reason}")
    return "\n".join(lines)


class Explainer:
    """Stage 4. Model prose when it passes the gate, rendered rules when not."""

    def __init__(
        self,
        transport: Transport | None = None,
        texts: dict[str, str] | None = None,
        instructions: str = INSTRUCTIONS,
    ) -> None:
        #: None is a supported configuration, not a degraded one: the
        #: deterministic rendering is a complete explanation.
        self.transport = transport
        self.texts = texts or {}
        self.instructions = instructions

    def explain(self, decision: Decision, description: str) -> Explanation:
        fallback = render(decision, self.texts)
        if self.transport is None:
            return Explanation(fallback)

        content, usage = self.transport.complete(
            self.instructions, _prompt(decision, description, self.texts)
        )
        try:
            text = str(json.loads(content).get("explanation") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            return Explanation(
                fallback, usage=usage, rejected=["response was not JSON"]
            )
        if not text:
            return Explanation(fallback, usage=usage, rejected=["response was empty"])

        # No ``quoted``: the model is told not to restate the description, so
        # nothing it writes is excused as quotation.
        invented = unsupported(text, decision, self.texts)
        if invented:
            return Explanation(
                fallback, usage=usage, rejected=invented, refused_text=text
            )
        return Explanation(text, source="model", usage=usage)

    def __call__(self, decision: Decision, description: str) -> str:
        return self.explain(decision, description).text
