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
* **GRI 2(b)** - goods of more than one material are settled by Rule 3. Used
  as the trigger it is: without a mixture there is nothing to send onward.
* **GRI 3(a)** - the most specific description wins, implemented narrowly: a
  heading that names the goods beats a residual "Other" provision. Which of
  two substantive descriptions is *more* specific is a judgment, and is not
  attempted.
* **GRI 3(b)** - essential character. *Not* implemented, by choice: it is a
  judgment call, and the primer is explicit that a system answering these
  confidently is more dangerous than one that flags them. It escalates.
* **GRI 3(c)** - the principle is implemented, but reached only through
  Section XI Note 2(A), which borrows it for its own tie-break. GRI 3(c)
  proper needs the competing headings to "equally merit consideration", and
  establishing that from retrieval candidates is not something this engine
  can honestly claim to do.
* **GRI 2(a)** - an incomplete, unfinished or unassembled article is the
  finished article. The trigger is textual ("unassembled", "knocked-down"),
  and the rule directs classification away from a parts provision toward the
  whole. Whether the goods already have the finished article's *essential
  character* is a judgment, and is not attempted.
* **GRI 4** - **deliberately not implemented**, on evidence rather than
  convenience: it is the deciding rule in **0 of 18,253** rulings, and "the
  heading for goods they are most akin to" is a similarity judgment. Deciding
  it would mean putting a similarity call inside stage 3, which is precisely
  the thing this stage exists not to do. Naming it on a rule path without
  evaluating it would be the fabrication this engine was corrected for once
  already.
* **GRI 5** - not implemented.

``rule_path`` records only rules that were actually evaluated. Naming a rule
the walk never reached would put a fabrication into the trace that week 4
scores explanations against.

Anything the rules do not resolve escalates with a ranked shortlist. A
classifier that always answers is not deployable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from hsclass.pipeline.conditions import consistent_conditions
from hsclass.schedule.exclusions import ExclusionGraph

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

#: GRI 2(a)'s trigger, as descriptions actually state it. 213 of the 18,253
#: rulings carry one of these words; 44 of them are cases the ruling itself
#: decided under 2(a).
INCOMPLETE_RE = re.compile(
    r"\b(?:unassembled|disassembled|knocked[-\s]down|unfinished|incomplete"
    r"|in kit form|not assembled|ready to assemble)\b",
    re.I,
)

#: A provision covering parts rather than the article. "Parts thereof",
#: "parts and accessories".
PARTS_RE = re.compile(r"\bparts?\b", re.I)

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
    #: Provisions eliminated because the goods contradict a condition stated
    #: in their own text, quoted as the schedule writes it. Kept separate
    #: from ``notes_cited`` because the terms of a heading are not a note,
    #: and a trace that blurred them would cite the wrong authority.
    conditions_applied: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "rule_path": self.rule_path,
            "notes_cited": self.notes_cited,
            "escalated": self.escalated,
            "shortlist": self.shortlist,
            "reason": self.reason,
            "conditions_applied": self.conditions_applied,
        }


