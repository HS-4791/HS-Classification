# HS tariff classification

Classify a product into its customs tariff code, return the legal reasoning path that produced
it, and escalate rather than guess when the nomenclature is genuinely ambiguous.

Scored against real binding rulings issued by US Customs, so the ground truth is an authority's,
not the author's.

> **Status: week 2 of 4, in progress.** The schedule is parsed from the PDF; **99.10%** of
> nomenclature rows agree with the published export on every field. The baseline is measured below;
> it is the floor every later component has to beat, and CI fails the build if it drops.

## The problem

Every imported shipment must be assigned a tariff code. That code sets the duty rate, decides
whether a licence is required, and triggers any quota or restriction. Get it wrong and the
importer owes back-duties and penalties while the shipment sits at the border.

Classification is not a lookup. It follows a legal procedure — the six General Rules of
Interpretation — over a hierarchical nomenclature whose Section and Chapter **notes are
themselves binding law**. Titles are explicitly guidance only; the notes control. Notes
cross-reference each other and exclude goods into other chapters, so reaching a code often means
following a chain rather than matching a description.

## What the system returns

For one product, given a description and optionally an image:

- the tariff code at both the six-digit international level and the ten-digit national level
- the GRI path taken — which rule resolved it, and at which level of the hierarchy
- the heading text and note numbers that decided it, each citable back to the source
- **the schedule version the answer is valid against**
- or, when candidates genuinely tie, a ranked shortlist and an explicit refusal to pick

That last line is what separates this from a demo. A classifier that always answers is not
deployable. The value sits in knowing which cases to put in front of a human, and being right
about which ones those are.

## Architecture

Four stages. **Only stage 3 decides.**

| Stage | Does | Deterministic? |
| --- | --- | --- |
| 1 · Normalise | extracts typed attributes from text and image | no — model |
| 2 · Propose | vector retrieval + hierarchical traversal generate candidates | no — model |
| 3 · **Adjudicate** | **GRI rules 1–4 in order, rule 6 at subheading level, over the notes** | **yes — no model in it** |
| 4 · Explain | writes the reasoning from a decision already made, or escalates | no — model |

Stage 4 writes the explanation from a decision already made, which is why it cannot invent a
justification for an answer the rules did not reach.

### What "only stage 3 decides" does and does not mean

This was stated here as "stages 1 and 2 are allowed to be wrong, because stage 3 checks them
against the rules", and that is too strong. A cold read of the reranker caught it.

What holds, and is asserted as a property rather than prose: **stage 3 is deterministic and
auditable.** The same candidates always produce the same decision and the same rule path.
Every branch that returns a code either has a single candidate or picks by a rule that reads
the whole set, so no reordering can change the answer — verified by exhausting all 120
permutations of the real top-5 on 30 cases, 3,600 adjudications, zero violations.

What does not hold is that stage 3 *checks* stage 2. It checks the candidates it is **given**.
It cannot know whether the right provision was ever proposed, and the adjudicator sees only the
first five distinct codes the proposer ranked. So stage 2 does not merely risk being wrong — it
bounds what stage 3 is able to decide, and a probabilistic reranker changes that set rather than
only its order. Measured with the free reranker on 500 cases, the top-5 set changed on 88.4%,
the answer-or-escalate outcome flipped on 7, and the decided code differed on 3 of the 5 both
configurations answered.

The sharpest case: a women's coat of 70% polyester and 30% wool. Reranking admitted a chapter 64
footwear line into a set of chapter 62 coat lines; the engine's condition rule then correctly
eliminated all four candidates requiring 36% wool or more, leaving the footwear line — which
states no threshold at all — as the only survivor, and decided the coat was
**6405.20.6060, "Other footwear, with soles and uppers of wool felt"**, under GRI 1 with a
citation attached.

Two things follow. That specific route is closed: a lone survivor must now share a heading with
something that was eliminated, because a provision in another heading was never competing on
those terms. And the general property is now stated as what it is — the refusal is what contains
the damage, not the determinism. 97.5% of cases escalate rather than answer, and a deterministic
wrong answer is worse than an escalation precisely because it carries a rule path that looks
like reasoning.

