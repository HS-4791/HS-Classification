"""Download the ATLAS ruling splits and report what they contain.

The spec names a path that 404s, so the dataset id lives in one place -
``hsclass.data.atlas.DATASET_ID`` - and is recorded in the manifest written
here alongside the row counts actually received.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.data.atlas import BASE_URL, DATASET_ID, LICENCE, SPLITS, load_split  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "data" / "raw" / "atlas"


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    summary: dict[str, dict] = {}

    for split in SPLITS:
        path = DEST / f"{split}.jsonl"
        if not path.exists():
            url = f"{BASE_URL}/{split}.jsonl"
            print(f"downloading {split} ...", flush=True)
            with urllib.request.urlopen(url, timeout=300) as response:  # noqa: S310
                path.write_bytes(response.read())

        examples, dropped = load_split(path)
        lengths = Counter(len(e.code) for e in examples)
        summary[split] = {
            "rows": len(examples) + dropped,
            "usable": len(examples),
            "dropped_no_code": dropped,
            "distinct_codes": len({e.code for e in examples}),
            "distinct_codes_at_6": len({e.code[:6] for e in examples}),
            "with_reasoning": sum(1 for e in examples if e.reasoning),
            "code_digit_lengths": dict(sorted(lengths.items())),
        }
        print(f"{split:11} {summary[split]}")

    manifest = ROOT / "data" / "manifests" / "atlas.json"
    manifest.write_text(
        json.dumps({"dataset": DATASET_ID, "licence": LICENCE, "splits": summary}, indent=2),
        encoding="utf-8",
    )
    print(f"\nwrote {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