class GRIEngine:
    """Applies the rules in order and stops at the first that resolves."""

    def __init__(self, exclusions: ExclusionGraph) -> None:
        self.exclusions = exclusions

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

    # ------------------------------------------ GRI 1: the terms of a heading

    def _condition_rule(
        self, facts: Facts, candidates: list[str], texts: dict[str, str]
    ) -> tuple[list[str], list[str]]:
        """Drop candidates whose own stated conditions the goods contradict.

        This is GRI 1 at its most direct - the terms of the heading - and at
        subheading level GRI 6, which is the same comparison one level down.
        "Containing 70 percent or more by weight of silk or silk waste" is not
        a description to be matched by similarity; it is a threshold, and a
        product of 40 percent silk is outside it as a matter of arithmetic.

        Only a condition evaluated to False removes anything. A condition
        that could not be tested leaves its candidate standing, because an
        untested condition has not failed - and the elimination is abandoned
        altogether if it would empty the list, since every candidate failing
        means the facts or the reading are wrong rather than the goods being
        unclassifiable.

        The same discipline applies to the survivor, and it took two goes to
        get right. A women's cardigan of 53 percent rayon and 47 percent wool
        had its "85 percent or more by weight of wool" siblings correctly
        eliminated, and what was left standing was "Containing 30 percent or
        more by weight of silk or silk waste" - kept only because the goods
        say nothing about silk, so the condition could not be tested.
        Surviving by *ignorance* is not qualifying.

        Surviving by *silence* is not either, which the first fix missed. A
        women's coat of 70 percent polyester and 30 percent wool had four
        chapter 62 candidates each requiring 36 percent wool or more, all
        four correctly eliminated - and the fifth candidate was 6405.20.6060,
        "Other footwear - with soles and uppers of wool felt". It states no
        threshold at all, so it was neither failed nor untested, and the
        engine decided a coat was footwear under GRI 1 with a citation
        attached. A provision that simply has no condition is not thereby the
        answer.

        So the elimination may only decide between provisions that were
        actually competing on these terms: the lone survivor has to share a
        **heading** with something that was eliminated. That is the GRI 6
        comparison this rule is for - sibling subheadings of one heading,
        where a threshold distinguishes the goods. A survivor from another
        heading was never in the same contest, and the honest outcome is to
        set the elimination aside and let the ordinary rules run.
        """
        if not facts.materials:
            return candidates, []
        kept, failed, untested = [], [], set()
        for code in candidates:
            text = texts.get(code, "")
            results = [
                c.satisfied_by(facts.materials)
                for c in consistent_conditions(text)
            ]
            if False in results:
                failed.append(f"{code}: {text}")
                continue
            if None in results:
                untested.add(code)
            kept.append(code)
        if not failed or not kept:
            return candidates, []
        if len(set(kept)) == 1:
            eliminated = {entry.split(":")[0][:4] for entry in failed}
            if kept[0] in untested or kept[0][:4] not in eliminated:
                return candidates, []
        return kept, failed

    # --------------------------------------------------- Section XI Note 2(A)

    def _textile_material_rule(
        self, facts: Facts, candidates: list[str], texts: dict[str, str]
    ) -> tuple[str, list[str], str] | None:
        """Apply Note 2(A), including the tie-break written into it.

        The running example: a knitted shirt of 60% cotton and 40% polyester
        is classified as if wholly of cotton, so 6109.10 rather than 6109.90.
        Reverse the blend and the answer changes with it - a number in a
        composition table decides the legal outcome.

        The note does not stop at predominance. It continues: "When no one
        textile material predominates by weight, the goods are to be
        classified as if consisting wholly of that one textile material which
        is covered by the heading which occurs last in numerical order among
        those which equally merit consideration." A 50/50 blend is therefore
        settled here, under GRI 1, and never reaches GRI 3 at all - treating
        it as an essential-character judgment would be the error the primer
        warns about, reaching Rule 3 where Rule 1 answered.
        """
        if not facts.is_mixture:
            return None

        # A Section XI note can only classify within Section XI. Applying the
        # test to every candidate let it place goods outside the section
        # entirely: a woman's 100% nylon lace dress with a 100% polyester
        # slip ties at 100, so the rule reached its "last in numerical order"
        # tie-break, and ``max`` over a candidate set spanning chapters
        # returned 6405.20.6060 - "Other footwear, with soles and uppers of
        # wool felt" - citing a textile note to put a dress in chapter 64.
        # The note governs chapters 50 to 63 and nothing else, so the
        # candidates it may choose between are only those.
        within = [code for code in candidates if code[:2] in SECTION_XI_CHAPTERS]
        if not within:
            return None
        chapters = {code[:2] for code in within}

        cited = ["Section XI Note 2(A)"]
        if chapters & SUBHEADING_NOTE_2A_CHAPTERS:
            # The subheading note is what reaches chapters 56-63; Note 2(A)
            # is the test it defers to. A ruling cites both, in that order.
            cited = ["Section XI Subheading Note 2(A)", "Section XI Note 2(A)"]

        predominant = facts.predominant()
        if predominant is not None:
            matching = _matching(within, [predominant[0]], texts)
            if len(matching) != 1:
                return None
            return matching[0], cited, "the material predominating by weight decides the subheading"

        heaviest = max(facts.materials.values())
        tied = [m for m, share in facts.materials.items() if share == heaviest]
        matching = _matching(within, tied, texts)
        if len(matching) < 2:
            return None
        return (
            self._last_in_numerical_order(matching),
            cited,
            "no material predominates, so the note takes the heading last in numerical order",
        )

    # ------------------------------------------------------------- GRI 3(a)

    @staticmethod
    def _most_specific(candidates: list[str], texts: dict[str, str]) -> list[str]:
        """Candidates that name the goods, rather than catching what is left.

        A narrow reading of GRI 3(a): a heading naming the article beats a
        residual "Other" provision. The rule's full sense - which of two
        substantive descriptions is *more specific* - is a judgment this
        engine does not make.
        """
        named = [c for c in candidates if not _is_residual(texts.get(c, ""))]
        return named if named else list(candidates)

    # ------------------------------------------------------------- GRI 3(c)

    @staticmethod
    def _last_in_numerical_order(candidates: list[str]) -> str:
        """The heading occurring last in numerical order.

        The principle GRI 3(c) states, and which Section XI Note 2(A) borrows
        for its own tie-break. It is reached here only through the note: GRI
        3(c) proper requires the competing headings to "equally merit
        consideration", and establishing that is a legal judgment this engine
        does not make from retrieval candidates.
        """
        return max(candidates)

    # ------------------------------------------------------------- the walk

    def adjudicate(
        self, facts: Facts, candidates: list[str], texts: dict[str, str] | None = None
    ) -> Decision:
        """Apply the terms of the competing provisions, then walk the rules.

        The conditions written into the provisions come first because they
        *are* GRI 1 - the terms of the headings - and a rule later in the
        order must not be reached while an earlier one still decides. What
        they do not settle they still narrow, and the walk continues on the
        candidates that survive.
        """
        texts = texts or {}
        if not candidates:
            return Decision(None, ["GRI 1"], escalated=True,
                            reason="no candidate headings were proposed")

        narrowed, failed = self._condition_rule(facts, list(candidates), texts)
        if failed and len(set(narrowed)) == 1:
            # GRI 6 is GRI 1 applied between subheadings of one heading, so it
            # is named only when that is what the comparison was.
            path = ["GRI 1", "GRI 6"] if len({c[:4] for c in candidates}) == 1 else ["GRI 1"]
            return Decision(
                narrowed[0], path, self._notes_for(narrowed, facts),
                conditions_applied=failed,
                reason="the goods fail the conditions stated in every competing "
                       "provision but one",
            )

        decision = self._walk(facts, narrowed, texts)
        decision.conditions_applied = failed
        return decision

    def _notes_for(self, candidates: list[str], facts: Facts) -> list[str]:
        """Notes in scope, cited in chapter order rather than candidate order.

        Taking the chapters as they happen to arrive made the citation order
        depend on how the proposer ranked its candidates: the same decision,
        with the same notes, would name a different authority first. Week 4
        scores explanations against these traces, so the order has to come
        from the nomenclature and not from the retrieval stage.
        """
        notes: list[str] = []
        for chapter in sorted({code[:2] for code in candidates}):
            notes.extend(self._excluded(chapter, facts))
        # One citation per note. A Section note excludes goods from several
        # chapters, so a shortlist spanning chapters 61 and 62 cited
        # "Section XI Note 1(c)" twice - reading, in a trace, as though the
        # officer had relied on it twice over.
        return list(dict.fromkeys(notes))

    def _walk(self, facts: Facts, candidates: list[str], texts: dict[str, str]) -> Decision:
        """Walk the rules in order, stopping at the first that resolves.

        ``rule_path`` lists only rules that were actually evaluated. Writing
        down a rule the engine never reached would put a fabrication into the
        trace that week 4 scores explanations against.
        """
        notes_cited = self._notes_for(candidates, facts)
        path = ["GRI 1"]

        # GRI 1 - one candidate, nothing to choose between.
        if len(set(candidates)) == 1:
            return Decision(candidates[0], path, notes_cited,
                            reason="a single heading describes the goods")

        # A Section note resolving under GRI 1. GRI 6 applies the same logic
        # inside a settled heading, which is where we are when the candidates
        # share one.
        resolved = self._textile_material_rule(facts, candidates, texts)
        if resolved:
            code, cited, why = resolved
            full = [*path, *cited]
            if len({c[:4] for c in candidates}) == 1:
                full.append("GRI 6")
            return Decision(code, full, [*notes_cited, *cited], reason=why)

        # GRI 2(a) - an incomplete, unfinished or unassembled article is
        # classified as the finished one. It sits before 2(b) in the sequence.
        if INCOMPLETE_RE.search(facts.description):
            path.append("GRI 2(a)")
            whole = [c for c in candidates if not _is_parts_provision(texts.get(c, ""))]
            if len(whole) == 1:
                return Decision(
                    whole[0], path, notes_cited,
                    reason="an incomplete or unassembled article is classified as "
                           "the finished one, not as its parts",
                )

        # GRI 2(b) sends goods of more than one material to Rule 3. Without a
        # mixture there is no trigger, so the failure is at GRI 1 and the path
        # says so rather than naming rules that never ran.
        if not facts.is_mixture:
            return Decision(
                None, path, notes_cited, escalated=True, shortlist=candidates[:5],
                reason="more than one heading describes the goods and no note separates them",
            )

        path.append("GRI 2(b)")
        path.append("GRI 3(a)")
        specific = self._most_specific(candidates, texts)
        if len(specific) == 1:
            return Decision(
                specific[0], path, notes_cited,
                reason="one heading names the goods; the others are residual provisions",
            )

        path.append("GRI 3(b)")
        return Decision(
            None, path, notes_cited, escalated=True, shortlist=specific[:5],
            reason="essential character decides between these, and that is a judgment call",
        )


