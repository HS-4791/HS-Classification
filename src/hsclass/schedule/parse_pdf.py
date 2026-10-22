"""Parse the HTS schedule PDF into tariff rows.

The PDF has no markup for hierarchy. A line's depth is its horizontal position
on the page and nothing else, so the parser reads x-coordinates and converts
them back into levels.

Measurements taken from the published layout, each pinned by a test:

* the description column starts at x=110.7 and each level indents by 14.4pt
* the other columns sit at fixed x ranges (see ``COLUMNS``)
* word spacing only survives at ``x_tolerance=1.0``; at 1.5 pdfplumber glues
  words together ("T-shirts,allwhite,shorthemmed") with no error
* a line is a group of words sharing a *baseline*, not a top - an italic
  scientific name rides about 1.8pt higher than the roman text beside it
* a superscript is identified by font size (5.1pt against 8.5pt), never by
  how high it sits: the raised 2 in "g/m2" is nearer the line above it than
  its own, so distance puts it on the wrong row

A description often wraps over several visual lines. A row stays open until
its text terminates - dot leaders, a trailing colon, or a rate on the line -
which is also what separates a wrapped line from the next grouping line,
since neither carries a code.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

#: Word spacing is only recovered at this tolerance. Do not raise it.
X_TOLERANCE = 1.0

#: Column boundaries as (name, x_start, x_end).
COLUMNS = (
    ("code", 0.0, 90.0),
    ("suffix", 90.0, 108.0),
    ("description", 108.0, 330.0),
    ("units", 330.0, 375.0),
    ("general", 375.0, 440.0),
    ("special", 440.0, 515.0),
    ("other", 515.0, 1000.0),
)

DESCRIPTION_X0 = 110.7
DESCRIPTION_STEP = 14.4

#: Everything above this is page furniture and column headings.
BODY_TOP = 92.0

#: Words on one visual line share a baseline but can differ by a couple of
#: points in `top`. Grouping by `top` splits such a row in two and scrambles
#: its text; grouping by `bottom` keeps it whole.
BASELINE_TOLERANCE = 2.5

#: A superscript is set at about 60% of the body size.
SUPERSCRIPT_SIZE_RATIO = 0.8

#: Below this horizontal gap two tokens were one word before a font change.
TIGHT_GAP = 1.5

CODE_RE = re.compile(r"^\d{4}(\.\d{2}){0,3}$")
SUFFIX_RE = re.compile(r"^\d{2}$")
CHAPTER_PAGE_RE = re.compile(r"^(\d{2,4})-\d+$")
DOT_LEADER_RE = re.compile(r"\.{2,}")
#: A reference to a footnote ("1/"), not part of the article description.
FOOTNOTE_RE = re.compile(r"^\d+/$")

#: The statistical-suffix column heading, which appears on every table page
#: and nowhere else. "Subheading" is no good as a marker: the Section and
#: Chapter notes are headed "Subheading Notes", and those pages also quote
#: heading numbers and years ("1977") that read as codes in the left column.
TABLE_HEADING = "Suf-"

#: Section is the one level that appears nowhere in a code's digits, yet
#: Section notes are binding - Section XI Note 2 is what decides a 60/40
#: cotton-polyester shirt. The schedule prints the Section in the page
#: furniture, inconsistently; the chapter ranges below are its definition.
#: Sections I-XXI are the international HS; XXII is the US-only chapters.
SECTION_CHAPTERS: dict[str, range] = {
    "I": range(1, 6), "II": range(6, 15), "III": range(15, 16),
    "IV": range(16, 25), "V": range(25, 28), "VI": range(28, 39),
    "VII": range(39, 41), "VIII": range(41, 44), "IX": range(44, 47),
    "X": range(47, 50), "XI": range(50, 64), "XII": range(64, 68),
    "XIII": range(68, 71), "XIV": range(71, 72), "XV": range(72, 84),
    "XVI": range(84, 86), "XVII": range(86, 90), "XVIII": range(90, 93),
    "XIX": range(93, 94), "XX": range(94, 97), "XXI": range(97, 98),
    "XXII": range(98, 100),
}
SECTIONS = set(SECTION_CHAPTERS)
SECTION_OF_CHAPTER = {
    f"{chapter:02d}": section
    for section, chapters in SECTION_CHAPTERS.items()
    for chapter in chapters
}


@dataclass
class ParsedRow:
    """One tariff line, in the same shape as the published export."""

    htsno: str = ""
    suffix: str = ""
    indent: int = 0
    description: str = ""
    units: list[str] = field(default_factory=list)
    general: str = ""
    special: str = ""
    other: str = ""
    section: str | None = None
    chapter: str | None = None
    #: What the page furniture claimed, kept only to cross-check the above.
    page_section: str | None = None
    page_chapter: str | None = None
    page: int = 0
    open: bool = field(default=True, repr=False)
    con: bool = field(default=False, repr=False)
    #: Closed only because the text ended in a colon, with no rate and no dot
    #: leaders to confirm it. Heading 8201 reads "Handtools of the following
    #: kinds and base metal parts thereof: spades, shovels, ..." - the colon
    #: is mid-sentence, and the rest runs onto the next line at the same depth.
    colon_only: bool = field(default=False, repr=False)

    @property
    def full_code(self) -> str:
        """The code as the schedule prints it, suffix included."""
        return f"{self.htsno}.{self.suffix}" if self.htsno and self.suffix else self.htsno

    def add_rates(self, units: list[str], general: str, special: str, other: str) -> None:
        """Accumulate a cell that wraps onto further lines."""
        self.units += units
        self.general = f"{self.general} {general}".strip()
        self.special = f"{self.special} {special}".strip()
        self.other = f"{self.other} {other}".strip()

    def to_export_row(self) -> dict:
        return {
            "htsno": self.full_code,
            "indent": str(self.indent),
            "description": self.description,
            "superior": "true" if not self.full_code else None,
            "units": self.units,
            "general": self.general,
            "special": self.special,
            "other": self.other,
        }


def _without_controls(text: str) -> str:
    """Drop glyphs whose font maps them to control characters, not Unicode."""
    return "".join(ch for ch in text if ch >= " ")


def _clean(text: str) -> str:
    """Drop the dot leaders and any unmappable glyphs.

    Only runs of two or more dots go: a unit of quantity is printed "doz."
    and the trailing period is part of it. Spacing around punctuation is left
    exactly as typeset - the published export is itself inconsistent there
    ("Goldfish( Carassius", "Other :"), so it is normalised when comparing
    rather than rewritten here.
    """
    return _without_controls(DOT_LEADER_RE.sub("", text)).strip()


def _body_size(words: list[dict]) -> float:
    sizes = Counter(round(w.get("size", 0.0), 1) for w in words)
    return max(sizes, key=lambda s: (sizes[s], s)) if sizes else 0.0


def _columns_of(words: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {name: [] for name, _, _ in COLUMNS}
    for word in words:
        for name, start, end in COLUMNS:
            if start <= word["x0"] < end:
                out[name].append(word)
                break
    return out


#: How far above or below its line a small glyph may sit.
SMALL_GLYPH_WINDOW = 12.0


def _attach_small(word: dict, lines: list[tuple[float, list[dict]]]) -> None:
    """Put a superscript or subscript on the line it belongs to.

    Vertical distance cannot decide this. The raised 2 in "g/m2" is nearer
    the line *above* than its own; the lowered 2 in a chemical formula is
    nearer the line *below*. What is reliable is that a small glyph sits
    immediately to the right of the character it modifies, so the line with
    the closest word ending just before it wins.
    """
    nearby = [line for line in lines if abs(line[0] - word["bottom"]) <= SMALL_GLYPH_WINDOW]
    best: tuple[float, float] | None = None
    target: tuple[float, list[dict]] | None = None
    for line in nearby:
        ends = [w["x1"] for w in line[1] if w["x1"] <= word["x0"] + 0.5]
        if not ends:
            continue
        rank = (word["x0"] - max(ends), abs(line[0] - word["bottom"]))
        if best is None or rank < best:
            best, target = rank, line
    if target is None:
        target = min(
            nearby or lines, key=lambda line: abs(line[0] - word["bottom"]), default=None
        )
    if target is not None:
        target[1].append(word)


def _group_lines(words: list[dict]) -> list[list[dict]]:
    """Cluster words into visual lines by baseline, then place small glyphs.

    Size identifies a superscript or subscript; `_attach_small` decides which
    line it belongs to, which vertical distance alone gets wrong.
    """
    cutoff = _body_size(words) * SUPERSCRIPT_SIZE_RATIO
    ordinary = [w for w in words if w.get("size", 0.0) >= cutoff]
    raised = [w for w in words if w.get("size", 0.0) < cutoff]

    lines: list[tuple[float, list[dict]]] = []
    for word in sorted(ordinary, key=lambda w: (w["bottom"], w["x0"])):
        if lines and abs(word["bottom"] - lines[-1][0]) <= BASELINE_TOLERANCE:
            lines[-1][1].append(word)
        else:
            lines.append((word["bottom"], [word]))

    for word in raised:
        _attach_small(word, lines)

    # Left to right within the line: depth is read off the leftmost word, and
    # a baseline cluster holds words from two slightly different `bottom`s.
    return [sorted(group, key=lambda w: w["x0"]) for _, group in lines]


def _text_of(words: list[dict], cutoff: float = 0.0) -> str:
    """Read a line left to right, re-attaching superscripts.

    "300 g/m2" is typeset with a raised 2. Left on its own it reads as a
    separate token, or lands in the wrong place in the sentence.

    A superscript is identified by its size, never by how high it sits: an
    italic scientific name also rides about 1.8pt above the roman text beside
    it, and gluing those together destroys the spaces inside a species list.
    """
    if not words:
        return ""
    ordered = sorted(words, key=lambda w: w["x0"])
    cutoff = cutoff or _body_size(ordered) * SUPERSCRIPT_SIZE_RATIO
    parts: list[str] = []
    previous_x1: float | None = None
    previous_super = False
    for word in ordered:
        superscript = word.get("size", 0.0) < cutoff
        if superscript and FOOTNOTE_RE.match(word["text"]):
            # The published export leaves footnote markers out as well.
            previous_x1, previous_super = word["x1"], False
            continue
        # Whatever follows a superscript with no gap closed the same word:
        # the colon in "g/m2:". The test stays narrow because some lines are
        # set tightly enough that a real space is under a point wide.
        closes_superscript = (
            previous_super and previous_x1 is not None and word["x0"] - previous_x1 < TIGHT_GAP
        )
        if parts and (superscript or closes_superscript):
            parts[-1] += word["text"]
        else:
            parts.append(word["text"])
        previous_x1, previous_super = word["x1"], superscript
    return " ".join(parts)


def indent_of(x0: float) -> int:
    """Convert a description's x position back into a hierarchy level."""
    return max(0, round((x0 - DESCRIPTION_X0) / DESCRIPTION_STEP))


