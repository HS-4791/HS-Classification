"""The reconciler, and the difference between finding a row and it being right."""

from __future__ import annotations

from hsclass.schedule.parse_pdf import ParsedRow
from hsclass.schedule.reconcile import reconcile


def export_row(code: str, description: str, **fields) -> dict:
    return {"htsno": code, "description": description, "indent": "1",
            "units": [], "general": "", "special": "", "other": ""} | fields


def parsed_row(code: str, description: str, **fields) -> ParsedRow:
    return ParsedRow(htsno=code, description=description, indent=1, **fields)


def test_a_row_that_aligns_but_disagrees_is_not_counted_as_correct():
    """The headline used to count alignment and reported 99.84% while the
    same report held 2,164 wrong general rates."""
    report = reconcile(
        [parsed_row("6109.10.00", "Of cotton", general="99%")],
        [export_row("6109.10.00", "Of cotton", general="16.5%")],
        "61",
    )
    assert report.alignment == 1.0      # the row was found
    assert report.accuracy == 0.0       # and it is wrong
    assert report.field_perfect == 0


def test_a_row_that_agrees_everywhere_counts():
    report = reconcile(
        [parsed_row("6109.10.00", "Of cotton", general="16.5%")],
        [export_row("6109.10.00", "Of cotton", general="16.5%")],
        "61",
    )
    assert report.accuracy == 1.0 and report.field_perfect == 1


def test_a_lost_abbreviating_period_is_now_visible():
    """2,296 rows read "No." as "No". The comparison used to delete the
    period from both sides, so the single largest defect was invisible."""
    report = reconcile(
        [parsed_row("0105.11.00", "Chickens", units=["No"])],
        [export_row("0105.11.00", "Chickens", units=["No."])],
        "01",
    )
    assert report.accuracy == 0.0
    assert any(m.field == "units" for m in report.mismatches)


def test_alignment_still_tolerates_a_trailing_period_in_the_description():
    """Deciding *which* row this is has to survive "Ictalurus spp." losing
    its period to the dot leaders; whether the text matches is judged after."""
    report = reconcile(
        [parsed_row("0302.72.50", "Ictalurus spp")],
        [export_row("0302.72.50", "Ictalurus spp.")],
        "03",
    )
    assert report.alignment == 1.0
