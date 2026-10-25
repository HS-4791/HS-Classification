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


@needs_pdf
def test_sibling_provisions_do_not_nest_on_sub_pixel_drift():
    """Two provisions at the same depth do not share an exact x position.

    On chapter 4's notes page "(a)" starts at 62.6397 and "(b)" at 62.6400.
    Compared exactly, "(b)" never closes "(a)" and nests inside it, so Note
    3(b) is filed as 3(a)(b) and Note 5 becomes a child of Note 4 called "45".
    """
    notes = {n.number: n for n in extract_notes(FULL_REVISION, pages=range(970, 972))
             if n.kind == "note"}
    assert {"1", "2", "3", "3(a)", "3(b)", "4", "4(a)", "4(b)", "4(c)"} <= set(notes)
    assert notes["3(b)"].parent == "3"
    assert notes["4"].parent is None
    assert not any(num.startswith("45") for num in notes)


@needs_pdf
def test_every_parent_reference_resolves():
    """A provision whose own text is empty still has to be emitted.

    Section XI Note 2 says nothing itself - 2(A) and 2(B) carry all of it -
    but dropping it leaves those children naming a parent that is not in the
    file, and the note tree cannot be walked.
    """
    notes = extract_notes(FULL_REVISION, pages=SECTION_XI)
    present = {(n.kind, n.section, n.chapter, n.number) for n in notes}
    dangling = [
        n.citation for n in notes
        if n.parent and (n.kind, n.section, n.chapter, n.parent) not in present
    ]
    assert dangling == []


@needs_pdf
def test_a_sub_provision_keeps_its_parents_kind():
    """A "Notes (con.)" header between 23(b) and 23(c) must not reclassify it."""
    notes = extract_notes(FULL_REVISION, pages=SECTION_XI)
    # Section XI has a Note 1, a Subheading Note 1 and an Additional U.S.
    # Note 1, so a sibling group is only a group within one kind.
    children: dict[tuple, list] = {}
    for note in notes:
        if note.parent:
            children.setdefault((note.section, note.chapter, note.parent), []).append(note)
    for (_, _, parent), group in children.items():
        parents = [n for n in notes if n.number == parent]
        for child in group:
            assert any(p.kind == child.kind for p in parents), (
                f"{child.citation} has no parent of its own kind"
            )


@needs_pdf
def test_a_heading_number_used_as_a_label_is_not_a_provision_number():
    """Chapter 84 prints "8479. Heading 8479 also covers ..." as a paragraph.

    Read as a provision marker it becomes a child of note 8 and is cited as
    "Chapter 84 Note 88479" - not something a ruling could reference.
    """
    numbers = {n.number for n in extract_notes(FULL_REVISION, pages=range(2731, 2733))}
    assert not any(num.startswith("88479") for num in numbers)