def _terminated(description: str, had_rate: bool, raw: str) -> bool:
    """Has this row's description finished, or does it wrap onto the next line?

    "(con.)" closes a repeated header: it is printed at the end of the restated
    text, and the next line is a genuine row again.
    """
    return (
        had_rate
        or description.endswith(":")
        or description.endswith("(con.)")
        or bool(DOT_LEADER_RE.search(raw))
    )


def _closed_on_a_bare_colon(description: str, had_rate: bool, raw: str) -> bool:
    """Did only a trailing colon end this row, with nothing else confirming it?"""
    return (
        description.endswith(":")
        and not had_rate
        and not DOT_LEADER_RE.search(raw)
        and not description.endswith("(con.)")
    )


def parse_page(page, section: str | None = None) -> tuple[list[ParsedRow], str | None, str | None]:
    """Parse one page. Returns its rows plus the Section and Chapter it declares."""
    words = page.extract_words(x_tolerance=X_TOLERANCE, extra_attrs=["size"])
    body: list[dict] = []
    chapter = None
    section_on_this_page = None

    for word in words:
        if word["top"] < BODY_TOP:
            # The furniture prints the Section and a chapter-page marker such
            # as "61-49", but not on every page and not always on the right.
            # It is read as a cross-check only; `resolve_scope` is what the
            # rows actually get.
            if word["x0"] > 500:
                if word["text"] in SECTIONS:
                    section = section_on_this_page = word["text"]
                elif m := CHAPTER_PAGE_RE.match(word["text"]):
                    chapter = m.group(1).zfill(2)[:2]
            continue
        body.append(word)

    rows: list[ParsedRow] = []
    current: ParsedRow | None = None
    base_code = ""  # the coded line that the statistical suffixes below it extend

    for line_words in _group_lines(body):
        cols = _columns_of(line_words)
        cutoff = _body_size(line_words) * SUPERSCRIPT_SIZE_RATIO
        code = _text_of(cols["code"], cutoff).strip()
        suffix = _text_of(cols["suffix"], cutoff).strip()
        raw_description = _text_of(cols["description"], cutoff)
        description = _clean(raw_description)
        # One unit per line, not per word: "doz. prs" (dozen pairs) is a
        # single unit of quantity that happens to be two tokens.
        unit_line = _clean(_text_of(cols["units"], cutoff))
        units = [unit_line] if unit_line else []
        general = _clean(_text_of(cols["general"], cutoff))
        special = _clean(_text_of(cols["special"], cutoff))
        other = _clean(_text_of(cols["other"], cutoff))
        had_rate = bool(general or special or other)

        # A repeated context header restates rows from the previous page. It
        # wraps like any other row, so it is parsed normally and dropped at
        # the end - that way its continuation lines go with it and the genuine
        # row printed directly underneath is not swallowed too.
        is_con = "(con.)" in raw_description or "(con.)" in code

        # The marker is also printed on a line of its own, after the restated
        # row has already closed on its colon. It refers to the row above.
        if not suffix and (description == "(con.)" or code == "(con.)"):
            if current is not None:
                current.con = True
                # Close it too, or the genuine row printed underneath would
                # be read as more of the restated text and dropped with it.
                current.open = False
            continue

        starts_row = bool(CODE_RE.match(code) or SUFFIX_RE.match(suffix) or description)
        line_indent = indent_of(cols["description"][0]["x0"]) if cols["description"] else -1
        # A row closed on a bare colon reopens for an uncoded line at its own
        # depth: a genuine grouping line sits one level deeper than its parent.
        resumes = (
            current is not None
            and current.colon_only
            and not code
            and not suffix
            and line_indent == current.indent
        )
        continues_row = (
            current is not None and (current.open or resumes) and not code and not suffix
        )

        if continues_row and description:
            current.description = f"{current.description} {description}".strip()
            current.add_rates(units, general, special, other)
            # The marker often lands on the wrapped line rather than the first,
            # so a row only turns out to be a repeat part-way through.
            current.con = current.con or is_con
            if _terminated(current.description, had_rate, raw_description):
                current.open = False
                current.colon_only = _closed_on_a_bare_colon(
                    current.description, had_rate, raw_description
                )
            else:
                current.open, current.colon_only = True, False
            continue

        if current is not None and not code and not suffix and not description:
            # A line holding only units or a wrapped rate belongs to the row
            # above. A Special rate routinely wraps over four lines.
            current.add_rates(units, general, special, other)
            current.con = current.con or is_con
            continue

        if not starts_row:
            continue

        if CODE_RE.match(code):
            base_code = code

        current = ParsedRow(
            # A statistical suffix prints alone in its column; the code it
            # extends is the coded line above it.
            htsno=code if CODE_RE.match(code) else (base_code if SUFFIX_RE.match(suffix) else ""),
            suffix=suffix if SUFFIX_RE.match(suffix) else "",
            indent=line_indent if line_indent >= 0 else 0,
            description=description,
            units=units,
            general=general,
            special=special,
            other=other,
            page_section=section_on_this_page,
            page_chapter=chapter,
            page=page.page_number,
            con=is_con,
        )
        current.open = not _terminated(description, had_rate, raw_description)
        current.colon_only = _closed_on_a_bare_colon(description, had_rate, raw_description)
        rows.append(current)

    rows = [r for r in rows if not r.con]
    for row in rows:
        # Only what this page actually printed, so a stale value carried from
        # an earlier page is never mistaken for a disagreement.
        row.page_section = section_on_this_page
        row.page_chapter = chapter
    return rows, section, chapter


