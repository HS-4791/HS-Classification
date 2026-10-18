"""Download a pinned HTS release and verify it against the committed manifest.

The release parameter on the PDF endpoint is honoured; the export endpoint's
is not confirmed, so the checksum in the manifest is the real pin. If USITC
publishes a new revision and the export silently moves with it, --verify fails
here rather than letting a Rev N tree be validated against a Rev N+1 export.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILE_ENDPOINT = "https://hts.usitc.gov/reststop/file"
EXPORT_ENDPOINT = "https://hts.usitc.gov/reststop/exportList"

DEFAULT_FILES = {
    "full-revision.pdf": "finalCopy",
    "general-rules-of-interpretation.pdf": "General Rules of Interpretation",
    "chapter-61.pdf": "Chapter 61",
    "change-record.pdf": "Change Record",
    "preface.pdf": "Preface",
}


def source_url(revision: str, filename: str) -> str:
    query = urllib.parse.urlencode({"release": revision, "filename": filename})
    return f"{FILE_ENDPOINT}?{query}"


def export_url() -> str:
    query = urllib.parse.urlencode(
        {"from": "0101", "to": "9999", "format": "JSON", "styles": "false"}
    )
    return f"{EXPORT_ENDPOINT}?{query}"


def download(url: str, dest: Path) -> bytes:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=300) as response:  # noqa: S310
        payload = response.read()
    dest.write_bytes(payload)
    return payload


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--verify", action="store_true",
                        help="fail if a checksum differs from the manifest")
    parser.add_argument("--force", action="store_true",
                        help="re-download files already present")
    args = parser.parse_args()

    raw = ROOT / "data" / "raw" / args.revision
    manifest_path = ROOT / "data" / "manifests" / f"{args.revision}.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else None
    )
    if args.verify and manifest is None:
        print(f"no manifest at {manifest_path}; cannot verify", file=sys.stderr)
        return 2

    targets = {name: source_url(args.revision, f) for name, f in DEFAULT_FILES.items()}
    targets["hts-export.json"] = export_url()

    failures = []
    for name, url in targets.items():
        dest = raw / name
        if dest.exists() and not args.force:
            payload = dest.read_bytes()
            action = "have"
        else:
            payload = download(url, dest)
            action = "got "
        digest = sha256(payload)
        expected = (manifest or {}).get("files", {}).get(name, {}).get("sha256")
        if expected and digest != expected:
            failures.append(name)
            status = "CHECKSUM MISMATCH"
        else:
            status = "ok" if expected else "unpinned"
        print(f"{action} {name:38} {len(payload)/1048576:6.2f} MB  {status}")

    if failures:
        print(
            f"\n{len(failures)} file(s) differ from {args.revision}: {', '.join(failures)}.\n"
            "USITC has most likely published a newer revision. Pin the new one "
            "and re-baseline rather than mixing versions.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
