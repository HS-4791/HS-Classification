import copy

from hsclass.schedule.models import build_from_export
from hsclass.schedule.validate import validate_tree

from .conftest import EXPORT_ROWS


def test_clean_tree_passes(tree):
    report = validate_tree(tree)
    assert report.passed
    # Section is not in the digits, so the export alone cannot supply it.
    assert any(i.check == "sections_unresolved" for i in report.warnings)


def test_counts_must_reconcile_against_the_manifest(tree):
    manifest = {"expected_tree": {"total_rows": 999, "chapters_present": 1}}
    report = validate_tree(tree, manifest)
    assert not report.passed
    assert any(i.check == "count_reconcile" for i in report.errors)


def test_dropped_children_are_caught():
    """Losing a grouping line's children must fail loudly, not silently."""
    rows = [r for r in copy.deepcopy(EXPORT_ROWS) if r["htsno"] not in
            ("6109.10.00.04", "6109.10.00.12")]
    report = validate_tree(build_from_export(rows, "TESTREV"))
    assert not report.passed
    assert any(i.check == "grouping_leaf" for i in report.errors)


def test_prefix_break_is_an_error_outside_the_special_chapters():
    rows = copy.deepcopy(EXPORT_ROWS)
    rows[4]["htsno"] = "6109.90.10.04"  # a 6109.90 line nested under 6109.10.00
    report = validate_tree(build_from_export(rows, "TESTREV"))
    assert any(i.check == "prefix_break" for i in report.errors)


def test_level_jump_is_only_a_warning_when_the_prefix_vouches_for_it():
    """The published indent is not contiguous everywhere.

    Statistical suffixes sometimes sit two or three levels below their tariff
    line with nothing in between. The code prefix still vouches for the
    parentage, so that is a quirk to record, not corruption to fail on.
    """
    rows = copy.deepcopy(EXPORT_ROWS)
    rows[4]["indent"] = "5"  # jumps 2 -> 5, but 6109.10.00.04 still fits 6109.10.00
    report = validate_tree(build_from_export(rows, "TESTREV"))
    assert report.passed
    assert any(i.check == "level_jump" and i.severity == "warn" for i in report.issues)