def _mentions(note_text: str, description: str) -> bool:
    """Does the description use a term the note turns on?

    Deliberately crude: it decides whether a note is *worth consulting*, not
    whether it applies. Deciding that is the week-3 rule layer's job.
    """
    words = {w for w in re.findall(r"[a-z]{5,}", note_text.lower())}
    seen = {w for w in re.findall(r"[a-z]{5,}", description.lower())}
    return bool(words & seen)


def _matching(candidates: list[str], materials: list[str], texts: dict[str, str]) -> list[str]:
    return [
        code for code in candidates
        if any(_describes_material(texts.get(code, ""), m) for m in materials)
    ]


def _is_parts_provision(text: str) -> bool:
    """Does this line cover parts rather than the article itself?

    GRI 2(a) turns on exactly this distinction: an unassembled bicycle is a
    bicycle, not a collection of bicycle parts.

    The whole inherited chain is searched, not just the line's own words:
    "Parts and accessories of vehicles of heading 8712 - Frames and forks"
    names parts in its heading and never again, so reading only the last
    segment makes a parts provision look like the finished article. That is
    the opposite of the residual test, where "Other" has to be the line's own
    description to mean anything.
    """
    return bool(PARTS_RE.search(text))


def _is_residual(text: str) -> bool:
    own = text.split("—")[-1].strip().rstrip(":").lower()
    return own in {"other", "others", "other articles"}


def _describes_material(text: str, material: str) -> bool:
    head = material.split()[-1] if material.split() else material
    return bool(head) and head in text.lower()
