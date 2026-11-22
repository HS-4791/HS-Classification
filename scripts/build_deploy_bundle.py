"""Build the smallest data bundle a live service needs.

The full processed directory is 177 MB, most of it a 44 MB array of BGE
embeddings over the schedule. **The deployed path needs none of them.**
Precedent retrieval is BM25 over ruling descriptions, which builds in seconds
from text, so the service carries no vectors, downloads no model and starts
cold in about a second.

What it does need, and nothing else:

* ``precedents.json`` - 18,253 ruling descriptions and the code an officer
  gave each one. 2.0 MB. This is the candidate source.
* ``provisions.json`` - every provision's code and inherited chain, so the
  rule engine can read the terms of a candidate and the answer can be shown
  with its description. 11 MB.
* ``notes.json`` - the parsed note provisions, for the exclusion graph. 2.8 MB.

About 16 MB in all. The bundle is a **build artefact and is not committed**:
``data/`` stays gitignored and reproducible from scripts, which is the rule
this project keeps. Build it, then build the image.

Usage: ``python -u scripts/build_deploy_bundle.py``
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.config import get_settings  # noqa: E402
from hsclass.data.atlas import load_split  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ATLAS = ROOT / "data" / "raw" / "atlas"


def say(line: str) -> None:
    sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()


def digits(code: str) -> str:
    return "".join(c for c in code if c.isdigit())


def main() -> int:
    settings = get_settings()
    revision = settings.require_revision()
    processed = settings.processed_dir
    out = ROOT / "data" / "deploy"
    out.mkdir(parents=True, exist_ok=True)

    say(f"revision {revision}")

    # The rulings the service retrieves over. Only the description and the
    # code: the reasoning text is for the judgement oracle, not for serving.
    train, _ = load_split(ATLAS / "train.jsonl")
    precedents = [
        {"d": e.description, "c": digits(e.code)} for e in train if digits(e.code)
    ]
    (out / "precedents.json").write_text(
        json.dumps(precedents, separators=(",", ":")), encoding="utf-8"
    )
    say(f"  precedents.json   {len(precedents):>6} rulings")

    # Provision code and inherited chain, lifted out of the vector index's
    # metadata so the bundle does not carry the vectors beside it.
    meta = json.loads(
        (processed / "index-levels" / "candidates.json").read_text(encoding="utf-8")
    )
    provisions = [
        {"c": c["code"], "t": c["text"]} for c in meta["candidates"] if c.get("code")
    ]
    (out / "provisions.json").write_text(
        json.dumps(provisions, separators=(",", ":")), encoding="utf-8"
    )
    say(f"  provisions.json   {len(provisions):>6} provisions")

    shutil.copyfile(processed / "notes.json", out / "notes.json")
    notes = json.loads((out / "notes.json").read_text(encoding="utf-8"))
    say(f"  notes.json        {len(notes):>6} note provisions")

    # Stamped so a running service can report which schedule it answers
    # against, and so an image built against one revision cannot quietly
    # serve another.
    (out / "bundle.json").write_text(
        json.dumps(
            {
                "revision": revision,
                "precedents": len(precedents),
                "provisions": len(provisions),
                "notes": len(notes),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    total = sum(f.stat().st_size for f in out.iterdir())
    say(f"\n{out.relative_to(ROOT)}: {total / 1e6:.1f} MB, no vectors, no model")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
