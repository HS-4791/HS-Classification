"""Extract every Section and Chapter note from the schedule PDF."""

from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.config import get_settings  # noqa: E402
from hsclass.schedule.exclusions import build_exclusion_graph  # noqa: E402
from hsclass.schedule.notes import extract_notes, save_notes  # noqa: E402


def main() -> int:
    settings = get_settings()
    notes = extract_notes(settings.raw_dir / "full-revision.pdf")
    path = save_notes(notes, settings.processed_dir / "notes.json")

    kinds = collections.Counter(n.kind for n in notes)
    scopes = collections.Counter(n.scope for n in notes)
    print(f"{len(notes)} provisions -> {path}")
    print(f"  kinds   : {dict(kinds)}")
    print(f"  scopes  : {dict(scopes)}")
    print(f"  sections: {len({n.section for n in notes if n.section})}")
    print(f"  chapters: {len({n.chapter for n in notes if n.chapter})}")
    print(f"  binding : {sum(1 for n in notes if n.is_binding)}")

    missing = [n.citation for n in notes if not n.section and not n.chapter]
    print(f"  no scope: {len(missing)}")

    graph = build_exclusion_graph(notes)
    graph.save(settings.processed_dir / "exclusions.json")
    loops = [p for p in graph.chain("61", max_hops=2) if "61" in p[-1].target.chapters]
    print()
    print(f"{len(graph)} exclusion edges across {len(graph.chapters)} chapters")
    reached = {c for e in graph.excluded_from("61") for c in e.target.chapters}
    print(f"  chapter 61 sends goods to {len(reached)} chapters")
    print(f"  two-hop round trips back into chapter 61: {len(loops)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
