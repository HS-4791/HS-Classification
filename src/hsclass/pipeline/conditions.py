"""Provision text as conditions over typed attributes - the KAG layer.

This is the question only this layer answers: *does this product satisfy the
condition written into this provision?* Retrieval cannot. "Containing 85
percent or more by weight of wool" and "Containing less than 85 percent by
weight of wool" are nearly the same string and describe opposite goods; a
similarity score puts them next to each other, and only arithmetic separates
them. 980 lines in the schedule state a percentage in their own text, and 95
binding notes do.

Two things here were learned by measuring the parser against the real
schedule rather than by reasoning about it, and both were silent:

* **"not over" inverted the comparison.** With only "over" in the operator
  vocabulary, "containing not over 6 percent by weight of butterfat" parsed
  as *greater than 6* - a confident, wrong arithmetic answer on 100-odd
  lines. Every comparison phrase the schedule actually uses is now spelled
  out in ``LEADING``/``TRAILING``, and a phrase that is not listed yields no
  condition instead of a partial match.
* **The attribute is as often stated before the number as after it.** "Of a
  fat content, by weight, not exceeding 1 percent" was the single largest
  unparsed shape; it is read now, from the words in front of "content".

A range ("over 6 percent but not over 35 percent by weight of butterfat") is
two conditions sharing one attribute, which is why the comparison phrases
that *introduce* a second bound - "but not over", "but less than" - are
leading phrases rather than trailing ones. Reading "but not over" as a suffix
on the first number is what produced the second row of that table.

What this does **not** do is decide a provision when its condition cannot be
evaluated. An unevaluated condition has not failed, and dropping a candidate
on one would be exactly the confident-but-wrong behaviour the architecture
exists to prevent, so every path that cannot test a condition returns None
and the caller must keep the two apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Comparison phrases that come *before* the number. Every one is written out
#: because a missing phrase is not a missed condition - it is a partial match
#: that inverts the comparison, which is how "not over" became ">".
LEADING: dict[str, str] = {
    "over": ">",
    "above": ">",
    "exceeding": ">",
    "more than": ">",
    "in excess of": ">",
    "but not over": "<=",
    "but not exceeding": "<=",
    "but not more than": "<=",
    "not over": "<=",
    "not exceeding": "<=",
    "not more than": "<=",
    "up to": "<=",
    "at least": ">=",
    "not less than": ">=",
    "but less than": "<",
    "less than": "<",
    "under": "<",
    "below": "<",
}

#: Comparison phrases that come *after* the number. Only forms that cannot
#: begin a further bound belong here; see the module docstring.
TRAILING: dict[str, str] = {
    "or more": ">=",
    "or over": ">=",
    "or higher": ">=",
    "or greater": ">=",
    "or less": "<=",
    "or lower": "<=",
}

ATMOST, ATLEAST = {"<", "<="}, {">", ">="}


def _alternation(phrases: dict[str, str]) -> str:
    """Longest first, so "not over" wins over "over"."""
    return "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True))


THRESHOLD_RE = re.compile(
    rf"(?:\b(?P<lead>{_alternation(LEADING)})\s+)?"
    rf"(?P<value>\d{{1,3}}(?:\.\d+)?)\s*percent"
    rf"(?:\s+(?P<trail>{_alternation(TRAILING)})\b)?",
    re.I,
)

#: What the percentage is measured on. Only a share of the whole by weight is
#: comparable with the composition the facts carry; a share of the dry matter,
#: of volume or of value is a different quantity and is refused rather than
#: compared. Order matters - "dry weight" also contains "weight".
BASES: tuple[tuple[str, str], ...] = (
    ("dry_weight", r"dry\s+weight|weight\s+of\s+the\s+dry\s+matter"),
    ("volume", r"by\s+volume"),
    ("value", r"ad\s+valorem|by\s+value|of\s+the\s+value"),
    ("weight", r"by\s+weight"),
)

#: "A milkfat content, by weight of the dry matter, of 5 percent or more" -
#: the attribute is in front of the word "content".
CONTENT_RE = re.compile(r"\b(?:a|an|the)?\s*(?P<name>[a-z][a-z'\- ]{2,30}?)\s+content\b", re.I)

#: "85 percent or more by weight of wool" - the attribute follows the number.
OF_RE = re.compile(r"\bof\s+(?P<name>[a-z][a-z'\- ]{2,40})", re.I)

#: "Containing by weight 99 percent or more lactose" - no preposition at all,
#: the attribute simply follows the threshold.
BARE_RE = re.compile(r"^[,\s]*(?P<name>[a-z][a-z'\- ]{2,40})", re.I)

#: How far past a threshold to look for the attribute it applies to.
LOOKAHEAD = 60

#: Words that end an attribute phrase, or cannot be one. Without these,
#: "over 65 percent by dry weight of sugar described in additional U.S. note
#: 2" names an attribute called "sugar described in".
#: ``vol``, ``one`` and ``as`` are here because the parser reached for them
#: and produced attributes with no meaning - "80 percent vol.", "of one or
#: more aromatic substances", "expressed as". Yielding no condition is the
#: safe failure; naming an attribute that is not one can eliminate a correct
#: candidate, which is the failure this layer must not have.
STOP_WORDS = frozenset(
    """a an the and or but not no of in on to by with for from this that these those
    any such which what other others more less than over under weight volume value
    content percent dry matter made containing contains consisting wholly described
    specified provided entered imported heading subheading chapter note section
    following kind kinds thereof each its having one vol as expressed calculated
    equivalent""".split()
)

#: Clause boundaries. An inherited chain is several provisions joined up, and
#: an attribute named in one of them does not govern a threshold in another.
CLAUSE_RE = re.compile(r"[;—]|\s-\s")


@dataclass(frozen=True)
class Condition:
    """One evaluable constraint: an attribute, a comparison and a number."""

    attribute: str
    operator: str  # >= | <= | > | <
    value: float
    basis: str = "weight"
    source: str = ""

    def satisfied_by(self, materials: dict[str, float]) -> bool | None:
        """True, False, or None when this cannot be tested at all.

        None is the important return, and it covers two different gaps: the
        attribute is not among the facts, or the threshold is measured on
        something the facts do not carry. A condition on a material the
        description never mentions has not failed - it has not been tested -
        and the caller must not treat the two alike.
        """
        if self.basis != "weight":
            return None
        share = _share_of(self.attribute, materials)
        if share is None:
            return None
        if self.operator == ">=":
            return share >= self.value
        if self.operator == "<=":
            return share <= self.value
        if self.operator == ">":
            return share > self.value
        return share < self.value


def _share_of(attribute: str, materials: dict[str, float]) -> float | None:
    """What share of the product this attribute makes up, if it is named."""
    wanted = {w for w in attribute.lower().split() if w not in STOP_WORDS}
    if not wanted:
        return None
    for name, share in materials.items():
        if wanted & {w for w in name.lower().split() if w not in STOP_WORDS}:
            return share
    return None


def _trim(phrase: str, limit: int = 3, from_end: bool = False) -> str:
    """Reduce a captured phrase to the attribute it names, or to nothing.

    Read from whichever end the anchor is on, because the attribute is the
    part touching it: "of **sugar** described in additional note 2" names
    sugar, and "a **fat** content" names fat. Reading either from the far end
    returns a stop word and loses the attribute, or keeps the words around it
    and invents one.
    """
    words = [w for w in re.split(r"[^a-z'\-]+", phrase.lower()) if w]
    kept: list[str] = []
    for word in reversed(words) if from_end else words:
        if word in STOP_WORDS:
            break
        kept.append(word)
        if len(kept) == limit:
            break
    return " ".join(reversed(kept) if from_end else kept)


def _basis_of(clause: str) -> str:
    for name, pattern in BASES:
        if re.search(pattern, clause, re.I):
            return name
    return "unknown"


def _attribute_in(clause: str, end: int) -> str:
    """The attribute a threshold applies to, from either side of it.

    "by weight of wool" first, because that is the form the threshold itself
    governs; then "<x> content" in front of it; then a bare attribute
    following it. Nothing found is a real answer - "Not more than 25 percent
    by weight" names no attribute of its own, and taking one from a parent
    provision would be a guess about which parent governs.
    """
    following = OF_RE.search(clause, end, end + LOOKAHEAD)
    if following and (name := _trim(following.group("name"))):
        return name
    for match in CONTENT_RE.finditer(clause[:end]):
        if name := _trim(match.group("name"), limit=2, from_end=True):
            return name
    bare = BARE_RE.match(clause[end: end + LOOKAHEAD])
    if bare and (name := _trim(bare.group("name"), limit=2)):
        return name
    return ""


def _operator(lead: str, trail: str) -> str:
    """The comparison, or "" when the text does not state one unambiguously.

    A bare percentage states no comparison. Lead and trail pointing opposite
    ways is text this parser has not understood, and guessing between them is
    how a threshold ends up reversed.
    """
    before, after = LEADING.get(lead.lower(), ""), TRAILING.get(trail.lower(), "")
    if before and after and not (
        ({before} | {after}) <= ATMOST or ({before} | {after}) <= ATLEAST
    ):
        return ""
    return before or after


def conditions_in(text: str) -> list[Condition]:
    """Every evaluable threshold a provision states."""
    found: list[Condition] = []
    for clause in CLAUSE_RE.split(text):
        basis = _basis_of(clause)
        for match in THRESHOLD_RE.finditer(clause):
            operator = _operator(match.group("lead") or "", match.group("trail") or "")
            if not operator:
                continue
            attribute = _attribute_in(clause, match.end())
            if not attribute:
                continue
            found.append(
                Condition(
                    attribute=attribute,
                    operator=operator,
                    value=float(match.group("value")),
                    basis=basis,
                    source=clause[match.start(): match.end()].strip(),
                )
            )
    return found


def _satisfiable(group: list[Condition]) -> bool:
    """Could any product satisfy all of these at once?

    1901.90.28.00 names both "whey solids containing over 5.5 percent by
    weight of butterfat" and "dried whey ... which contain 5.5 percent or
    less by weight of butterfat". Both phrases are read correctly; they are
    conditions on different *components* of the mixture, and this layer
    models conditions on the product. The flattened set is unsatisfiable,
    which is the signal that it cannot be attributed to the product at all -
    so it is refused rather than evaluated to False against every product.
    """
    low, high = float("-inf"), float("inf")
    low_strict = high_strict = False
    for c in group:
        if c.operator in ATLEAST and c.value >= low:
            low, low_strict = c.value, c.operator == ">"
        elif c.operator in ATMOST and c.value <= high:
            high, high_strict = c.value, c.operator == "<"
    if low > high:
        return False
    return not (low == high and (low_strict or high_strict))


def consistent_conditions(text: str) -> list[Condition]:
    """The conditions a provision states *about the product itself*.

    Conditions whose flattened set is unsatisfiable are dropped, because an
    unsatisfiable group means this layer has mis-attributed them, not that no
    product qualifies.
    """
    groups: dict[tuple[str, str], list[Condition]] = {}
    for c in conditions_in(text):
        groups.setdefault((c.attribute, c.basis), []).append(c)
    return [c for group in groups.values() if _satisfiable(group) for c in group]


def evaluate(text: str, materials: dict[str, float]) -> bool | None:
    """Does this provision's text hold for these materials?

    True if every condition that could be tested is satisfied, False if any
    is contradicted, and None if the provision states no condition, states
    only ones its attributes were never given for, or states a set this layer
    cannot attribute to the product.
    """
    tested = [
        result
        for result in (c.satisfied_by(materials) for c in consistent_conditions(text))
        if result is not None
    ]
    if not tested:
        return None
    return all(tested)
