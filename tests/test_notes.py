"""Extracting the notes - the part of the schedule that is actually binding."""

from __future__ import annotations

from pathlib import Path

import pytest

from hsclass.schedule.notes import Note, extract_notes

FULL_REVISION = Path("data/raw/2026HTSRev21/full-revision.pdf")
needs_pdf = pytest.mark.skipif(not FULL_REVISION.exists(), reason="run scripts/fetch_schedule.py")

#: Section XI's notes, which decide the running example.
SECTION_XI = range(1899, 1908)


@pytest.fixture(scope="module")
def section_xi() -> dict[str, Note]:
    notes = extract_notes(FULL_REVISION, pages=SECTION_XI)
    return {f"{n.kind}:{n.number}": n for n in notes}


def test_a_note_cites_itself_the_way_a_ruling_would():
    note = Note(kind="note", number="2(A)", text="...", section="XI")
    assert note.citation == "Section XI Note 2(A)"
    chapter_note = Note(kind="note", number="5", text="...", section="XI", chapter="61")
    assert chapter_note.citation == "Chapter 61 Note 5"
    us_note = Note(kind="additional_us_note", number="1", text="...", chapter="61")
    assert us_note.citation == "Chapter 61 Additional U.S. Note 1"


def test_statistical_notes_are_not_binding_law():
    assert Note(kind="note", number="1", text="x").is_binding
    assert Note(kind="subheading_note", number="1", text="x").is_binding
    assert not Note(kind="statistical_note", number="1", text="x").is_binding


def test_a_provision_carries_the_context_that_makes_it_mean_anything():
    """"This section does not cover:" says nothing on its own."""
    note = Note(
        kind="note", number="1(h)", section="XI",
        lead_in="This section does not cover:",
        text="Woven fabrics ... laminated with plastics, of chapter 39;",
    )
    assert note.embeddable.startswith("Section XI Note 1(h)")
    assert "This section does not cover:" in note.embeddable
    assert "chapter 39" in note.embeddable


@needs_pdf
def test_the_exclusion_that_sends_coated_fabric_to_chapter_39(section_xi):
    """Section XI Note 1(h) - a plastic-coated fabric leaves textiles entirely.

    No amount of similarity on the word "fabric" finds this. Only the note does.
    """
    note = section_xi["note:1(h)"]
    assert note.section == "XI" and note.chapter is None
    assert "impregnated, coated, covered or laminated with plastics" in note.text
    assert "chapter 39" in note.text
    assert note.lead_in == "This section does not cover:"


@needs_pdf
def test_the_weight_test_that_decides_the_t_shirt(section_xi):
    """Section XI Note 2(A): the rule that makes 60/40 cotton classify as cotton."""
    note = section_xi["note:2(A)"]
    assert "predominates by weight" in note.text
    assert "chapters 50 to 55" in note.text
    # The tie-break when nothing predominates is part of the same provision.
    assert "occurs last in numerical order" in note.text


@needs_pdf
def test_sub_provisions_are_separate_units_under_their_parent(section_xi):
    """2(A) and 2(B) are different units; 2(B)(a) hangs under 2(B).

    A marker peeled inline ("2. (A) Goods ...") prints to the left of where
    its own siblings print, so getting this wrong files 2(B) as 2(A)(B).
    """
    assert section_xi["note:2(B)"].text.startswith("For the purposes of the above rule")
    assert section_xi["note:2(B)"].parent == "2"
    assert section_xi["note:2(B)(a)"].parent == "2(B)"
    assert section_xi["note:2(C)"].parent == "2"


@needs_pdf
def test_a_provision_survives_the_page_break_it_sits_across(section_xi):
    """2(A) ends on one page and 2(B) opens on the next, under "Notes (con.)".

    Parsing pages independently loses every provision that wraps.
    """
    assert section_xi["note:2(A)"].page != section_xi["note:2(B)"].page


@needs_pdf
def test_the_four_kinds_are_told_apart(section_xi):
    kinds = {key.split(":")[0] for key in section_xi}
    assert {"note", "subheading_note", "additional_us_note", "statistical_note"} <= kinds
