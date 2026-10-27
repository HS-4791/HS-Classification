"""Score the engine's reasoning against what the rulings say they applied."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.config import get_settings  # noqa: E402
from hsclass.data.atlas import load_split  # noqa: E402
from hsclass.eval.explanation import authorities_in, score_explanations  # noqa: E402
from hsclass.pipeline.adjudicated import AdjudicatedClassifier  # noqa: E402
from hsclass.pipeline.gri import GRIEngine  # noqa: E402
from hsclass.pipeline.traversal import HierarchicalClassifier, LevelIndex  # noqa: E402
from hsclass.retrieval.embedder import get_embedder  # noqa: E402
from hsclass.retrieval.index import VectorIndex  # noqa: E402
from hsclass.schedule.exclusions import build_exclusion_graph  # noqa: E402
from hsclass.schedule.notes import load_notes  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 1500


def main() -> int:
    settings = get_settings()
    revision = settings.require_revision()

    examples, _ = load_split(ROOT / "data" / "raw" / "atlas" / "train.jsonl")
    # Only rulings that name a rule beyond GRI 1 can discriminate anything.
    scorable = [e for e in examples if authorities_in(e.reasoning).beyond_gri_1][:LIMIT]
    print(f"{len(scorable)} discriminating rulings to score", flush=True)

    levels = LevelIndex(VectorIndex.load(settings.processed_dir / "index-levels"))
    embedder = get_embedder(settings.embedding_backend, settings.embedding_model)
    notes = load_notes(settings.processed_dir / "notes.json")
    texts = {c.code: c.text for c in levels.index.candidates}
    classifier = AdjudicatedClassifier(
        HierarchicalClassifier(levels, embedder, k=5),
        GRIEngine(build_exclusion_graph(notes)),
        texts,
    )

    cases = []
    for i, example in enumerate(scorable):
        decision, _ = classifier.decide(example.description)
        cases.append((decision, example.code, example.reasoning))
        if (i + 1) % 250 == 0:
            print(f"  scored {i + 1}/{len(scorable)}", flush=True)

    score = score_explanations(cases)
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    (settings.reports_dir / "explanation-quality.json").write_text(
        json.dumps({"revision": revision} | score.to_dict(), indent=2), encoding="utf-8"
    )
    (settings.reports_dir / "explanation-quality.md").write_text(
        score.to_markdown(), encoding="utf-8"
    )
    print()
    print(score.to_markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
