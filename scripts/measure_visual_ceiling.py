"""Could an image fix these errors? The ceiling, before any image is sourced.

Product-image input has to be **constructed** for this project: ATLAS is text,
CROSS letters describe submitted photographs without embedding them, and
HSCodeComp - the one expert-annotated alternative, 632 entries - has no image
field (checked: its columns are product_name, product_attributes, price,
currency_code, cate_lv1..5_desc and hs_code). Constructing a slice costs about
three days, so the question worth answering first is how much an image could
possibly buy.

**The check, stated before building, and corrected once.** For every case the
pipeline gets wrong, find the first segment where the gold provision's chain
diverges from the top-1 candidate's, and ask what the system would have needed
to know there.

The first version asked only "is that fact visible?" and buckets built for
textiles left 71.9% of a multi-chapter corpus unclassified. Worse, it was the
wrong question. For "cast carbon steel fittings made to ASTM A216" the
divergent segment is "Cast fittings" - which the description already says. An
image adds nothing there; the failure is retrieval, not missing information.
So the partition is three-way:

1. **already in the description** - an image cannot help, because the
   discriminating words are in the text and the system still missed them.
2. **absent and visible** - the article itself, or how it is made (knitted,
   woven, lace, pile, coated). This is the only bucket an image can serve.
3. **absent and not visible** - composition by weight, end use ("imported as
   parts of playsuits"), quota restraints, statistical-note references. No
   photograph shows that a garment is 60 percent cotton, and Section XI Note
   2(A) - which decides 23 of the engine's 24 held-out answers - needs exactly
   that.

Section and chapter titles are dropped before comparing, because GRI 1 says
they have no legal force; leaving them in made a cross-chapter error report
its divergence as "Chapter 29", which names no fact at all.

Two limits, stated rather than discovered later. It reads only the *first*
divergence, so fixing it would not guarantee the rest of the chain resolves -
every number here is an **upper bound**. And a class-level image, the only
kind obtainable, depicts a representative member of the class rather than the
goods in the ruling, so even bucket 2 is optimistic.

Usage: ``python -u scripts/measure_visual_ceiling.py [test-200 | heldout-2000]``.
Sends nothing and costs nothing.
"""

from __future__ import annotations

import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hsclass.config import get_settings  # noqa: E402
from hsclass.data.atlas import load_split  # noqa: E402
from hsclass.eval.metrics import at_digits  # noqa: E402
from hsclass.pipeline.traversal import HierarchicalClassifier, LevelIndex  # noqa: E402
from hsclass.retrieval.embedder import get_embedder  # noqa: E402
from hsclass.retrieval.index import VectorIndex  # noqa: E402
from hsclass.retrieval.lexical import LexicalIndex  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ATLAS = ROOT / "data" / "raw" / "atlas"
SEED = 20261017