def is_table_page(page) -> bool:
    """A table page carries the column headings and at least one tariff code."""
    has_headings = has_code = False
    for word in page.extract_words(x_tolerance=X_TOLERANCE):
        if word["top"] < BODY_TOP and word["text"] == TABLE_HEADING:
            has_headings = True
        elif word["top"] >= BODY_TOP and word["x0"] < 90 and CODE_RE.match(word["text"]):
            has_code = True
        if has_headings and has_code:
            return True
    return False


def parse_pdf(path: Path, pages: range | None = None) -> list[ParsedRow]:
    """Parse every table page of a schedule PDF into rows."""
    rows: list[ParsedRow] = []
    section: str | None = None
    with pdfplumber.open(str(path)) as pdf:
        selected = pdf.pages if pages is None else [pdf.pages[i] for i in pages]
        for page in selected:
            try:
                if not is_table_page(page):
                    continue
                page_rows, section, _ = parse_page(page, section)
                rows.extend(page_rows)
            finally:
                # pdfplumber caches every page it touches; over 4,500 pages
                # that is the difference between 300MB and several GB.
                page.flush_cache()
                page.get_textmap.cache_clear()
    resolve_scope(rows)
    return rows


def resolve_scope(rows: list[ParsedRow]) -> list[str]:
    """Assign each row its Chapter and Section, and report furniture disagreements.

    Chapter comes from the digits, carried forward across the uncoded grouping
    lines in between - the same rule the published export implies. Section
    follows from the chapter. Neither depends on the page furniture, which is
    printed inconsistently and is missing altogether on some pages; where the
    furniture does state a Section, it is compared and any disagreement is
    returned rather than silently preferred.
    """
    disagreements: list[str] = []
    chapter: str | None = None
    for row in rows:
        code = "".join(ch for ch in row.htsno if ch.isdigit())
        if code:
            chapter = code[:2]
        row.chapter = chapter
        row.section = SECTION_OF_CHAPTER.get(chapter or "")
        if row.page_section and row.section and row.page_section != row.section:
            disagreements.append(
                f"page {row.page}: furniture says Section {row.page_section}, "
                f"chapter {row.chapter} is in Section {row.section}"
            )
    return disagreements
