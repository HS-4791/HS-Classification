"""Extract the Section and Chapter notes - the part that is actually binding.

Section and Chapter *titles* have no legal force; GRI 1 says so outright. The
notes are the law, and they do two jobs: they **exclude** goods into some
other chapter, and they **define** what counts as what. A heading can look
like a perfect match and still be wrong because a note three pages earlier
removed those goods from the chapter entirely.

Layout, measured and asserted in the tests:

* a note page is any page the tariff parser rejects as a table
* its scope is printed as a marker - "XI-1" for Section XI, "61-2" for
  chapter 61 - and continuation pages inherit the last scope seen
* provisions hang on an indent ladder: numbered notes start at x=46.8,
  lettered sub-provisions at 62.6, nested ones at 78.5, and every provision's
  wrapped lines are indented about 18pt further than its own first line

Chunking follows the provision, never a token count: Note 2(A) is one unit
and Note 2(B) is another, because splitting on length separates a condition
from the qualifier that governs it. Each provision also carries its scope and
its parent's lead-in, since "This Chapter does not cover:" means nothing on
its own - which chapter?
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pdfplumber

from hsclass.schedule.parse_pdf import (
    X_TOLERANCE,
    _group_lines,
    is_table_page,
)

#: "Notes", "Subheading Notes", "Additional U.S. Notes", "Statistical Notes".
HEADING_RE = re.compile(
    r"^(Subheading Notes?|Additional U\.?\s?S\.? Notes?|Statistical Notes?|Notes?)"
    r"(\s*\(con\.\))?\s*$",
    re.I,
)

#: A notes page's running head is shorter than a table page's, so a fixed
#: y-threshold either keeps the running head or eats the first provision -
#: Section XI Note 2(B) opens directly under a "Notes (con.)" header. The
#: head is recognised by what it says instead.
RUNNING_HEAD_RE = re.compile(
    r"^(Harmonized Tariff Schedule of the United States"
    r"|Annotated for Statistical Reporting Purposes)",
    re.I,
)
MARKER_ONLY_RE = re.compile(r"^(?:[IVXL]+|\d{1,4})-\d+$")
KINDS = {
    "note": "note",
    "notes": "note",
    "subheading note": "subheading_note",
    "subheading notes": "subheading_note",
    "additional u.s. note": "additional_us_note",
    "additional u.s. notes": "additional_us_note",
    "additional us note": "additional_us_note",
    "additional us notes": "additional_us_note",
    "statistical note": "statistical_note",
    "statistical notes": "statistical_note",
}

#: A provision opens with "1." or "(a)" or "(A)" or "(ii)".
#:
#: The numeric form is capped at three digits because the notes also use a
#: bare heading number as a paragraph label - chapter 84 prints "8479.
#: Heading 8479 also covers machines for making rope or cable ...". Reading
#: that as a provision number makes it a child of note 8, cited as "Note
#: 88479", which is not a provision anyone could look up. No note in the
#: schedule is numbered above 999.
MARKER_RE = re.compile(r"^(?:(\d{1,3})\.|\(([A-Za-z]+)\))\s+")

#: One rung of the provision ladder: numbered notes start at x=46.8, lettered
#: sub-provisions at 62.6, nested ones at 78.5.
#:
#: A marker peeled from the middle of a line ("2. (A) Goods classifiable...")
#: prints wherever the preceding token happens to end - about x=58 - which is
#: to the *left* of where its own siblings print on later lines. Recording
#: that position would stop "(B)" from closing "(A)", and Note 2(B) would be
#: filed as Note 2(A)(B). Inline markers are given their ladder rung instead.
LADDER_STEP = 15.85

#: Two provisions at the same depth do not share an exact x. On chapter 4's
#: notes page "(a)" starts at 62.6397 and its sibling "(b)" at 62.6400 - three
#: ten-thousandths of a point further right. Compared exactly, "(b)" never
#: closes "(a)" and nests under it instead, so Note 3(b) is filed as 3(a)(b)
#: and Note 5 becomes a child of Note 4 numbered "45". A rung is 15.85pt wide,
#: so a tolerance well under that separates drift from a real indent.
INDENT_TOLERANCE = 1.0

SECTION_MARKER_RE = re.compile(r"^([IVXL]+)-\d+$")
CHAPTER_MARKER_RE = re.compile(r"^(\d{1,4})-\d+$")

#: Statistical notes are US reporting rules, not classification law. They are
#: kept but flagged, so the rule engine can be told to ignore them.
BINDING_KINDS = {"note", "subheading_note", "additional_us_note"}


@dataclass
class Note:
    """One provision, with everything needed to cite it."""

    kind: str
    number: str
    text: str
    section: str | None = None
    chapter: str | None = None
    parent: str | None = None
    lead_in: str = ""
    page: int = 0

    @property
    def scope(self) -> str:
        return "chapter" if self.chapter else "section"

    @property
    def is_binding(self) -> bool:
        return self.kind in BINDING_KINDS

    @property
    def citation(self) -> str:
        """How a ruling would refer to this provision."""
        where = f"Chapter {int(self.chapter)}" if self.chapter else f"Section {self.section}"
        label = {
            "note": "Note",
            "subheading_note": "Subheading Note",
            "additional_us_note": "Additional U.S. Note",
            "statistical_note": "Statistical Note",
        }[self.kind]
        return f"{where} {label} {self.number}"

    @property
    def embeddable(self) -> str:
        """The provision with the context that makes it mean anything.

        "This Chapter does not cover:" is ambiguous alone - which chapter, and
        does the exclusion even apply to the goods in hand? The citation and
        the parent's lead-in answer both.
        """
        parts = [self.citation]
        if self.lead_in and self.lead_in != self.text:
            parts.append(self.lead_in)
        parts.append(self.text)
        return " — ".join(parts)

    def to_dict(self) -> dict:
        return asdict(self) | {"citation": self.citation, "scope": self.scope}


@dataclass
class _Open:
    """A provision still collecting its wrapped lines."""

    note: Note
    x0: float
    lines: list[str] = field(default_factory=list)
    #: Note 2's own text is empty - everything it says is in 2(A) and 2(B).
    #: It still has to be emitted, or those children name a parent that is
    #: not in the file and the note tree cannot be walked.
    has_children: bool = False


def _scope_of(words: list[dict]) -> tuple[str | None, str | None]:
    """Read "XI-1" or "61-2" off the page, wherever it is printed.

    On a chapter's opening page the marker sits in the title block rather
    than the furniture, so the whole page is searched.
    """
    section = chapter = None
    for word in words:
        if m := SECTION_MARKER_RE.match(word["text"]):
            section = section or m.group(1)
        elif m := CHAPTER_MARKER_RE.match(word["text"]):
            chapter = chapter or m.group(1).zfill(2)[:2]
    return section, chapter


class NotesParser:
    """Reads notes pages in sequence, keeping state across page breaks.

    A provision routinely runs over a page boundary - Section XI Note 2(A)
    starts on one page and 2(B) opens on the next - so the heading kind, the
    open provision stack and the scope all have to survive the break. Parsing
    each page independently silently loses every provision that wraps.
    """

    def __init__(self) -> None:
        self.notes: list[Note] = []
        self.section: str | None = None
        self.chapter: str | None = None
        self.kind: str | None = None
        self._stack: list[_Open] = []

    def _close(self, down_to: float) -> None:
        while self._stack and self._stack[-1].x0 >= down_to - INDENT_TOLERANCE:
            done = self._stack.pop()
            done.note.text = " ".join(done.lines).strip()
            if done.note.text or done.has_children:
                self.notes.append(done.note)

    def _set_scope(self, words: list[dict]) -> None:
        section, chapter = _scope_of(words)
        if not section and not chapter:
            return  # a continuation page states neither; keep what we have
        if (section or self.section) != self.section or chapter != self.chapter:
            # New Section or Chapter: nothing still open belongs to it.
            self._close(0.0)
            self.kind = None
        if section:
            self.section = section
        # A Section page carries no chapter, so a stale one must not leak on.
        self.chapter = chapter

    def feed(self, page) -> None:
        words = page.extract_words(x_tolerance=X_TOLERANCE, extra_attrs=["size"])
        self._set_scope(words)

        for line in _group_lines(words):
            text = " ".join(w["text"] for w in line).strip()
            if not text or RUNNING_HEAD_RE.match(text) or MARKER_ONLY_RE.match(text):
                continue

            if heading := HEADING_RE.match(text):
                # "Notes (con.)" resumes the same list across a page break -
                # closing the stack there would orphan every provision still
                # collecting its wrapped lines.
                if not heading.group(2):
                    self._close(0.0)
                self.kind = KINDS.get(heading.group(1).lower().replace("  ", " "))
                continue
            if self.kind is None:
                continue  # title block, running heads, anything before a heading

            # A provision can open more than one level on a single line:
            # Section XI Note 2 is printed "2. (A) Goods classifiable in
            # chapters 50 to 55 ...", so 2 and 2(A) both begin here. Peel the
            # markers in turn, taking each one's x from the word carrying it.
            remaining = sorted(line, key=lambda w: w["x0"])
            opened = False
            depth: float | None = None
            while remaining:
                marker = MARKER_RE.match(" ".join(w["text"] for w in remaining).strip())
                if not marker:
                    break
                depth = remaining[0]["x0"] if depth is None else depth + LADDER_STEP
                self._close(depth)
                label = marker.group(1) or f"({marker.group(2)})"
                parent = self._stack[-1].note if self._stack else None
                lead_in = ""
                if parent:
                    lead_in = parent.lead_in or " ".join(self._stack[-1].lines).strip()
                    self._stack[-1].has_children = True
                self._stack.append(_Open(
                    Note(
                        # A sub-provision is the same kind as its parent. A
                        # "Notes (con.)" header appearing between 23(b) and
                        # 23(c) must not turn one sibling into a Note while
                        # the others stay Additional U.S. Notes.
                        kind=parent.kind if parent else self.kind,
                        number=f"{parent.number}{label}" if parent else label,
                        text="",
                        section=self.section,
                        chapter=self.chapter,
                        parent=parent.number if parent else None,
                        lead_in=lead_in,
                        page=page.page_number,
                    ),
                    depth,
                ))
                remaining, opened = remaining[1:], True

            tail = " ".join(w["text"] for w in remaining).strip()
            if self._stack and tail:
                self._stack[-1].lines.append(tail)
            elif not opened and self._stack:
                self._stack[-1].lines.append(text)

    def finish(self) -> list[Note]:
        self._close(0.0)
        return self.notes


def extract_notes(path: Path | str, pages: range | None = None) -> list[Note]:
    """Every note in the schedule, in document order."""
    parser = NotesParser()
    with pdfplumber.open(str(path)) as pdf:
        selected = pdf.pages if pages is None else [pdf.pages[i] for i in pages]
        for page in selected:
            try:
                if is_table_page(page):
                    continue
                parser.feed(page)
            finally:
                page.flush_cache()
                page.get_textmap.cache_clear()
    return parser.finish()


def save_notes(notes: list[Note], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([n.to_dict() for n in notes], indent=1), encoding="utf-8")
    return path


def load_notes(path: Path) -> list[Note]:
    """Read the extracted provisions back, ignoring fields Note does not carry.

    The filtering comprehension this replaces was copied into four call
    sites - the CLI and three measurement scripts - each reconstructing the
    same object from the same file. One of them drifting would mean the
    engine being measured was not the engine being run.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        Note(**{k: v for k, v in entry.items() if k in Note.__dataclass_fields__})
        for entry in raw
    ]
