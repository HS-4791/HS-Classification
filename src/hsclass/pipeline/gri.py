"""The GRI engine: the stage that decides, with no model in it.

The General Rules of Interpretation are a procedure, not a heuristic. They
apply **in order** and you stop at the first that resolves; reaching Rule 3
when Rule 1 would have answered is itself a classification error, which is
why the engine records *which rule fired* rather than only the code.

Nothing here is probabilistic. The same facts and the same candidates give
the same decision every time, on any machine, with no API key. That is the
property the whole architecture is arranged around: stages 1 and 2 are
allowed to be wrong because this stage checks them, and stage 4 can only
explain a decision this stage already made.

What is implemented, and what is honestly not:

* **GRI 1** - the terms of the headings and any relative Section or Chapter
  Notes. Exclusions are applied from the notes graph.
* **Section XI Note 2(A)** - the predominant-weight test, which is what
  decides a 60/40 cotton-polyester shirt. Implemented because the condition
  is arithmetic over attributes the facts carry.
* **GRI 3(c)** - the heading last in numerical order, among those equally
  meriting consideration. Deterministic by definition.
* **GRI 3(b)** - essential character. *Not* implemented, by choice: it is a
  judgment call, and the primer is explicit that a system answering these
  confidently is more dangerous than one that flags them. It escalates.
* **GRI 2(a), 4, 5** - not yet reached by the current candidate generator.

Anything the rules do not resolve escalates with a ranked shortlist. A
classifier that always answers is not deployable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from hsclass.schedule.exclusions import ExclusionGraph
from hsclass.schedule.notes import Note

#: "60% cotton", "60 percent cotton", "cotton 60%".
PERCENT_BEFORE_RE = re.compile(
    r"(\d{1,3}(?:\.\d+)?)\s*(?:%|percent|pct)\s*(?:of\s+)?([a-z][a-z \-]*)", re.I
)
#: Only the single word touching the number: "cotton 60%". Allowing a run of
#: words here swallows the sentence around it - "a girls' pullover sweater
#: made of knitted fabric with 90% acrylic" yielded a material called
#: "pullover sweater made", sharing acrylic's 90% and tying with it, so
#: nothing predominated and the rule never fired.
PERCENT_AFTER_RE = re.compile(
    r"([a-z][a-z\-]*)\s*[:\-]?\s*(\d{1,3}(?:\.\d+)?)\s*(?:%|percent|pct)", re.I
)

#: The schedule phrases a threshold as "85 percent or more by weight of
#: wool", so the words between the number and the material have to come off
#: before what is left can be read as a material at all.
FILLER = {
    "or", "more", "less", "by", "weight", "of", "the", "a", "an", "not",
    "exceeding", "than", "containing", "and", "with", "made", "from",
    "value", "volume", "ad", "valorem", "least", "over", "under", "its",
}

#: Section XI Note 2(A) governs goods of chapters 50 to 55 directly.
NOTE_2A_CHAPTERS = {f"{n:02d}" for n in range(50, 56)}
#: Chapters 56 to 63 - where the running example's chapter 61 sits - reach
#: the same test indirectly: Section XI Subheading Note 2(A) says their goods
#: are regarded as wholly of the material "which would be selected under note
#: 2 to this section" for a chapter 50-55 product. Citing Note 2(A) alone
#: skips the provision that actually brings it to bear on a knitted shirt.
SUBHEADING_NOTE_2A_CHAPTERS = {f"{n:02d}" for n in range(56, 64)}
SECTION_XI_CHAPTERS = NOTE_2A_CHAPTERS | SUBHEADING_NOTE_2A_CHAPTERS


@dataclass
class Facts:
    """The typed attributes a rule can be evaluated against.

    Stage 1 is meant to fill this with a model reading text and images. What
    is here is deterministic extraction of the one attribute the rules in
    this engine actually test - composition by weight - so the adjudicator
    can be exercised and measured before the model exists.
    """

    description: str = ""
    materials: dict[str, float] = field(default_factory=dict)

    @property
    def is_mixture(self) -> bool:
        return len(self.materials) > 1

    def predominant(self) -> tuple[str, float] | None:
        """The material with the greatest share, or None if it is a tie.

        Section XI Note 2(A) turns on predominance *over each other single*
        material, so an exact tie does not resolve and must not be guessed.
        """
        if not self.materials:
            return None
        ranked = sorted(self.materials.items(), key=lambda kv: -kv[1])
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            return None
        return ranked[0]


def _material_of(phrase: str) -> str:
    """Reduce a captured phrase to the material it names, or to nothing.

    The material ends at the first connective: "100% polyester with a
    self-fabric hood" names polyester, not "polyester with various". Leading
    qualifiers are skipped instead, because the schedule writes thresholds as
    "85 percent or more by weight of wool".
    """
    material: list[str] = []
    for word in (w for w in re.split(r"[^a-z\-]+", phrase.lower()) if w):
        if word in FILLER:
            if material:
                break  # the material has ended
            continue  # still skipping the qualifier in front of it
        material.append(word)
        if len(material) == 3:
            break
    return " ".join(material)


def extract_facts(description: str) -> Facts:
    """Read composition percentages out of a product description.

    The first reading of a material wins, so "60% cotton" is not overwritten
    by a later clause that happens to re-capture the same word.
    """
    materials: dict[str, float] = {}
    for pattern, flip in ((PERCENT_BEFORE_RE, False), (PERCENT_AFTER_RE, True)):
        for match in pattern.finditer(description):
            phrase = match.group(1) if flip else match.group(2)
            share = float(match.group(2) if flip else match.group(1))
            material = _material_of(phrase)
            if material and 0 < share <= 100 and len(material) <= 40:
                materials.setdefault(material, share)
    return Facts(description=description, materials=materials)


@dataclass
class Decision:
    """What the engine decided, and every step that got it there."""

    code: str | None
    rule_path: list[str] = field(default_factory=list)
    notes_cited: list[str] = field(default_factory=list)
    escalated: bool = False
    shortlist: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "rule_path": self.rule_path,
            "notes_cited": self.notes_cited,
            "escalated": self.escalated,
            "shortlist": self.shortlist,
            "reason": self.reason,
        }


class GRIEngine:
    """Applies the rules in order and stops at the first that resolves."""

    def __init__(self, notes: list[Note], exclusions: ExclusionGraph) -> None:
        self.exclusions = exclusions
        self.notes_by_chapter: dict[str, list[Note]] = {}
        for note in notes:
            if note.is_binding and note.chapter:
                self.notes_by_chapter.setdefault(note.chapter, []).append(note)

    # ---------------------------------------------------------------- GRI 1

    def _excluded(self, chapter: str, facts: Facts) -> list[str]:
        """Notes that remove goods like these from this chapter.

        An exclusion only bites when the goods answer its description, and
        deciding that is what the week-3 rule layer is for. Until then the
        engine reports the exclusions in scope rather than acting on them,
        because silently dropping a candidate on an unevaluated condition is
        exactly the confident-but-wrong behaviour the project exists to avoid.
        """
        return [
            edge.citation
            for edge in self.exclusions.excluded_from(chapter)
            if _mentions(edge.text, facts.description)
        ]

    # --------------------------------------------------- Section XI Note 2(A)

    def _textile_material_rule(
        self, facts: Facts, candidates: list[str]
    ) -> tuple[str, list[str]] | None:
        """Pick the subheading for the material that predominates by weight.

        This is the running example: a knitted shirt of 60% cotton and 40%
        polyester is classified as if wholly of cotton, so 6109.10 rather
        than 6109.90. Reverse the blend and the answer changes with it - a
        number in a composition table decides the legal outcome.
        """
        if not facts.is_mixture:
            return None
        chapters = {code[:2] for code in candidates}
        if not chapters & SECTION_XI_CHAPTERS:
            return None
        predominant = facts.predominant()
        if predominant is None:
            return None

        material, _ = predominant
        matching = [
            code for code in candidates
            if _describes_material(self._text_for(code), material)
        ]
        if len(matching) != 1:
            return None
        cited = ["Section XI Note 2(A)"]
        if chapters & SUBHEADING_NOTE_2A_CHAPTERS:
            # The subheading note is what reaches chapters 56-63; Note 2(A)
            # is the test it defers to. A ruling cites both, in that order.
            cited = ["Section XI Subheading Note 2(A)", "Section XI Note 2(A)"]
        return matching[0], cited

    # ------------------------------------------------------------- GRI 3(c)

    @staticmethod
    def _last_in_numerical_order(candidates: list[str]) -> str:
        """GRI 3(c): among those equally meriting consideration, the last."""
        return max(candidates)

    # ------------------------------------------------------------- the walk

    def adjudicate(
        self, facts: Facts, candidates: list[str], texts: dict[str, str] | None = None
    ) -> Decision:
        """Walk GRI 1, then 2 and 3, stopping at the first rule that resolves."""
        self._texts = texts or {}
        if not candidates:
            return Decision(None, ["GRI 1"], escalated=True,
                            reason="no candidate headings were proposed")

        notes_cited: list[str] = []
        for chapter in dict.fromkeys(code[:2] for code in candidates):
            notes_cited.extend(self._excluded(chapter, facts))

        # GRI 1 - one candidate, nothing to choose between.
        if len(set(candidates)) == 1:
            return Decision(candidates[0], ["GRI 1"], notes_cited,
                            reason="a single heading describes the goods")

        # GRI 6 operates inside a settled heading, applying rules 1 to 5
        # again. Where the candidates share a heading, that is where we are.
        headings = {code[:4] for code in candidates}
        resolved = self._textile_material_rule(facts, candidates)
        if resolved:
            code, cited = resolved
            path = ["GRI 1", *cited, "GRI 6"] if len(headings) == 1 else ["GRI 1", *cited]
            return Decision(code, path, [*notes_cited, *cited],
                            reason="the material predominating by weight decides the subheading")

        # GRI 3(b) - essential character. A judgment call, so it escalates.
        if facts.is_mixture and facts.predominant() is None:
            return Decision(
                None, ["GRI 1", "GRI 2(b)", "GRI 3(a)", "GRI 3(b)"], notes_cited,
                escalated=True, shortlist=candidates[:5],
                reason="no material predominates; essential character is a judgment call",
            )

        return Decision(
            None, ["GRI 1", "GRI 2(b)", "GRI 3(a)"], notes_cited,
            escalated=True, shortlist=candidates[:5],
            reason="more than one heading applies and no rule in this engine separates them",
        )

    def _text_for(self, code: str) -> str:
        return self._texts.get(code, "")


def _mentions(note_text: str, description: str) -> bool:
    """Does the description use a term the note turns on?

    Deliberately crude: it decides whether a note is *worth consulting*, not
    whether it applies. Deciding that is the week-3 rule layer's job.
    """
    words = {w for w in re.findall(r"[a-z]{5,}", note_text.lower())}
    seen = {w for w in re.findall(r"[a-z]{5,}", description.lower())}
    return bool(words & seen)


def _describes_material(text: str, material: str) -> bool:
    head = material.split()[-1] if material.split() else material
    return bool(head) and head in text.lower()
