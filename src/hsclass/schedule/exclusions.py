"""The exclusion graph: which notes push goods out of one chapter into another.

Notes do two jobs. They **define** what counts as what, and they **exclude**
goods into somewhere else. The exclusions are a graph rather than a list,
because the chapter a note sends you to has notes of its own, which may send
you on again or send you back. Resolving one classification can take several
hops through provisions that live in different parts of the schedule.

That is the question only this component answers: *this note excludes the
goods into chapter 48 - does chapter 48 send them back?* A single retrieval
pass cannot do it, because the second hop is not similar to the query; it is
similar to the answer of the first hop.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from hsclass.schedule.notes import Note
from hsclass.schedule.parse_pdf import SECTION_CHAPTERS

#: "This chapter does not cover:" and its variants. 417 provisions open this
#: way; the rest define terms rather than redirect goods.
EXCLUDES_RE = re.compile(r"\bdoes not (?:cover|apply to|include)\b", re.I)

#: A reference names a level and then one or more codes, possibly as a range
#: ("chapters 50 to 55") or a list ("heading 0501, 6703 or 6704").
LEVEL_RE = re.compile(r"\b(chapters?|headings?|subheadings?)\s+", re.I)
#: Each level writes its codes differently, and a single pattern gets chapters
#: wrong. The notes say "chapter 5", not "chapter 05" - twelve provisions cite
#: a single-digit chapter, including chapter 3's exclusion of goods into
#: chapter 5. A pattern demanding two digits drops those edges silently.
CODE_PATTERNS = {
    "chapter": re.compile(r"\d{1,2}(?!\d)"),
    "heading": re.compile(r"\d{4}(?:\.\d{2})*"),
    "subheading": re.compile(r"\d{4}(?:\.\d{2})*"),
}
#: A note can redirect to a whole Section by numeral - chapter 39 Note 12(p)
#: sends goods to "section XI (textiles and textile articles)", which is the
#: return edge for Section XI Note 1(h). Miss it and the round trip the
#: nomenclature actually contains stays invisible.
SECTION_REF_RE = re.compile(r"\bsection\s+([IVXL]+)\b", re.I)
JOIN_RE = re.compile(r"^(?:,|;|or|and|to|through|-)$", re.I)
RANGE_WORDS = {"to", "through", "-"}

LEVEL_DIGITS = {"chapter": 2, "heading": 4, "subheading": 6}


@dataclass(frozen=True)
class Reference:
    """A pointer to somewhere else in the nomenclature."""

    level: str  # chapter | heading | subheading
    code: str

    @property
    def chapters(self) -> list[str]:
        """Every chapter this reference reaches."""
        if self.level == "section":
            return [f"{n:02d}" for n in SECTION_CHAPTERS.get(self.code, range(0))]
        return [self.code[:2]]


@dataclass
class Edge:
    """One note sending goods from where it sits to somewhere else."""

    source_kind: str  # section | chapter
    source: str  # "XI" or "61"
    target: Reference
    citation: str
    text: str

    def to_dict(self) -> dict:
        return asdict(self) | {"target": asdict(self.target)}


def _expand(level: str, codes: list[str], ranged: list[bool]) -> list[str]:
    """Turn "chapters 50 to 55" into every chapter it names."""
    out: list[str] = []
    for i, code in enumerate(codes):
        if ranged[i] and out:
            try:
                start, stop = int(out[-1]), int(code)
            except ValueError:
                out.append(code)
                continue
            if 0 < stop - start < 100:
                width = max(len(out[-1]), LEVEL_DIGITS[level])
                out.extend(str(n).zfill(width) for n in range(start + 1, stop + 1))
                continue
        out.append(code)
    return out


def references_in(text: str) -> list[Reference]:
    """Every chapter, heading or subheading the text points at.

    Reads the codes that follow a level word, across the joins the schedule
    actually uses: "heading 0501, 6703 or 6704", "chapters 50 to 55".
    """
    found: list[Reference] = []
    for match in SECTION_REF_RE.finditer(text):
        found.append(Reference("section", match.group(1).upper()))
    for match in LEVEL_RE.finditer(text):
        level = match.group(1).lower().rstrip("s")
        width = LEVEL_DIGITS[level]
        code_pattern = CODE_PATTERNS[level]
        codes: list[str] = []
        ranged: list[bool] = []
        position = match.end()
        expecting_range = False
        while position < len(text):
            code = code_pattern.match(text, position)
            if not code:
                break
            codes.append(code.group(0))
            ranged.append(expecting_range)
            position = code.end()
            # step over a joining word to see whether the list continues
            rest = text[position:position + 12]
            join = re.match(r"\s*(,|;|or|and|to|through|-)\s*", rest, re.I)
            if not join:
                break
            expecting_range = join.group(1).lower() in RANGE_WORDS
            position += join.end()
        for code in _expand(level, codes, ranged):
            digits = code.replace(".", "")
            if level == "chapter":
                # The schedule writes "chapter 5"; a code is always two digits.
                digits = digits.zfill(width)
            if len(digits) >= width:
                found.append(Reference(level, digits[:width]))
    # keep document order, drop repeats
    return list(dict.fromkeys(found))


def is_exclusion(note: Note) -> bool:
    """Does this provision push goods elsewhere, or just define a term?"""
    return bool(EXCLUDES_RE.search(note.lead_in) or EXCLUDES_RE.search(note.text))


class ExclusionGraph:
    """Chapters and the notes that redirect goods between them."""

    def __init__(self, edges: list[Edge]) -> None:
        self.edges = edges
        self._out: dict[str, list[Edge]] = defaultdict(list)
        self._in: dict[str, list[Edge]] = defaultdict(list)
        for edge in edges:
            # A Section note binds every chapter in that Section, so it is
            # indexed under each of them - Section XI Note 1(h) is as much a
            # rule of chapter 61 as of chapter 50.
            for chapter in self._sources_of(edge):
                self._out[chapter].append(edge)
            for chapter in edge.target.chapters:
                self._in[chapter].append(edge)

    def __len__(self) -> int:
        return len(self.edges)

    @staticmethod
    def _sources_of(edge: Edge) -> list[str]:
        if edge.source_kind == "chapter":
            return [edge.source]
        return [f"{n:02d}" for n in SECTION_CHAPTERS.get(edge.source, range(0))]

    @property
    def chapters(self) -> set[str]:
        return set(self._out) | set(self._in)

    def excluded_from(self, chapter: str) -> list[Edge]:
        """Notes in this chapter that send goods away."""
        return self._out.get(chapter, [])

    def excluded_into(self, chapter: str) -> list[Edge]:
        """Notes elsewhere that send goods here."""
        return self._in.get(chapter, [])

    def chain(self, chapter: str, max_hops: int = 3) -> list[list[Edge]]:
        """Follow exclusions out of a chapter, so a round trip becomes visible.

        Chapter 39 excludes textiles of Section XI; Section XI excludes
        plastic-coated fabric into chapter 39. Which of the two governs is a
        legal question, but the system cannot even ask it without seeing both.
        """
        paths: list[list[Edge]] = []

        def walk(current: str, path: list[Edge], seen: set[str]) -> None:
            if len(path) >= max_hops:
                return
            for edge in self.excluded_from(current):
                for target in edge.target.chapters:
                    if target in seen:
                        paths.append([*path, edge])  # closed the loop
                        continue
                    walk(target, [*path, edge], seen | {target})

        walk(chapter, [], {chapter})
        return paths

    def to_dict(self) -> dict:
        return {"edges": [e.to_dict() for e in self.edges]}

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")
        return path


def build_exclusion_graph(notes: list[Note]) -> ExclusionGraph:
    """Every binding exclusion in the schedule, as edges between chapters."""
    edges: list[Edge] = []
    for note in notes:
        if not note.is_binding or not is_exclusion(note):
            continue
        source_kind = note.scope
        source = note.chapter if note.chapter else (note.section or "")
        for target in references_in(note.text):
            if source_kind == "chapter" and target.chapters == [note.chapter]:
                continue  # a note pointing inside its own chapter redirects nothing
            edges.append(Edge(source_kind, source, target, note.citation, note.text))
    return ExclusionGraph(edges)
