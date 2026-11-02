"""CAG: what prompt caching would be worth here, which is nothing, measured.

Week 3's gate asks for a CAG latency and cost delta with accuracy confirmed
unchanged. The delta is zero, and not because the saving is small. **The
instrument has no subject.** Cache-augmented generation keeps a large static
prefix - the General Rules and the chapter notes - resident in a model's
context so repeated classifications do not re-send it. In this architecture
those texts are never sent to a model at all: stage 3 is deterministic and
reads the notes from a parsed structure, so the corpus CAG exists to cache
never enters a prompt.

The check stated before measuring:

1. **What a CAG design would cache** - the size of the GRI and notes corpus a
   prompt-based engine would have to carry. If that number is large, it says
   what this architecture avoids rather than what it should adopt.
2. **What this system actually sends** - measured input tokens per call for
   the two stages that do call a model, stage 1 and stage 4.
3. **What caching actually returned** - ``cache_read_tokens`` is already
   recorded on every call by ``obs/cost.py``, so this is a measurement and
   not an estimate.
4. **Accuracy unchanged** - trivially, since nothing is being switched on.

Token counts for the corpus are an estimate and say so: ``tiktoken`` is not a
dependency here (the project runs ONNX, not the OpenAI stack), so the corpus
is reported in characters with a chars/4 token estimate beside it. The numbers
that matter - what was sent and what was cached - come from the API's own
usage field and are exact.

Usage: ``python -u scripts/measure_caching.py``. Sends nothing; costs nothing.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.config import get_settings  # noqa: E402
from hsclass.pipeline.explain import INSTRUCTIONS as EXPLAIN_INSTRUCTIONS  # noqa: E402
from hsclass.pipeline.extract import INSTRUCTIONS as EXTRACT_INSTRUCTIONS  # noqa: E402
from hsclass.schedule.notes import load_notes  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

#: OpenAI caches an exact prompt prefix only once it reaches this length, and
#: only in 128-token increments above it. A prompt below it cannot be cached
#: at all, however often it repeats.
MIN_CACHEABLE_PREFIX_TOKENS = 1024

#: Characters per token, for English prose. An estimate, labelled as one.
CHARS_PER_TOKEN = 4


def say(line: str) -> None:
    sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()


def sent_tokens(report: str) -> list[int]:
    """Input tokens per call, from the API's own usage field."""
    path = ROOT / "reports" / f"{report}.json"
    if not path.exists():
        return []
    cost = json.loads(path.read_text(encoding="utf-8")).get("cost") or {}
    return [call["input_tokens"] for call in cost.get("calls", [])]


def cached_tokens(report: str) -> list[int]:
    path = ROOT / "reports" / f"{report}.json"
    if not path.exists():
        return []
    cost = json.loads(path.read_text(encoding="utf-8")).get("cost") or {}
    return [call["cache_read_tokens"] for call in cost.get("calls", [])]


def main() -> int:
    settings = get_settings()
    revision = settings.require_revision()
    notes = load_notes(settings.processed_dir / "notes.json")

    binding = [n for n in notes if n.is_binding]
    corpus_chars = sum(len(n.embeddable) for n in binding)
    corpus_tokens = corpus_chars // CHARS_PER_TOKEN

    stages = {
        "stage 1 (extract)": ("extraction-test-200", EXTRACT_INSTRUCTIONS),
        "stage 4 (explain)": ("explanations-test-200", EXPLAIN_INSTRUCTIONS),
    }

    say(f"revision {revision}")
    say("\n1. what a CAG design would keep resident:")
    say(f"   binding note provisions           {len(binding):>8}")
    say(f"   characters                        {corpus_chars:>8}")
    say(f"   estimated tokens (chars/4)        {corpus_tokens:>8}   <- an estimate")
    say("   plus the General Rules themselves, which this project never puts")
    say("   in a prompt and therefore never parsed into a promptable form.")

    say("\n2. what this system actually sends, per call (exact, from the API):")
    rows = {}
    for label, (report, instructions) in stages.items():
        sent = sent_tokens(report)
        cached = cached_tokens(report)
        if not sent:
            say(f"   {label:<20} no report on disk; run its measurement first")
            continue
        rows[label] = {
            "calls": len(sent),
            "mean_input_tokens": round(statistics.mean(sent), 1),
            "max_input_tokens": max(sent),
            "instructions_chars": len(instructions),
            "cache_read_tokens": sum(cached),
            "below_cacheable_minimum": max(sent) < MIN_CACHEABLE_PREFIX_TOKENS,
        }
        say(f"   {label:<20} {len(sent):>4} calls   "
            f"mean {statistics.mean(sent):>6.1f}   max {max(sent):>4} input tokens")

    say("\n3. what caching actually returned:")
    total_cached = sum(r["cache_read_tokens"] for r in rows.values())
    total_calls = sum(r["calls"] for r in rows.values())
    say(f"   cache_read_tokens over {total_calls} real calls   {total_cached}")
    say(f"   caching minimum prefix                 {MIN_CACHEABLE_PREFIX_TOKENS} tokens")
    if rows and all(r["below_cacheable_minimum"] for r in rows.values()):
        say("   every prompt is below that minimum, so no prefix is eligible")

    say("\n4. the delta:")
    say("   cost      0.0000 USD per 1,000 classifications")
    say("   latency   0.0 ms")
    say("   accuracy  unchanged; nothing was switched on")
    say("   The notes are read by a deterministic stage, so the corpus above")
    say("   is never in a context window for a cache to hold.")

    report = {
        "revision": revision,
        "verdict": "not applicable: the cacheable corpus never enters a prompt",
        "cacheable_corpus": {
            "binding_note_provisions": len(binding),
            "characters": corpus_chars,
            "estimated_tokens": corpus_tokens,
            "estimate_basis": f"characters / {CHARS_PER_TOKEN}, tiktoken not a dependency",
        },
        "what_is_sent": rows,
        "caching_minimum_prefix_tokens": MIN_CACHEABLE_PREFIX_TOKENS,
        "measured_cache_read_tokens": total_cached,
        "delta": {
            "cost_usd_per_1000": 0.0,
            "latency_ms": 0.0,
            "accuracy": "unchanged",
        },
    }
    out = ROOT / "reports" / "caching.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    say(f"\nwrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