#: The buckets, in priority order: the first pattern to match decides, because
#: a segment reading "Of cotton, knitted" turns on the fibre first.
#:
#: Order matters and is deliberate. Composition is tested before fibre name,
#: because "Containing 36 percent or more by weight of flax" is a threshold
#: rather than a material term. Use and legal references are tested before
#: article type, because "Boys' garments imported as parts of playsuits" names
#: a garment but turns on how it was entered.
BUCKETS: tuple[tuple[str, re.Pattern[str], bool | None], ...] = (
    (
        "composition by weight",
        re.compile(r"\bpercent\b|\bby weight\b|%", re.I),
        False,
    ),
    (
        "use, entry or legal reference",
        re.compile(
            r"imported as|parts of|for use|designed for|suitable for|restraint"
            r"|statistical note|additional u\.?s\.?|provided for in|note \d",
            re.I,
        ),
        False,
    ),
    # A photograph cannot identify a heterocyclic compound; one white powder
    # looks like another. Chemistry is a large part of this corpus - chapter
    # 29 alone is 143 of the 2,000 held-out cases - so it earns its own line
    # rather than sitting in "ambiguous". Deliberately about compound
    # chemistry and not materials, so "Other articles of plastics" still
    # falls to material identity below.
    (
        "chemical identity",
        re.compile(
            r"heterocyclic|acyclic|carboxylic|hetero-atom|alcohols?\b|acids?\b"
            r"|esters?\b|ethers?\b|amines?\b|amides?\b|nitriles?\b|aldehyde"
            r"|ketone|sulphonated|sulfonated|halogenated|nitrated|nitrosated"
            r"|\bCAS\b|organo-|hydrocarbon",
            re.I,
        ),
        False,
    ),
    (
        "fibre or material identity",
        re.compile(
            r"\bof (?:cotton|wool|silk|flax|ramie|jute|hemp|man-made|synthetic"
            r"|artificial|other textile|fine animal hair|leather|rubber|plastics"
            r"|paper|glass|iron|steel|copper|aluminum|aluminium|wood)\b"
            r"|\b(?:cashmere|mohair|angora|alpaca|viscose|rayon|acrylic|nylon"
            r"|polyester|spandex|elastane|modal|lyocell|polyamide)\b",
            re.I,
        ),
        False,
    ),
    (
        "construction or visible feature",
        re.compile(
            r"knit|crochet|woven|pile|lace|net\b|embroider|impregnat|coat(?:ed|ing)"
            r"|laminat|quilt|pleat|sleeve|collar|hood|zipper|button|open(?:-| )work"
            r"|jacquard|ribbed|mesh|print",
            re.I,
        ),
        True,
    ),
    (
        "article type",
        re.compile(
            r"coat|jacket|vest|waistcoat|pullover|sweater|cardigan|sweatshirt"
            r"|shirt|blouse|dress|skirt|trouser|pant|short|suit|ensemble|overall"
            r"|pyjama|pajama|nightwear|sleepwear|robe|swimwear|brief|panty|sock"
            r"|stocking|hosiery|glove|scarf|shawl|hat|cap|footwear|shoe|boot"
            r"|bag|case|pan|pot|tent|blanket|towel|curtain|carpet|rug",
            re.I,
        ),
        True,
    ),
    ("gender or age", re.compile(r"\b(?:men|women|boy|girl|babies|infant)", re.I), None),
    ("residual — other", re.compile(r"^other\b|^others$", re.I), None),
)


def say(line: str) -> None:
    sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()


#: "Section XI", "Chapter 61". Dropped before comparing: GRI 1 gives section
#: and chapter titles no legal force, and a cross-chapter error that reports
#: its divergence as "Chapter 29" has named no fact the system could have used.
TITLE_RE = re.compile(r"^(?:section|chapter)\s+\S+$", re.I)

#: Words too common to carry the discriminating sense.
STOP = frozenset(
    "the and for with other others of or not elsewhere specified included "
    "including parts thereof whether in by to be more less than over under "
    "containing made from their its".split()
)


def segments(text: str) -> list[str]:
    return [
        s.strip()
        for s in text.split("—")
        if s.strip() and not TITLE_RE.match(s.strip())
    ]


def first_divergence(gold_text: str, got_text: str) -> str | None:
    """The gold segment at the point the two chains part company."""
    gold, got = segments(gold_text), segments(got_text)
    for index, segment in enumerate(gold):
        if index >= len(got) or segment.lower() != got[index].lower():
            return segment
    return None


def content_words(segment: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z]{4,}", segment.lower()) if w not in STOP
    }


def stated_in(segment: str, description: str) -> bool:
    """Does the description already carry this segment's discriminating words?

    Matched on a four-character stem so "fittings" finds "fitting" and
    "knitted" finds "knit". Deliberately generous: a false "yes" here
    *shrinks* the share an image could help with, so the bias runs against
    the conclusion this script is most likely to be used to support.
    """
    words = content_words(segment)
    if not words:
        return False
    haystack = description.lower()
    return any(re.search(rf"\b{re.escape(w[:5])}", haystack) for w in words)


def bucket(segment: str) -> tuple[str, bool | None]:
    """Which kind of fact this segment turns on, and whether a photo shows it."""
    for name, pattern, visible in BUCKETS:
        if pattern.search(segment):
            return name, visible
    return "unclassified", None