The shared knowledge layer holds the nomenclature graph (serving stage 2's traversal) and the
notes and GRI preloaded (serving stage 3's evaluation).

### Why each component is here

Each row names the question only that component answers. If the middle column cannot be written,
the component gets dropped.

| Component | The question it answers |
| --- | --- |
| Vector retrieval | Which twenty headings might this plausibly be? |
| Hierarchical traversal | Inside chapter 39, which heading — then which subheading? |
| Note-chain traversal | This note excludes the goods into chapter 48 — does 48 send them back? |
| KAG | Does this product satisfy the conditions in note 2(b) of chapter 39? |
| CAG | What are the GRI and the notes for the chapters in play? |
| Multimodal extraction | What is this thing actually made of, and what form is it in? |

## The schedule is versioned, and that is load-bearing

USITC publishes a new HTS revision roughly every two weeks. A classification is only true
against a schedule version, so the version is pinned, stamped onto every trace and every eval
report, and returned with every answer.

Current pin: **`2026HTSRev21`** (published 7 Oct 2026).

`data/manifests/2026HTSRev21.json` holds the source URLs, SHA-256 of every file and the expected
tree shape. `scripts/fetch_schedule.py --verify` fails if what the endpoint serves no longer
matches — which is how a silently-newer export gets caught instead of being validated against
the wrong tree.

## The tree

The nomenclature is parsed into a tree, **one node per tariff line, and not chunked** — chunking
by token count would destroy the hierarchy, and the hierarchy is the legal structure. GRI 6
states that only subheadings at the same level are comparable, which is what makes hierarchical
traversal legally required rather than merely efficient.

Built from the pinned release: **35,903 nodes** — 29,861 coded lines, 5,944 uncoded grouping
lines and 98 synthesised chapter nodes (chapter 77 is reserved in the HS and has no lines).

Four things the published data does that a naive parser gets wrong:

1. **5,944 lines carry no code at all.** `Men's or boys':` is a grouping line; everything beneath
   it inherits that condition. Keep only coded lines and one node in six is silently discarded.
2. **The export has no Section or Chapter rows.** Chapters are synthesised from the digits.
   Section is not in the digits anywhere and must come from the PDF — and Section XI's notes are
   what decide the cotton-versus-polyester question.
3. **`indent` is zero-padded on three rows** (`"03"`, `"04"`, all in chapter 98). Compared as
   strings, the hierarchy breaks in exactly three places.
4. **Chapters 28, 29 and others carry Roman-numeral subchapters** emitted *before* the chapter
   they open. They scope 7,353 nodes.

Every node carries its **inherited text chain**. `6109.10` reads as literally "Of cotton" — the
embeddable unit is `T-shirts, singlets, tank tops and similar garments, knitted or crocheted —
of cotton`, assembled from the tree before embedding.

### Validation is a deliverable, not a formality

A corrupt tree does not raise. It answers confidently and wrongly. `hsclass schedule validate`
asserts: exactly one parent per node, links symmetric in both directions, every leaf a terminal
code, no level jumps the code prefix cannot vouch for, every coded node nested under its own
digit prefix, every colon-terminated line having children, and counts reconciling against the
manifest.

Current state: **0 errors, 50 warnings**, each one a documented property of the published data
(27 non-contiguous indents, 16 chapter-98/99 cross-references, 6 colon-terminated terminal codes,
and 1 flagging that Sections are unresolved until the PDF parse lands).

## Parsing the PDF, and proving it

The PDF is the source, because most jurisdictions publish nothing else. The export is the
**oracle**: it carries the same lines with an explicit `indent`, so every parsed row can be
checked against it — same code, same depth, same text, same rates.

Across all 4,534 pages, two numbers are reported, and the gap between them is the point:

| | |
| --- | ---: |
| rows **found** - code and description align | 99.86% |
| rows **correct**, chapters 1-97 - every field agrees | **99.10%** |
| rows correct, chapters 98-99 | 44.74% |
| rows correct, whole document | 93.11% |

The first was published on its own as "row accuracy" for two weeks. It counts a row as matched
when it lines up with its counterpart, whatever its contents, so it sat at 99.84% while the same
report recorded 2,163 wrong general rates, 1,652 wrong Column 2 rates and 576 rows that do not
exist in the schedule at all. The comparison also stripped periods from both sides before
checking, which made 2,296 rows reading `No` for `No.` invisible by construction - a check
deleting the characters it exists to compare.

Both numbers are now published. Chapter 61, where the running example lives, is at 100% on the
field-level measure, and that is what CI enforces. Chapter 99 is at 34.93%: its subchapters use
a rate layout the parser does not model, which is recorded under known limits below rather than
averaged away.

The hierarchy is encoded in horizontal position alone. Four things make that harder than it
sounds, each found by the reconciliation rather than by reading the file:

| What | Why it breaks a naive parser |
| --- | --- |
| Word spacing | Only survives at `x_tolerance=1.0`. At 1.5 pdfplumber silently yields `T-shirts,allwhite,shorthemmed` |
| Italics | A scientific name rides ~1.8pt above the roman text *on the same line*. Grouping rows by `top` splits them; grouping by **baseline** keeps them whole |
| Superscripts | The raised 2 in `g/m2` sits 5.3pt below the line above and 6.6pt above its own — it is nearer the *wrong* line. Identified by font size (5.1pt vs 8.5pt), then attached to the line beneath |
| `(con.)` headers | A table continuing across a page restates its parent context. The marker sometimes lands on its own line *after* the restated row closed, so a naive rule keeps the duplicate or eats the real row below it |

None of these raise an error. They produce a tree that is quietly wrong.

### Known limits, stated rather than hidden

- **Chapters 98 and 99 are not parsed for rates, and the number says so.** They are US special
  provisions that re-describe goods classified elsewhere, and their subchapters use layouts this
  parser does not model - a fifth rate column in 99 I-II, a single "Additional Duties" column in
  99 IV whose values print at the General position. Every rate mismatch in the whole document is
  in these two chapters: 2,163 general, 1,657 special, 1,652 Column 2, and **zero anywhere
  else**. Modelling a second column grid for provisions the classifier never visits would buy
  nothing, so they are reported separately rather than averaged into a flattering total.
- **261 units of quantity are still wrong**, all of them `m2`/`m3` where a superscript attaches
  to the neighbouring row - one row reads `m33` and the row that owned the glyph is left with
  `m`. Two attempts at the tie-break did not fix it and the mechanism is not yet understood;
  measured, not guessed at, and not claimed to be solved.
- Roughly a hundred rows document-wide contain glyphs the schedule's fonts map to control
  characters rather than Unicode. The character data is not in the file; they are dropped rather
  than guessed at.
- A description ending in an abbreviation (`Ictalurus spp.`) has its dot leaders printed flush
  against it, so the abbreviating period cannot be told from the leaders. Both are stripped.

## Results

**Vector-only baseline** — embed the description, embed every tariff line, take the nearest.
No hierarchy, no notes, no rules. `BAAI/bge-small-en-v1.5` over 27,146 searchable lines.

| Metric | Official test split | Held-out slice of train |
| --- | ---: | ---: |
| n | 200 | 2,000 |
| top-1 @ 6 digits | **14.50%** | **14.00%** |
| top-5 @ 6 digits | 28.00% | 30.10% |
| top-1 @ 10 digits | 6.09% | 4.55% |
| escalation rate | 0% — the baseline never declines | 0% |
| latency per classification | 32ms | 31ms |

Two sets, reported separately and never averaged. The official 200-row split is what the
published baselines score on, so it is the comparable number; 200 rows carry a confidence
interval of roughly ±3.5 points, which is wider than several of the deltas the later ablations
need to show, so a 2,000-row slice of train is scored alongside it at about ±1.1. Nothing is
trained on ATLAS — the index is built from the tariff schedule alone — so that slice is a second
test set rather than a leak. The two agree to within half a point at six digits.

Six-digit and ten-digit accuracy are reported separately throughout. Six digits is
internationally identical and is the primary claim; ten is US-only and harder. Conflating them
overstates.

**What the floor says.** 14.5% is low, and that is the finding: flat similarity search over a
legal nomenclature mostly fails. The weakest chapters are the contested ones the domain primer
names — organic chemicals (7%), machinery (12%), plastics (15%), apparel (18%) — which is
exactly where the notes and the GRI are supposed to earn their place. `reports/` carries the
full per-chapter breakdown.

**One measured negative result.** BGE's model card prescribes a query instruction prefix for
retrieval. Adding it moved top-1 at six digits by +0.5 points and moved top-5 *down* by 1.5, so
it is not used. Stripping the dataset's question template was worth +4.5 points on its own.

## Ablations

Each configuration is scored on both sets, and the delta is published. A component that adds
nothing is a finding, not something to hide.

| Configuration | top-1 @ 6 (test / held-out) | top-5 @ 6 | top-1 @ 10 | Δ top-1 @ 6 |
| --- | ---: | ---: | ---: | ---: |
| 1 · vector retrieval only | 14.50% / 14.00% | 28.00% / 30.10% | 6.09% / 4.55% | — |
| 2 · + hierarchical traversal | 13.50% / 13.50% | 21.00% / 24.55% | 2.54% / 2.88% | **−1.00 / −0.50** |
| 3 · + GRI engine adjudicating | 1.50% / 0.35% | 1.50% / 0.35% | 0% / 0% | **−13.00 / −13.65** |
| 4 · + hybrid dense/lexical retrieval | 15.00% / 13.75% | 22.50% / 24.70% | 3.05% / 2.98% | **+0.50 / −0.25** |
| 5 · hybrid + GRI engine | 1.50% / 0.35% | 1.50% / 0.35% | 0% / 0% | **−13.00 / −13.65** |

Configurations 3 and 5 answer 5 of 200 and 5 of 200 on the test split; the coverage pair is
below, and five decided cases is too small to rank 3 against 5.

**Hierarchical traversal does not improve accuracy, and the honest reading is that it costs a
little.** Top-1 is a wash within noise; top-5 is clearly worse. Adding hybrid retrieval (4)
recovers top-1 on the test split and pushes it half a point past the flat floor, but top-5
stays well behind it — 22.50% against 28.00% — so the overall reading does not change. Its
justification is not accuracy, and the ablation is what makes that claim checkable rather than
rhetorical:

- **It is legally required.** GRI 6 states that only subheadings at the same level are
  comparable. Flat search weighs a six-digit subheading in one heading against a six-digit
  subheading in another, which is not a comparison the nomenclature permits.
- **It bounds the context by construction.** Flat search compares every query against all
  **27,146** lines. Traversal compares a median of **1,078** — a **96% reduction** — and the
  winning path carries a median of 628 characters of schedule text. That is the token-budget
  argument, measured rather than asserted.
- **It is what the rule engine needs.** GRI rules apply at a level, to the candidates at that
  level, against the notes in scope. There is nowhere to attach them in a flat ranking.

### Why the first attempt was much worse, and what fixed it

The first run scored 7.00%, which is −7.5 points rather than −0.5. Two design errors, both mine:

| Diagnostic | Finding |
| --- | --- |
| Heading recall @5 | **40.0%** — a beam of 5 loses 60% of cases before the walk descends |
| Heading recall @20 | 66.5% |
| Heading recall @50 | **78.5%** — the default now |

The heading step is the bottleneck: a heading is a handful of words scored against a detailed
product description. The spec's own framing — *"which twenty headings might this be?"* — is the
floor, not the ceiling.

The second error was ranking paths by the **mean** of their level scores. A weak heading match
then drags down the line that actually matched well. Ranking by the deepest step instead — the
levels above it only narrowed — is worth **+5 points** of top-1 at six digits on its own.

### Ablation 3: the rule engine decides almost nothing, and that is the result

Headline accuracy collapses because an escalated case counts as a miss. The pair of numbers that
matters is coverage against accuracy on the slice the system actually answered:

| | Decided | Accuracy on those | If those had been forced |
| --- | ---: | ---: | ---: |
| test split (n=200) | 5 cases — 2.50% | 3 / 5 = 60% | 13.85% |
| held-out (n=2,000) | 22 cases — 1.10% | 7 / 22 = 31.8% | 13.45% |
| held-out, on hybrid retrieval | 24 cases — 1.20% | 7 / 24 = 29.2% | 13.66% |

**Twenty-seven decided cases is too small to claim much**, though both slices sit above the 14%
baseline. What the ablation does establish is *why* coverage is so narrow, and that is measured
rather than guessed:

| Gate | Share of descriptions |
| --- | ---: |
| states any percentage at all | 12.0% |
| yields a mixture of two or more materials | 5.0% |
| **and** retrieval proposed candidates a rule can tell apart | **1.0–2.5%** |

The engine is not wrong; it is starved. It can only decide a case whose rule condition is
*evaluable*, and a condition over typed attributes needs those attributes to exist. Composition
is currently read with a regex from whatever the description happens to say. That is what stage
1 — model-based attribute extraction from text and images — is for, and it is what widens the
first row. The third row widens with better candidate generation.

One honest reading of the escalation column: forcing an answer on the declined cases scores
about 14%, which is the baseline. The refusal is not yet selecting hard cases — it is declining
nearly everything, because nearly nothing is evaluable. Escalation calibration becomes a real
question in week 3, once there is a population of genuinely close calls to calibrate on.


### The KAG layer: provision text as arithmetic, and why it changes nothing yet

Retrieval cannot separate "Containing 85 percent or more by weight of wool" from "Containing
less than 85 percent by weight of wool". They are nearly the same string and they describe
opposite goods; only arithmetic separates them. 980 lines in the schedule state a percentage in
their own text and 95 binding notes do, so `pipeline/conditions.py` reads them as conditions —
an attribute, a comparison, a number — and the engine evaluates them under GRI 1, before any
later rule is reached.

It parses **790 of those 980 lines** into **999 conditions**. The 190 it declines are honest
declines: the attribute lives in some parent provision ("Not more than 25 percent by weight" —
of what?), and choosing which parent governs is a judgment, not a parse.

Two defects were found by measuring the parser against all 980 lines rather than by reading it,
and both were silent:

* **"not over" inverted the comparison.** With only "over" in the operator vocabulary, "not over
  6 percent by weight of butterfat" parsed as *greater than 6* — a confident wrong answer on the
  chapter 4 lines that phrase a ceiling that way.
* **A range parsed as one reversed bound.** "over 6 percent but not over 35 percent" came out as
  a single `<= 6`, because "but not over" was read as a suffix on the first number instead of as
  the second bound it introduces.

Wired into the engine, the layer decides **zero additional cases on the test split**, and the
funnel says exactly why:

| Gate | test split (n=200) |
| --- | ---: |
| composition extractable from the description | 24 |
| a candidate provision carries a condition | 21 |
| **the condition's attribute is among the extracted facts** | **4** |
| the condition eliminated a candidate | 1 |
| elimination left exactly one provision | 0 |

The parser is not the bottleneck. The condition asks about a material the one-line description
never quantifies — the collapse is 21 to 4. This is the same starvation the ablation reports,
measured one level deeper, and it localises the fix to stage 1: model-based attribute extraction
is what fills `Facts`, and nothing in stage 3 can compensate for its absence.

#### Surviving by ignorance is not qualifying

Before that guard existed, the layer did decide one held-out case, and got it wrong. A cardigan
of 53% rayon and 47% wool had its "85 percent or more by weight of wool" candidates correctly
eliminated — 47 is not 85, and the arithmetic was right. What survived was "Containing 30
percent or more by weight of silk or silk waste", kept only because the goods say nothing about
silk, so that condition could not be tested at all.

"An untested condition has not failed" was applied to elimination and forgotten on selection. A
lone survivor must now be *positively* consistent — stating no condition, or only conditions
that were tested and held — or the elimination is set aside and the ordinary rules run on every
candidate. With the guard, the layer makes **no decisions it cannot justify**: coverage and
accuracy return to the baseline above exactly, and the judgement oracle drops from 10 decided to
9 while holding agreement at 4 of 903.

Published baselines this will be scored beside: arXiv `2509.18400` (ATLAS), `2412.14179`
(independent benchmark), `2605.14857` (deterministic agentic workflow).

## Ablations 4 and 5: hybrid retrieval, and a finding that was retracted

Retrieval recall was the obvious thing to fix next. Heading recall@5 was 40.0%, the rule engine
was starved of candidates, and one held-out failure had the walk proposing chapter 51 *woven
fabrics* for a knitted cardigan. Everything in this section is produced by
`scripts/measure_retrieval.py`, which imports what the pipeline imports.

### Chapter routing: refuted by its own numbers

The first instinct is to predict the chapter and then search inside it. It does not work, and
the measurement says why in one line: **the chapter is not an easier decision than the
heading.**

| | |
| --- | ---: |
| gold chapter already present in the beam | 98.5% |
| heading misses whose chapter *was* present | 40 of 43 |
| chapter top-1 accuracy | 47.0% |
| heading recall@5, restricted to the top 3 chapters | 42.0% (from 40.0%) |

Knowing the chapter would be worth a lot — heading recall@5 inside the gold chapter is 66.0% —
but that figure needs an oracle. Predicting the chapter is 47% accurate, which is no better than
the problem it was meant to simplify. No routing stage can help.

### Hybrid dense/lexical retrieval: kept

| heading recall (test / held-out) | @5 | @20 | @50 |
| --- | ---: | ---: | ---: |
| dense | 40.0% / 38.6% | 66.5% / 61.8% | 78.5% / 77.2% |
| BM25, a heading's own text only | 18.5% / 23.5% | 28.0% / 34.8% | 39.5% / 42.6% |
| BM25 with descendant vocabulary | 36.5% / 37.3% | 52.0% / 57.5% | 64.0% / 67.2% |
| **fused, reciprocal rank** | **49.0% / 49.4%** | **68.5% / 71.7%** | **83.5% / 82.5%** |

**A heading's own text is too short to search lexically** — 16 tokens of legal drafting, and
BM25 over it recalls 18.5%. Giving each provision the vocabulary of everything beneath it takes
the same method to 36.5%, because the schedule names goods in the statistical breakouts rather
than in the heading. Fused with the dense ranking it beats both, by nine points at five.

Rank fusion rather than a weighted sum: cosine and BM25 are different scales with different
spreads, and any weighting between them would have to be fitted — on these sets, which is where
that goes wrong.

### The retraction

This section first reported that **lexical fusion helps only at the heading level and the damage
grows with depth**, on these figures: top1@6 of 14.50% fusing the heading, against 11.00% adding
the tariff line and 6.50% fusing every level. The explanation offered was that a deep sibling
set is a handful of near-identical lines, so BM25 over it is noise. The gain was also quoted
against a dense baseline of 14.00% measured *before* per-heading pruning landed — comparing two
code versions, which is the same discipline the CI gate enforces across schedule revisions.

That finding was wrong, and a subagent cold-read of the two files found why. Walks were sorted
by the score each level was *selected* by — which, once fusion existed, was a rank-fusion value
(about 0.016 to 0.033) at a fused level and a cosine (about 0.4 to 0.8) everywhere else. Sorting
mixed the two directly. With fusion at the heading and the tariff line, 15% of queries produced
a list where every fused-level walk sorted below every unfused one regardless of merit; fusing
all four levels gave 91% of walks a duplicate score, because RRF's maximum is `2/(k+1)` whatever
the sibling set's size. The deeper the fusion, the more of the ranking the bug destroyed — which
is exactly the shape of a component that gets worse with depth.

Fusion now decides *which* candidates survive a level and similarity ranks the survivors. Those
are separate jobs, and only the second needs one comparable scale. Re-measured:

| levels fused | test top1@6 / top5@6 | held-out top1@6 / top5@6 |
| --- | ---: | ---: |
| none, dense only | 13.50% / 21.00% | 13.50% / 24.55% |
| heading | 14.50% / 22.00% | 13.75% / 24.55% |
| **heading + subheading** | **15.00% / 22.50%** | **13.75% / 24.70%** |
| heading + tariff line | 14.50% / 22.00% | 13.75% / 24.45% |
| every level | 15.00% / 22.50% | 13.75% / 24.70% |

There is no deep-level penalty. Fusing every level is tied for best, and identical to fusing
heading and subheading on all four figures — so fusion at eight and ten digits changes nothing,
which matches the vocabulary: 48.2% of ten-digit provisions share an identical bag of terms with
another provision, and 5,488 of 20,280 reduce to no terms at all. The shipped default is
`(4, 6)`: the cheaper of two equal configurations.

**The lesson is the one worth keeping.** A bug that degrades a configuration is
indistinguishable from a configuration that does not work, and the plausible mechanism offered
for it made the result easier to believe rather than harder. The number was reproducible, the
explanation was sound, and both were wrong.

### What still limits accuracy, and it is not recall

| | test split | held-out 2,000 |
| --- | ---: | ---: |
| gold heading retrieved into the beam | 83.5% | 82.5% |
| gold heading still standing after the descent | 83.5% | 82.5% |
| gold subheading in the candidate pool | 46.5% | 46.3% |
| gold subheading in the final top 5 | 22.0% | 24.6% |
| **when it is in the pool at all, ranked within 5** | **47.3%** | **53.0%** |

The last row is the finding, and it survived the retraction. The pool holds the right subheading
twice as often as the output does, at a median rank of 7 on the test split. **Retrieval recall
is not the binding constraint on accuracy; the ranking of what has already been retrieved is.**
Nine points of heading recall@5 converted to 1.5 points of top-1 — three cases in 200. A
reranker is the next real lever, and these numbers are the argument for it.

### Why the per-heading beam is kept although it gains nothing

Rows one and two of that table are now equal, and they were not. A single global beam of 50,
shared across 50 expanded headings, let one heading's subheadings evict another heading's entire
subtree: 83.5% of gold headings were retrieved and only 65.0% were still alive at the bottom.
Pruning within each heading keeps all of them — the two rows match exactly, which is the claim
tested rather than asserted.

It is worth being careful about the reason. Retrieval is stage 2 and is not adjudicating, so
this is **not** a GRI 6 violation — nothing is being classified yet. But ranking one heading's
subheading against another's is the operation traversal was adopted here to avoid, and the
global beam quietly reintroduced it inside the walk. It is a wash on accuracy. It is kept for
the recall it stops discarding and for consistency with the architecture's own rationale, not
for a gain it does not deliver.

### What hybrid retrieval cannot do

Two limits, both counted rather than asserted.

**Vocabulary the schedule does not contain cannot be indexed.** The held-out cardigan is the
honest case: "a women's cardigan made of 53% rayon and 47% wool" belongs in heading 6110, which
reads "Sweaters, pullovers, sweatshirts, waistcoats (vests) and **similar articles**, knitted or
crocheted". The word *cardigan* appears nowhere in the nomenclature — not in 6110, not in any
line beneath it, nowhere in the schedule. Knowing a cardigan is a similar article to a sweater
is world knowledge, and supplying it is stage 1's job.

**Vocabulary the tokeniser collapses cannot be distinguished.** "m-Xylene", "o-Xylene" and
"p-Xylene" all reduce to a single term, *xylene*, and that prefix is the entire legal
distinction between 2902.41, .42 and .43. Seventy provisions carry such a prefix and collapse
into thirteen collisions. Two descriptions differing only by an isomer therefore produce
byte-identical BM25 rankings, and contribute the same vote to provisions at different duty
rates.

## Reranking: the largest accuracy gain in the project, and it buys nothing

The funnel said the ranking of what retrieval had already found was the binding constraint: the
candidate pool held the right subheading for 47.0% of test cases and the output surfaced it for
22.0%. A cross-encoder over the pool closes much of that gap, and changes the adjudicated
system not at all. Both halves are the result. Everything here comes from
`scripts/measure_rerank.py`.

### What it buys, by stage

| test split (n=200) | top1@6 | top5@6 | decided | right on decided | per query |
| --- | ---: | ---: | ---: | ---: | ---: |
| **proposer**, no reranker | 15.00% | 22.50% | — | — | 16ms |
| **proposer**, cross-encoder | **19.50%** | **26.00%** | — | — | 1,948ms |
| **adjudicated**, no reranker | 1.50% | 1.50% | 5 of 200 | 3/5 = 60.0% | 30ms |
| **adjudicated**, cross-encoder | 1.50% | 1.50% | 5 of 200 | 3/5 = 60.0% | 1,810ms |

**+4.5 points of top-1 on the proposer, and nothing whatever on the adjudicated system.** Not a
regression, not noise — the same five cases, the same three correct, verified case by case
rather than inferred from matching totals.

That is not a contradiction, it is what the escalation rate means. The adjudicated system
answers 2.5% of cases and declines the rest, and whether the GRI resolve a case does not depend
on how the five candidates are *ordered*. So reranking improves the ranked shortlist handed to a
human on the 97.5% that escalate, which is the system's actual output, and leaves the rules'
decisions untouched. For a stage-2 component that is the behaviour you want. It is also the
reason the proposer number must never be quoted on its own.

### It is not adopted, and the cost is why

`HierarchicalClassifier` takes `reranker=None` by default and nothing in the shipped pipeline
passes one. On this evidence that stays, because the price is not only latency:

- **A hundred times slower.** 16ms to 1,948ms on the proposer, 30ms to 1,810ms adjudicated. And
  the figure is sensitive to memory headroom rather than being a constant: the same adjudicated
  work measured **3,188ms** while another process held its own ONNX arena, so quoting a single
  number for it would be quoting whichever run suited the argument.
- **Six times the memory.** 472MB to **2.7GB** of working set. ONNX reserves its arena inside
  the first twenty classifications and holds it flat, so this is a floor, not a leak. On a 16GB
  machine two evaluation runs do not fit, and the second dies with `bad allocation` from inside
  onnxruntime — an error that names memory nowhere, and which cost an hour to diagnose through
  two wrong hypotheses (unbounded document length, then a leak) before the instrumentation was
  working.

A cost ceiling counting API tokens would see none of this. That is now the argument for wiring
the tracing and cost ledger in.

### A bigger reranker is not a better one

Four models screened on 30 cases, scoring "gold within the top five" among cases whose gold was
in the pool:

| | gold in top 5 | per case |
| --- | ---: | ---: |
| no reranker | 43.3% | 0ms |
| `jinaai/jina-reranker-v1-turbo-en` | 60.0% | 4,509ms |
| **`Xenova/ms-marco-MiniLM-L-6-v2`** | **60.0%** | **4,591ms** |
| `Xenova/ms-marco-MiniLM-L-12-v2` | 53.3% | 6,201ms |
| `BAAI/bge-reranker-base` | **40.0%** | 34,029ms |

`bge-reranker-base` is the strongest of the four on general benchmarks and scores **below having
no reranker at all**, at seven times the cost of the cheapest. These documents are terse legal
nomenclature, not the web prose the models were trained on, so reranking quality does not
transfer from a leaderboard and had to be measured. MiniLM-L-6 is chosen for being tied best and
cheapest.

### How deep to rerank

The pool averages 212 walks and the tail is nearly empty of right answers. Of the cases whose
gold subheading is in the pool at all, the share reachable within the first N by cosine:

| N | reachable | cost |
| ---: | ---: | ---: |
| 10 | 56.4% | ~880ms |
| 30 | 76.6% | ~1,270ms |
| **50** | **89.4%** | **1,948ms, measured** |
| 100 | 95.7% | 2,620ms, measured |
| 212, the whole pool | 100% | ~4,780ms |

Two measured points fit 19.3ms per pair plus about 670ms of fixed overhead per call. Depth 50 is
chosen on cost: the whole pool is 2.9 times the budget for the last 10.6% of reachability, and
depth 100 measured +0.50 top1@6 — one case in 200, which this sample cannot separate from noise.

### The free reranker is worse than nothing, and it took 2,000 cases to say so

`LexicalReranker` fuses each line's cosine with BM25 over that line's own document, for about a
millisecond. Three sample sizes gave three different answers:

| | verdict |
| --- | --- |
| 30-case screen | +10 points — "closes two thirds of the gap" |
| 200-case test split | neutral — +0.00 top1@6, +0.50 top5@6 |
| held-out 2,000 | **harmful — −1.75 top5@6**, 35 cases |
| adjudicated, test split | **harmful** — decided 5 → 7 while right on decided fell 3/5 → 1/7 |

It is kept in the tree as the control, labelled measured-harmful, because a reranker costing a
hundred times as much has to be compared against the free attempt rather than against the
baseline. Why it churns without being more right: 47.1% of a 212-walk pool scores exactly zero
on BM25, so a single shared term lifts a walk past the entire rank-fusion spread of the top 50
cosine positions. The top-5 set changed on 88.4% of 500 cases.

### What reranking exposed, which was worth more than the reranking

Listing the five decided cases instead of comparing the totals is what found this, and the
totals were identical — 5 decided, 60% correct, with and without.

A woman's 100% nylon lace knit dress with a 100% polyester slip was classified
**`6405.20.6060`, "Other footwear, with soles and uppers of wool felt"**, citing
`Section XI Note 2(A)`, in the shipped configuration with no reranker involved. Note 2(A)
governs chapters 50 to 63 and cannot place goods in chapter 64 at all. The two "100%" figures
tie, so the rule reached its "last in numerical order" tie-break, and `max` over a candidate set
spanning chapters returned the footwear line.

`_textile_material_rule` now considers only candidates inside Section XI. The dress decides
`6108.29.1000` — women's slips and night dresses — which is still wrong against a gold of
`6114.90.1030`, but is a garment rather than footwear. **No metric moved**: the case was wrong
before and is wrong now, so coverage and accuracy are unchanged to the decimal. The improvement
is in the quality of the failure, which nothing the project measures can see.

Two uncomfortable things about it. It is the same error class as the condition-rule defect fixed
the day before — a rule eliminating or selecting across chapters it has no authority over — and
after fixing that path, the other path was never checked for the same flaw. And the candidate
set for that dress was
`6405203060, 5512110030, 5804299010, 6405206060, 6108291000`: two footwear lines, a woven
fabric, a lace trim, and one plausible garment. The rules picked the only sensible candidate
offered. The engine behaved correctly on input it should never have been given, which is the
case for fixing stage 1 and stage 2 before polishing stage 3 further.

## Running it

```bash
make setup
```

```bash
make fetch
```

```bash
make check
```

`setup` creates the venv and installs the package; `fetch` downloads the pinned release and
verifies every checksum; `check` runs lint, tests and tree validation.

```bash
hsclass schedule show 6109.10.00
```

Every number in the retrieval section is produced by one script, and that is deliberate — the
first version of those figures came from a prototype whose BM25 documents differed slightly
from the committed ones, so the published table could not be reproduced from the code it
described. A cold read caught it by re-running. The script imports what the pipeline imports:

```bash
python scripts/measure_retrieval.py
```

```bash
python scripts/run_ablations.py
```

```bash
python scripts/measure_rerank.py test-200 cross-encoder-50
```

The reranker takes the process from 472MB to 2.7GB of working set, so run one of these at a
time — two together do not fit on a 16GB machine, and the second dies with `bad allocation`
from inside onnxruntime, which names memory nowhere.

## Production concerns, built in from week 1

- **Tracing** — every classification is reconstructable after the fact: candidates considered,
  rules fired, notes consulted, schedule version, cost and latency, written as one JSON line.
- **A cost ceiling per request**, enforced, raising rather than overspending.
- **Schedule versioning** — stamped on every trace and report; a code without one is unusable.
- **Evals gated in CI** — `scripts/check_regression.py` fails the build when a metric falls below
  the committed floor, and refuses to compare across schedule versions.

## Layout

```
src/hsclass/
  config.py          pinned revision, paths, cost ceiling
  schedule/          tree model, export builder, validators
  eval/              metrics (6 vs 10 digit, coverage, per-chapter), runner
  obs/               tracing and the cost ledger
  schedule/parse_pdf.py  the PDF parser: x-positions back into levels
  schedule/reconcile.py  row-by-row check against the published export
  retrieval/         embedding backends and the dense index
  retrieval/lexical.py   BM25 over descendant vocabulary, and rank fusion
  pipeline/          the four stages
  pipeline/gri.py        the deterministic adjudicator
  pipeline/conditions.py provision thresholds as evaluable arithmetic
scripts/             fetch the pinned release, the CI regression gate
data/manifests/      committed: checksums and expected tree shape
data/raw/            gitignored: reproducible via scripts/fetch_schedule.py
reports/             eval output, committed
```
