"""The PDF parser's measurements, and one end-to-end page.

The unit tests pin the layout constants, because every row in the schedule
depends on them and a wrong one corrupts the tree silently rather than
raising. The integration test runs the real page the running example lives on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hsclass.schedule.parse_pdf import (
    SECTION_OF_CHAPTER,
    ParsedRow,
    _clean,
    _terminated,
    indent_of,
    parse_pdf,
    resolve_scope,
)

CHAPTER_61 = Path("data/raw/2026HTSRev21/chapter-61.pdf")
FULL_REVISION = Path("data/raw/2026HTSRev21/full-revision.pdf")
needs_pdf = pytest.mark.skipif(not CHAPTER_61.exists(), reason="run scripts/fetch_schedule.py")
needs_full_pdf = pytest.mark.skipif(
    not FULL_REVISION.exists(), reason="run scripts/fetch_schedule.py"
)


@pytest.mark.parametrize(
    ("x0", "level"),
    [(110.7, 0), (125.1, 1), (139.5, 2), (153.9, 3), (168.3, 4), (182.7, 5)],
)
def test_the_indent_ladder(x0, level):
    """Depth is the x position and nothing else - these are the rungs."""
    assert indent_of(x0) == level


def test_indent_never_goes_negative():
    assert indent_of(40.0) == 0


def test_dot_leaders_go_but_an_abbreviating_period_stays():
    assert _clean("Of cotton.........................") == "Of cotton"
    assert _clean("..................") == ""
    # "doz." is a unit of quantity, not a truncated leader.
    assert _clean("doz.") == "doz."


def test_what_ends_a_row():
    assert _terminated("Of other textile materials:", False, "Of other textile materials:")
    assert _terminated("Of cotton", False, "Of cotton......")
    assert _terminated("Of wool", True, "Of wool")
    # A restated header closes on its marker.
    assert _terminated("knitted or crocheted: (con.)", False, "knitted or crocheted: (con.)")
    # An unterminated line wraps onto the next one.
    assert not _terminated("T-shirts, singlets, tank tops and", False, "T-shirts, singlets")


@needs_pdf
def test_the_t_shirt_page_parses_exactly():
    rows = parse_pdf(CHAPTER_61, pages=range(48, 49))
    by_code = {r.full_code: r for r in rows if r.full_code}

    heading = by_code["6109"]
    assert heading.indent == 0
    assert heading.description.startswith("T-shirts, singlets, tank tops")
    assert heading.description.endswith("knitted or crocheted:")  # wrapped over two lines

    # The duty attaches at eight digits.
    cotton = by_code["6109.10.00"]
    assert (cotton.indent, cotton.general, cotton.other) == (1, "16.5%", "90%")
    assert cotton.special.startswith("Free (AU,")

    # The statistical suffix inherits the code above it and carries no rate.
    suffix = by_code["6109.10.00.04"]
    assert suffix.indent == 3
    assert suffix.general == ""
    assert suffix.units == ["doz.", "kg"]

    # Section is printed in the page furniture, never in the digits.
    assert heading.section == "XI"
    assert heading.chapter == "61"


@needs_pdf
def test_grouping_lines_survive_the_parse():
    rows = parse_pdf(CHAPTER_61, pages=range(48, 49))
    grouping = [r.description for r in rows if not r.full_code]
    assert "Men's or boys':" in grouping
    assert "Other T-shirts:" in grouping


@needs_pdf
def test_notes_pages_are_not_parsed_as_tables():
    """Chapter 61 opens with its Notes, which quote heading numbers freely."""
    assert parse_pdf(CHAPTER_61, pages=range(0, 3)) == []


def test_every_chapter_belongs_to_exactly_one_section():
    assert len(SECTION_OF_CHAPTER) == 99
    assert SECTION_OF_CHAPTER["61"] == "XI"   # textiles - the running example
    assert SECTION_OF_CHAPTER["29"] == "VI"   # organic chemicals
    assert SECTION_OF_CHAPTER["99"] == "XXII"  # US special provisions


def test_scope_is_carried_across_uncoded_lines():
    """Chapter comes from the digits, not the page furniture."""
    rows = [
        ParsedRow(htsno="6109", description="T-shirts:"),
        ParsedRow(description="Men's or boys':"),          # no code of its own
        ParsedRow(htsno="6109.10.00", suffix="04", description="All white"),
    ]
    resolve_scope(rows)
    assert [r.chapter for r in rows] == ["61", "61", "61"]
    assert {r.section for r in rows} == {"XI"}


def test_furniture_disagreement_is_reported_not_obeyed():
    row = ParsedRow(htsno="6109", description="T-shirts:", page_section="VI")
    disagreements = resolve_scope([row])
    assert row.section == "XI"  # the digits win
    assert len(disagreements) == 1 and "Section VI" in disagreements[0]


@needs_full_pdf
def test_superscripts_rejoin_the_word_they_belong_to():
    """A raised 2 is nearer the line above than its own - size identifies it.

    Left alone it becomes its own row, or splices itself into the middle of a
    neighbouring sentence; either way the description stops matching.
    """
    rows = parse_pdf(FULL_REVISION, pages=range(1919, 1922))
    by_code = {r.full_code: r for r in rows if r.full_code}

    assert by_code["5111.11"].description == "Of a weight not exceeding 300 g/m2:"
    assert by_code["5111.19.60.20"].description == "Weighing not more than 400 g/m2 (410)"
    # The unit of quantity is square metres, not a stray digit beside a metre.
    assert by_code["5111.11.30.00"].units == ["m2", "kg"]
    assert not any(r.description == "2" for r in rows)