def main() -> int:
    wanted = sys.argv[1] if len(sys.argv) > 1 else "test-200"
    settings = get_settings()
    revision = settings.require_revision()

    embedder = get_embedder(settings.embedding_backend, settings.embedding_model)
    levelled = VectorIndex.load(settings.processed_dir / "index-levels")
    texts = {c.code: c.text for c in levelled.candidates}
    proposer = HierarchicalClassifier(
        LevelIndex(levelled, LexicalIndex.build(levelled.candidates)), embedder, k=5
    )

    official, _ = load_split(ATLAS / "test.jsonl")
    train, _ = load_split(ATLAS / "train.jsonl")
    examples = (
        official if wanted == "test-200" else random.Random(SEED).sample(train, 2000)
    )

    counts: Counter[str] = Counter()
    visible_of: dict[str, bool | None] = {}
    correct = no_gold_text = no_divergence = 0
    samples: dict[str, list[str]] = {}

    for example in examples:
        gold = "".join(ch for ch in example.code if ch.isdigit())
        candidates = list(proposer(example.description)[0])
        if not candidates:
            continue
        top1 = candidates[0]
        if at_digits(top1, 6) == at_digits(gold, 6):
            correct += 1
            continue
        if gold not in texts:
            # The gold line is absent from this revision's tree, so there is
            # no chain to compare. Counted, never silently skipped.
            no_gold_text += 1
            continue
        segment = first_divergence(texts[gold], texts.get(top1, ""))
        if segment is None:
            no_divergence += 1
            continue

        if stated_in(segment, example.description):
            name, visible = "already in the description", False
        else:
            name, visible = bucket(segment)
            name = f"absent: {name}"
        counts[name] += 1
        visible_of[name] = visible
        samples.setdefault(name, [])
        if len(samples[name]) < 3:
            samples[name].append(f"{example.description[:44]} | {segment[:52]}")

    errors = sum(counts.values())
    stated = counts["already in the description"]
    visual = sum(n for k, n in counts.items() if visible_of.get(k) is True)
    not_visual = (
        sum(n for k, n in counts.items() if visible_of.get(k) is False) - stated
    )
    unclear = errors - stated - visual - not_visual

    say(f"{wanted}, revision {revision}")
    say(f"  top-1 right at 6 digits        {correct:>5}/{len(examples)}")
    say(f"  errors with a comparable chain {errors:>5}")
    say(f"  gold line absent from the tree {no_gold_text:>5}")
    say(f"  chains never diverge           {no_divergence:>5}")
    say("\nwhat the system needed to know, at the point the chains part:")
    for name, count in counts.most_common():
        mark = {True: "visible", False: "NOT visible", None: "ambiguous"}[
            visible_of.get(name)
        ]
        say(f"  {name:<34}{count:>5}  {count / errors:>6.1%}   {mark}")
    say("\nthe ceiling on an image, as a share of errors with a comparable chain:")
    say(f"  the text already said it         {stated:>5}  {stated / errors:>6.1%}"
        "   image adds nothing; retrieval missed it")
    say(f"  absent and not visible           {not_visual:>5}  {not_visual / errors:>6.1%}"
        "   nothing but the importer can supply it")
    say(f"  absent and visible               {visual:>5}  {visual / errors:>6.1%}"
        "   <- the only bucket an image serves")
    say(f"  ambiguous or unclassified        {unclear:>5}  {unclear / errors:>6.1%}")
    say("\nupper bound: only the first divergence is bucketed, and a class-level")
    say("image depicts the class rather than these goods.")
    say("\nexamples per bucket:")
    for name, rows in samples.items():
        say(f"  {name}:")
        for row in rows:
            say(f"      {row}")

    report = {
        "revision": revision,
        "set": wanted,
        "n": len(examples),
        "top1_right_at_6": correct,
        "errors_with_comparable_chain": errors,
        "gold_absent_from_tree": no_gold_text,
        "chains_never_diverge": no_divergence,
        "buckets": {
            name: {
                "n": count,
                "share": round(count / errors, 4) if errors else 0.0,
                "visible_in_a_photograph": visible_of.get(name),
            }
            for name, count in counts.most_common()
        },
        "ceiling": {
            "an_image_could_settle": visual,
            "an_image_cannot_settle": not_visual,
            "ambiguous": unclear,
            "share_settleable": round(visual / errors, 4) if errors else 0.0,
            "caveat": "upper bound: first divergence only, and class-level images",
        },
    }
    out = ROOT / "reports" / f"visual-ceiling-{wanted}.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    say(f"\nwrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
