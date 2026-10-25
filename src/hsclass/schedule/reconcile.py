"""Check the PDF parse against the published export, row by row.

The export is not the source of truth for the project - the PDF is, because
most jurisdictions publish nothing else. But the export carries the same lines
with an explicit ``indent``, which makes it an exact oracle: every row the
parser produces should appear in it, at the same depth, with the same text.

Anything that does not line up is a parser bug with a page number attached.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from hsclass.schedule.parse_pdf import ParsedRow

WHITESPACE_RE = re.compile(r"\s+")
#: Both sides space punctuation inconsistently - the PDF because a font change
#: splits a word, the export for no visible reason ("Goldfish( Carassius",
#: "Other :") - so spacing around punctuation is ignored when matching.
SPACE_BEFORE_PUNCTUATION_RE = re.compile(r"\s+([,.;:)])")
SPACE_AFTER_OPEN_PAREN_RE = re.compile(r"\(\s+")
#: The PDF renders some punctuation as characters the export spells plainly.
TRANSLATIONS = str.maketrans({"’": "'", "‘": "'", "“": '"',
                              "”": '"', "–": "-", "—": "-",
                              "�": "'"})

FIELDS = ("indent", "description", "units", "general", "special", "other")


def normalise(text: str) -> str:
    text = WHITESPACE_RE.sub(" ", (text or "").translate(TRANSLATIONS))
    text = SPACE_BEFORE_PUNCTUATION_RE.sub(r"", text)
    return SPACE_AFTER_OPEN_PAREN_RE.sub("(", text).strip()


def normalise_rate(text: str) -> str:
    """Rates wrap mid-list in the PDF ("Free (AU,BH,CL," / "CO,IL,JO,KR,").

    Where the line broke is a typesetting artefact, so whitespace is dropped
    entirely before comparing.
    """
    return WHITESPACE_RE.sub("", (text or "").translate(TRANSLATIONS))


def _units(units: list[str]) -> str:
    """Compare units as printed.

    This used to strip periods, which made the comparison blind to the single
    largest defect in the parse: 2,296 rows where "No." was read as "No" and
    "doz." as "doz", because pdfplumber returns the unit glued to its dot
    leader. A check that deletes the characters it is meant to compare buys
    confidence it has not earned.
    """
    return ",".join(normalise(u) for u in units)


def row_key(code: str, description: str) -> tuple[str, str]:
    """Match a row to its counterpart on code plus text.

    A trailing period is ignored *here only*, because this decides which rows
    are the same row - and an abbreviation at the very end of a description
    ("Ictalurus spp.") is printed with its dot leaders glued on, so the
    period cannot be recovered. Alignment must survive that. Whether the text
    then matches is judged separately, by `_compare_fields`.
    """
    return code, normalise(description).casefold().rstrip(".")


@dataclass
class Mismatch:
    kind: str  # "missing" | "extra" | "field"
    code: str
    field: str = ""
    pdf: str = ""
    export: str = ""
    page: int = 0


@dataclass
class ReconcileReport:
    chapter: str
    pdf_rows: int
    export_rows: int
    matched: int = 0
    #: Rows that aligned *and* whose every compared field agreed.
    field_perfect: int = 0
    mismatches: list[Mismatch] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.mismatches

    @property
    def alignment(self) -> float:
        """How many rows were found at all. Not a correctness figure.

        This was published as "row_accuracy" and reported 99.84% while the
        same report counted 2,164 wrong general rates, 1,666 wrong Column 2
        rates and 649 rows that do not exist in the schedule. A row counts
        here if its code and description line up, whatever its contents.
        """
        return self.matched / self.export_rows if self.export_rows else 0.0

    @property
    def accuracy(self) -> float:
        """Rows where every compared field agrees. The honest headline."""
        return self.field_perfect / self.export_rows if self.export_rows else 0.0

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for m in self.mismatches:
            key = m.kind if m.kind != "field" else f"field:{m.field}"
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items()))

    def to_dict(self) -> dict:
        return {
            "chapter": self.chapter,
            "pdf_rows": self.pdf_rows,
            "export_rows": self.export_rows,
            "aligned": self.matched,
            "field_perfect": self.field_perfect,
            # The headline. "alignment" is reported beside it so the gap
            # between "we found the row" and "the row is right" is visible
            # rather than collapsed into one flattering number.
            "accuracy": round(self.accuracy, 4),
            "alignment": round(self.alignment, 4),
            "passed": self.passed,
            "counts": self.counts(),
            "mismatches": [asdict(m) for m in self.mismatches[:200]],
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path


def export_rows_for_chapter(rows: list[dict], chapter: str) -> list[dict]:
    """Select one chapter's rows, carrying uncoded lines with the chapter above."""
    out, current = [], None
    for row in rows:
        code = (row.get("htsno") or "").strip()
        if code:
            current = code[:2]
        if current == chapter:
            out.append(row)
    return out


def _compare_fields(parsed: ParsedRow, expected: dict) -> list[Mismatch]:
    code = parsed.full_code or "(grouping)"
    found: list[Mismatch] = []
    pairs = {
        "indent": (str(parsed.indent), str(int(str(expected.get("indent") or 0)))),
        # "doz." prints with its dot leader glued on, so the abbreviating
        # period cannot be told from the leader. It carries no legal meaning.
        "units": (_units(parsed.units), _units(expected.get("units") or [])),
        "general": (normalise_rate(parsed.general), normalise_rate(expected.get("general") or "")),
        "special": (normalise_rate(parsed.special), normalise_rate(expected.get("special") or "")),
        "other": (normalise_rate(parsed.other), normalise_rate(expected.get("other") or "")),
    }
    for name, (got, want) in pairs.items():
        if got != want:
            found.append(Mismatch("field", code, name, got, want, parsed.page))
    return found


def reconcile(parsed: list[ParsedRow], expected: list[dict], chapter: str) -> ReconcileReport:
    report = ReconcileReport(chapter=chapter, pdf_rows=len(parsed), export_rows=len(expected))

    left = [row_key(p.full_code, p.description) for p in parsed]
    right = [row_key((e.get("htsno") or "").strip(), e.get("description") or "") for e in expected]

    for tag, i1, i2, j1, j2 in SequenceMatcher(a=left, b=right, autojunk=False).get_opcodes():
        if tag == "equal":
            for p, e in zip(parsed[i1:i2], expected[j1:j2], strict=True):
                wrong = _compare_fields(p, e)
                report.mismatches.extend(wrong)
                if not wrong:
                    report.field_perfect += 1
            report.matched += i2 - i1
            continue
        for e in expected[j1:j2]:
            report.mismatches.append(
                Mismatch("missing", (e.get("htsno") or "").strip() or "(grouping)",
                         export=normalise(e.get("description") or "")[:90])
            )
        for p in parsed[i1:i2]:
            report.mismatches.append(
                Mismatch("extra", p.full_code or "(grouping)",
                         pdf=normalise(p.description)[:90], page=p.page)
            )
    return report
