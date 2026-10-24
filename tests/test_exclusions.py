"""The exclusion graph - where a note sends goods, and whether they come back."""

from __future__ import annotations

import pytest

from hsclass.schedule.exclusions import (
    Reference,
    build_exclusion_graph,
    is_exclusion,
    references_in,
)
from hsclass.schedule.notes import Note


def ref(level: str, code: str) -> Reference:
    return Reference(level, code)


def test_a_single_chapter_reference():
    assert references_in("...laminated with plastics, of chapter 39;") == [ref("chapter", "39")]


def test_a_range_names_every_chapter_in_it():
    found = references_in("Goods classifiable in chapters 50 to 55")
    assert found == [ref("chapter", str(n)) for n in range(50, 56)]


def test_a_list_of_headings():
    assert references_in("(heading 0501, 6703 or 6704)") == [
        ref("heading", "0501"), ref("heading", "6703"), ref("heading", "6704")
    ]


def test_a_heading_range():
    assert references_in("Sensitized textiles of headings 3701 to 3704;") == [
        ref("heading", str(n)) for n in (3701, 3702, 3703, 3704)
    ]


def test_a_subheading_keeps_six_digits():
    assert references_in("Goods provided for in subheading 0403.90.16") == [
        ref("subheading", "040390")
    ]


def test_a_section_reference_reaches_all_its_chapters():
    """Chapter 39 Note 12(p) sends goods to "section XI", not to a chapter."""
    found = references_in("Goods of section XI (textiles and textile articles);")
    assert found == [ref("section", "XI")]
    assert found[0].chapters == [f"{n:02d}" for n in range(50, 64)]


def test_an_exclusion_is_told_from_a_definition():
    excluding = Note(kind="note", number="1", text="x", lead_in="This chapter does not cover:")
    defining = Note(kind="note", number="2", text="The expression 'plastics' means ...")
    assert is_exclusion(excluding)
    assert not is_exclusion(defining)


@pytest.fixture
def graph():
    """The round trip the domain primer names, in miniature."""
    return build_exclusion_graph([
        Note(kind="note", number="1(h)", section="XI",
             lead_in="This section does not cover:",
             text="Woven fabrics laminated with plastics, of chapter 39;"),
        Note(kind="note", number="12(p)", section="VII", chapter="39",
             lead_in="This chapter does not cover:",
             text="Goods of section XI (textiles and textile articles);"),
        Note(kind="statistical_note", number="1", chapter="61",
             lead_in="This chapter does not cover:", text="Reporting rules of chapter 98."),
    ])


def test_a_section_note_binds_every_chapter_in_the_section(graph):
    """Section XI Note 1(h) is as much a rule of chapter 61 as of chapter 50."""
    assert any("39" in e.target.chapters for e in graph.excluded_from("61"))
    assert any("39" in e.target.chapters for e in graph.excluded_from("50"))


def test_statistical_notes_do_not_create_edges(graph):
    """They are US reporting rules, not classification law."""
    assert not any(e.citation.startswith("Chapter 61") for e in graph.edges)


def test_the_round_trip_is_visible(graph):
    """39 sends textiles back to Section XI, which is where they came from.

    Which of the two governs is a legal question; the system cannot even ask
    it without seeing both hops.
    """
    loops = graph.chain("61", max_hops=2)
    assert any(
        len(path) == 2
        and "39" in path[0].target.chapters
        and "61" in path[1].target.chapters
        for path in loops
    )


def test_a_note_pointing_inside_its_own_chapter_is_not_an_edge():
    graph = build_exclusion_graph([
        Note(kind="note", number="1", chapter="61",
             lead_in="This chapter does not cover:", text="Goods of chapter 61."),
    ])
    assert len(graph) == 0
