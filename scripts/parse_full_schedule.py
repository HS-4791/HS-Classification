"""Parse the whole schedule PDF and reconcile every chapter against the export."""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.schedule.parse_pdf import parse_pdf, resolve_scope  # noqa: E402
from hsclass.schedule.reconcile import export_rows_for_chapter, reconcile  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REVISION = "2026HTSRev21"
RAW = ROOT / "data" / "raw" / REVISION


def main() -> int:
    t0 = time.time()
    rows = parse_pdf(RAW / "full-revision.pdf")
    print(f"parsed {len(rows)} rows in {time.time() - t0:.0f}s", flush=True)

    disagreements = resolve_scope(rows)
    print(f"section furniture disagreements: {len(disagreements)}", flush=True)
    for line in disagreements[:5]:
        print("   ", line)

    out = ROOT / "data" / "processed" / REVISION
    out.mkdir(parents=True, exist_ok=True)
    (out / "pdf-rows.json").write_text(
        json.dumps([r.to_export_row() | {"page": r.page, "section": r.section,
                                         "chapter": r.chapter} for r in rows]),
        encoding="utf-8",
    )

    export = json.loads((RAW / "hts-export.json").read_text(encoding="utf-8"))
    by_chapter: dict[str, list] = defaultdict(list)
    for row in rows:
        if row.chapter:
            by_chapter[row.chapter].append(row)

    summary, total_matched, total_expected = {}, 0, 0
    for chapter in sorted(by_chapter):
        expected = export_rows_for_chapter(export, chapter)
        if not expected:
            continue
        report = reconcile(by_chapter[chapter], expected, chapter)
        summary[chapter] = report.to_dict()["counts"] | {
            "accuracy": round(report.accuracy, 4),
            "alignment": round(report.alignment, 4),
            "pdf_rows": report.pdf_rows,
            "export_rows": report.export_rows,
        }
        total_matched += report.field_perfect
        total_expected += report.export_rows

    (ROOT / "reports" / "pdf-reconciliation.json").write_text(
        json.dumps({"revision": REVISION, "total_matched": total_matched,
                    "total_export_rows": total_expected,
                    "accuracy": round(total_matched / total_expected, 4),
                    "by_chapter": summary}, indent=2),
        encoding="utf-8",
    )
    print(f"overall field-perfect: {total_matched}/{total_expected} = "
          f"{total_matched/total_expected:.2%}")
    worst = sorted(summary.items(), key=lambda kv: kv[1]["accuracy"])[:12]
    for chapter, s in worst:
        print(f"  ch {chapter}: {s['accuracy']:.2%}  {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
