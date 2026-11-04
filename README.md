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
| CAG | What are the GRI and the notes for the chapters in play? — *answered by a deterministic stage instead, so there is no prefix to cache; [measured](#cag-the-gate-is-closed-by-showing-the-instrument-has-no-subject)* |
| Multimodal extraction | What is this thing actually made of, and what form is it in? — *document multimodality is built; a product image has a [measured ceiling of ~6% of errors](#multimodal-the-ceiling-on-a-product-image-measured-before-sourcing-one)* |

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
| top-1 @ 6 digits | **14.50%** | **14.05%** |
| top-5 @ 6 digits | 28.00% | 30.15% |
| top-1 @ 10 digits | 6.09% | 4.55% |
| escalation rate | 0% — the baseline never declines | 0% |
| latency, p50 per classification | 9.4ms | 9.4ms |

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
| 1 · vector retrieval only | 14.50% / 14.05% | 28.00% / 30.15% | 6.09% / 4.55% | — |
| 2 · + hierarchical traversal | 13.50% / 13.50% | 21.00% / 24.60% | 2.54% / 2.88% | **−1.00 / −0.55** |
| 3 · + GRI engine adjudicating | 1.50% / 0.25% | 1.50% / 0.25% | 0% / 0% | **−13.00 / −13.80** |
| 4 · + hybrid dense/lexical retrieval | 15.00% / 13.75% | 22.50% / 24.75% | 3.05% / 2.98% | **+0.50 / −0.30** |
| 5 · hybrid + GRI engine | 1.50% / 0.25% | 1.50% / 0.25% | 0% / 0% | **−13.00 / −13.80** |
| 6 · 5 without the note-chain | 1.50% / 0.25% | 1.50% / 0.25% | 0% / 0% | −13.00 / −13.80 |
| 7 · 5 without KAG conditions | 1.50% / 0.25% | 1.50% / 0.25% | 0% / 0% | −13.00 / −13.80 |
| 8 · 5 without Section XI Note 2(A) | 0% / 0% | 0% / 0% | 0% / 0% | −14.50 / −14.05 |

Rows 6 to 8 price each mechanism by removing it. Two change nothing at all and the third takes
the engine from 17 answers to one — see "Pricing each mechanism" below.

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
| test split (n=200) | 4 cases — 2.00% | 3 / 4 = 75% | 13.85% |
| held-out (n=2,000) | 15 cases — 0.75% | 5 / 15 = 33.3% | 13.45% |
| held-out, on hybrid retrieval | 17 cases — 0.85% | 5 / 17 = 29.4% | 13.66% |

**Nineteen decided cases is too small to claim much**, though both slices sit above the 14%
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

One reading of the escalation column used to live here: forcing an answer on the declined cases
scores about 14%, the baseline, so "the refusal is not yet selecting hard cases — it is
declining nearly everything". The first half is a measurement and the second was a guess, and
the guess is wrong, at least on the test split: there the cases the rules answer score five
times the accuracy the declined cases would have scored if forced, which is the spec's own test
for whether a refusal is too cautious. On the held-out 2,000 the rules answer 17 cases and the
intervals overlap, so that set settles nothing either way. What the refusal selects for is
**having facts to reason over**: nearly every case it answers lies inside the 239 where a
composition was extractable. Not confidence — the rules' cases are rank-indistinguishable from
random under every confidence signal tried. See the calibration section below.


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
| **proposer**, cross-encoder | **19.50%** | **26.00%** | — | — | 1,729-1,948ms |
| **adjudicated**, no reranker | 1.50% | 1.50% | 4 of 200 | 3/4 = 75.0% | 18ms |
| **adjudicated**, cross-encoder | 1.50% | 1.50% | 4 of 200 | 3/4 = 75.0% | 1,308-1,810ms |

**+4.5 points of top-1 on the proposer, and nothing whatever on the adjudicated system.** Not a
regression, not noise — the same four cases, the same three correct, verified case by case
rather than inferred from matching totals.

That is not a contradiction, it is what the escalation rate means. The adjudicated system
answers 2% of cases and declines the rest, and whether the GRI resolve a case does not depend
on how the five candidates are *ordered*. So reranking improves the ranked shortlist handed to a
human on the 97.5% that escalate, which is the system's actual output, and leaves the rules'
decisions untouched. For a stage-2 component that is the behaviour you want. It is also the
reason the proposer number must never be quoted on its own.

### It is not adopted, and the cost is why

`HierarchicalClassifier` takes `reranker=None` by default and nothing in the shipped pipeline
passes one. On this evidence that stays, because the price is not only latency:

- **Roughly eighty times slower, and the range is the honest form.** Against a 22.8ms pipeline,
  the cross-encoder measured 1,729ms and 1,948ms on the proposer across two runs, and 1,308ms
  and 1,810ms adjudicated — single runs, not the repeated sweep the other latency figures get,
  so the spread is a floor on the uncertainty rather than a measurement of it. A third run under
  memory pressure gave 3,188ms. Quoting any one of these as *the* number would be quoting
  whichever suited the argument; see the latency section for what happened when this project
  did that.
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
| **50** | **89.4%** | **1,729-1,948ms, measured** |
| 100 | 95.7% | 2,620ms, one run |
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
| adjudicated, test split | **harmful** — decided 4 → 6 while right on decided fell 3/4 → 1/6 |

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
python -u scripts/measure_caching.py
```

```bash
python -u scripts/measure_visual_ceiling.py heldout-2000
```

```bash
python scripts/measure_rerank.py test-200 cross-encoder-50
```

```bash
python scripts/measure_calibration.py
```

```bash
python scripts/measure_latency.py
```

```bash
python scripts/measure_chunking.py
```

The reranker takes the process from 472MB to 2.7GB of working set, so run one of these at a
time — two together do not fit on a 16GB machine, and the second dies with `bad allocation`
from inside onnxruntime, which names memory nowhere.

## Pricing each mechanism, and where the errors actually are

Three measurements the spec asks for and the project had not made: what each rule mechanism is
worth, what the pipeline costs at p50 and p95 rather than on average, and which chapters it
fails in. Produced by `scripts/run_ablations.py`, `scripts/measure_latency.py` and the
`per_chapter` block every eval report already carried and nobody had read.

### Ablations 6 to 8: what each mechanism is worth

The spec lists "+ note-chain" and "+ KAG rule evaluation" as steps upward from a baseline. They
are built here as subtractions from the full engine instead, because adding a mechanism to a
stripped engine measures it against a configuration nobody would ship, while removing it answers
the question an operator has: what do I lose if this is off?

| configuration | test: decided / right | held-out: decided / right |
| --- | ---: | ---: |
| full engine (5 · hybrid + GRI) | 4 / 75.0% | 17 / 29.4% |
| 6 · without the note-chain | 4 / 75.0% | 17 / 29.4% |
| 7 · without KAG conditions | 4 / 75.0% | 17 / 29.4% |
| 8 · without Section XI Note 2(A) | **0 / —** | **1 / 0.0%** |

**The adjudication rests almost entirely on one note.** Remove Section XI Note 2(A) — the
predominant-weight test — and the engine answers *one* case in 2,200, by GRI 2(a), and gets it
wrong: a ruling on bicycle parts answered as copper pipe fittings, `7412.20.0015`, which the
full engine also decides. Everything else is built, tested and almost unexercised. Measured on
the held-out 2,000, the deciding rule is Note 2(A) for 16 of the 17 answers and GRI 2(a) for the
other one; the note-chain, the KAG threshold conditions and the narrow 3(a) decide nothing.

Nothing is being shadowed by rule order, which was the obvious alternative reading. The
conditions rule runs *before* Note 2(A) and GRI 3(a) runs after it, and with 2(A) switched off
GRI 3(a) is reached 105 times instead of 82 — the extra 23 being exactly 2(A)'s cases — and
resolves none of them. `_most_specific` needs exactly one non-residual candidate out of five,
and on this corpus that never happens.

Two of those three rows were predicted before the run and are not failures:

- **The note-chain cannot move a decision, by design.** `_excluded` reports the exclusions in
  scope rather than acting on them, because dropping a candidate on an unevaluated condition is
  the confident-but-wrong behaviour the project exists to avoid. So its row belongs in the
  explanation scores and not in the accuracy table, and
  `test_switching_off_the_note_chain_removes_citations_not_decisions` pins exactly that: same
  code, same rule path, no citations.
- **The KAG layer's reach was already measured at zero** and the ablation confirms it from the
  other direction. Of 21 test cases where a candidate carries an evaluable threshold, only four
  ask about a material the description quantifies. It still decides nothing — ablations 5 and 7
  remain identical at 17 covered and 29.41%. An attempt to widen its reach is set out below; it
  was reverted, and the ablation row is unchanged because of that.

#### Making a threshold evaluable: built, measured, reverted

A pullover of "60% cotton and 40% modal" was classified 6110.20.1010, whose own chain reads "Of
cotton — **Containing 36 percent or more by weight of flax fibers**". The goods contain no flax.
The facts carried no `flax` key, so the threshold read as *untested* rather than false and the
provision survived by silence — the cardigan failure in a new place, and found by reading stage
4's prose rather than by any test.

The stated shares sum to 100, so the composition is **closed**: nothing is left for flax, and
the condition is evaluable and false. `composition_is_closed` accepts 99–101, which admits a
shipper's rounding and rejects the case that matters — "a 100% nylon lace dress with a 100%
polyester slip" sums to 200, states no whole-garment composition, and must not be read as
excluding anything. The inference is confined to Section XI, and that is a legal limit rather
than a cautious one: Note 2 makes textile material by weight *the* classification dimension, so
a complete fibre list excludes every other fibre, whereas "over 5.5 percent by weight of
butterfat" measures a constituent *of* the components and a complete component list says nothing
about it.

**It broke more than it fixed, and the reason is the oldest finding in this project.** Two
independent measurements killed it.

*It promotes whatever is left standing.* Used to eliminate candidates, it looked like a clear
win — held-out coverage rose 17 → 28 and accuracy on decided rose 29.41% → 35.71%, both the
right direction, with the mechanism's own ablation row confirming the gain belonged to it.
Listing the cases rather than totalling them showed **11 further decisions of which 2 were
right**:

```
ladies' sleepwear, 65% polyester  ->  "pajamas ... Of wool or fine animal hair"
women's poncho, 100% polyester    ->  "(cashmere) goats - Wholly of cashmere - Vests"
women's knit pants, 92% cotton    ->  6115.95.6000, hosiery
```

Making thresholds evaluable means more candidates fail outright, so the one left standing is
more often **the provision whose text carries no percentage to test**. "Wholly of cashmere"
states no number, is never tested, and wins by silence. 100% polyester classified as cashmere
with a citation attached is the exact failure this project exists to prevent, and the totals
endorsed it.

*And "not named" is not "absent".* This is the decisive one, and it holds however carefully the
inference is placed. A shipper writes **spandex** or **elastane** where the schedule writes
"elastomeric yarn", so `_share_of` finds no overlap for goods of "95% cotton and 5% elastane"
and the inference declares "Containing 5 percent or more by weight of elastomeric yarn" false —
against gold **6104.62.20.06**, which *is* that provision. Over the train split it contradicts
the provision the ruling calls right in **103 rows, 4.8% of those with a closed composition**:

| attribute of the falsely-failed condition | rows |
| --- | ---: |
| `elastomeric yarn` | 71 |
| `wool` | 24 |
| `flax fibers` | 4 |
| `silk`, `cotton` | 4 |

It costs nothing today only because stage 2 proposes the gold provision in none of those cases —
so it is a defect that fires precisely as retrieval improves, which is this project's main open
lever. The `None` return it replaced is what kept the vocabulary gap harmless.

So **one vocabulary gap has now defeated four mechanisms**: retrieval ("cardigan" is nowhere in
the schedule), the GRI 6 material-terms rule ("acrylic" against "Of man-made fibers"), the KAG
threshold layer (the numbers are absent), and this. Deciding that a material is genuinely absent
needs a synonym map from the schedule's vocabulary to a shipper's, and that map is measured
elsewhere as underivable — the notes supply 12.7% of the names descriptions use, and asking a
model for the category instead scores 76.5% while handing it Section XI Note 2(A) to apply.
Reverted, and recorded as a limit of the data.

**What survived.** `_back_off` stays, because it is sound without the inference and guards a real
path: `_condition_rule` sets aside an elimination that would empty the candidate list, and the
walk can then decide a candidate that *is* demonstrably contradicted. A suit of 40 percent silk
decided under "Containing 70 percent or more by weight of silk" misses by 30 points, and the
subheading above states no threshold and still holds — the six-digit and ten-digit answers are
different claims. Backing off does not enter `rule_path`: it is GRI 6 declining to go deeper, and
the subheading it lands on was not itself compared with its siblings, which the trace says rather
than hides.

Alongside it, a lone survivor must now have *passed* a condition rather than merely not failed
one, since "nothing tested" covered both an untestable condition and a provision stating none; a
residual "Other" is exempt, because "Other" is defined by what its siblings exclude and a suit of
40 percent silk belongs there once its "70 percent or more" sibling is eliminated. And
`_back_off` is gated on the `conditions` mechanism, or ablation 7 would price a configuration it
does not describe — caught by that ablation's own test.

**Every published number is unchanged**: all four adjudicated configurations, both eval sets,
coverage and accuracy alike. Both survivors fire zero times on the current corpus, so they are
backstops rather than contributors, and they are reported as that.

The third was not predicted and is the useful one. An engine whose coverage depends on a single
textile note is not a general GRI engine yet, whatever its rule paths say — and that is an
argument about stage 1, not about the rules. The mechanisms are starved, not wrong.

### Latency: p50, p95, and three attempts to measure them honestly

Three fresh processes per configuration, 20 warm-up cases discarded and not re-timed, 180 timed
cases each, with the spread of the three medians shown because it is what makes the differences
readable:

| configuration | p50 (spread) | p95 | mean |
| --- | ---: | ---: | ---: |
| 1 · vector-only | **9.4ms** (9.3–9.6) | 16.2ms | 10.2ms |
| 2 · + hierarchical traversal | 15.4ms (14.6–15.7) | 25.1ms | 16.0ms |
| 4 · + hybrid retrieval | 21.7ms (20.8–23.0) | 37.9ms | 23.5ms |
| 5 · + the GRI engine | **22.8ms** (22.0–23.9) | 39.4ms | 24.0ms |
| 6 · without the note-chain | 22.4ms (22.4–23.6) | 42.3ms | 24.4ms |
| 7 · without KAG conditions | 21.7ms (21.2–22.4) | 39.9ms | 23.9ms |

**The whole pipeline is 22.8ms at the median.** Traversal adds 6ms over flat search, hybrid
retrieval adds another 6.3ms, and the rule engine adds 1.1ms — which is inside the ±1ms spread,
so the honest statement is that the deterministic stage is free. Rows 6 and 7 sit inside that
same spread, so neither the note-chain nor the KAG conditions has a measurable cost either.
The p95 is about 1.7 times the p50 in every row.

#### What this number cost to get right

This table is the third attempt, and the first two were published. They are kept here because
the project's rule is that a difference has to beat its own noise, and latency is where that
rule was hardest to follow.

**The first** took the percentiles from the ablation sweep: eight configurations back to back in
one process, which gave configuration 6 a p50 of 102ms and configuration 7 a p50 of 17ms —
two configurations sharing one retrieval object and differing only in the rule engine. I
diagnosed it as position in a long run, "after about 13,000 classifications something changes",
and wrote that into a script docstring.

**The second** gave each configuration its own interpreter, which is what that diagnosis
implied. Its own output refuted it: the same configuration in two fresh processes forty minutes
apart differed by six times. A fresh interpreter does not isolate a measurement from the machine
it runs on.

**The diagnosis was also backwards.** The slow numbers were the artefact, not the fast ones.
Configuration 1 is a single 27,000 × 384 matmul and takes 9.4ms; the sweep that reported it at
42ms was measuring whole-machine contention from its very first classification, before any
long-run effect could apply. So the earlier published figures — a p95 of 6.7 times the p50,
hybrid retrieval costing 70ms, the rule engine costing 8ms — were all contention. The real
figures are 1.7 times, 6.3ms, and nothing measurable.

**What the third attempt does differently** is repeat each configuration three times, publish
the spread, and check the cheapest configuration against a floor: one matmul cannot take 25ms,
so if it does, the machine is busy and the sweep is discarded rather than published. That check
earned itself on its first use — it refused a sweep taken while a calibration run was still
going, where configuration 1 measured 38.9ms.

### Error analysis by chapter, and a claim in the spec that does not hold

Held-out 2,000, top-1 at six digits. Only 10 of 41 chapters reach n = 20, and those ten carry
1,843 of the 2,000 cases, so the per-chapter view is really a view of ten chapters.

| | n | top1@6 |
| --- | ---: | ---: |
| chapter 82 · tools | 136 | **6.6%** |
| chapter 85 · electrical machinery | 171 | 9.9% |
| chapter 90 · optical and measuring instruments | 156 | 10.3% |
| chapter 39 · plastics | 268 | 10.4% |
| chapter 84 · machinery | 379 | 13.7% |
| chapter 29 · organic chemicals | 143 | 14.0% |
| chapter 61 · knitted apparel | 353 | 17.8% |
| chapter 73 · articles of iron or steel | 178 | **22.5%** |

Grouped the way the spec groups them:

| | n | top1@6 |
| --- | ---: | ---: |
| chemicals (28–38) | 189 | 12.2% |
| machinery (84–85) | 550 | 12.5% |
| base metals (72–83) | 341 | 16.7% |
| **textiles (50–63)** | 374 | **17.6%** |
| whole set | 2,000 | 13.8% |

The spec says "chemicals, textiles and machinery are notoriously contested". Two of the three
hold — chemicals and machinery sit below the whole-set rate. **Textiles is the strongest large
group in the project**, four points above it, and that is not a property of the nomenclature.
It is a property of this build: the only live rule mechanism is a Section XI textile note, the
running example throughout is a knitted t-shirt, and the KAG threshold conditions were written
against chapter 50–63 weight tests. The system has been developed against textiles and reports
textiles as its best subject. Worth stating plainly, because it is the kind of result that
flatters a project if nobody names the cause.

The weak chapters have something in common that points the same way as everything else here.
Tools, electrical machinery, instruments and plastics are classified on **function and
construction** — what the thing does, how it is assembled, what it is for. The one typed
attribute stage 1 extracts is material composition. The system does best where composition
decides and worst where it does not, which is the same finding the KAG funnel and the mechanism
ablations arrived at from two other directions.

### The chunking ablation, and the one spec claim that held exactly

The spec asks for four chunking strategies — fixed-size, structural, structural plus
contextual, small-to-big — measured on the note-dependent slice only. Three of the four do not
apply to this architecture, and saying why is part of the answer rather than a way around it:

- **The schedule is not chunked.** It is parsed into a tree, one node per tariff line, because
  chunking by token count would destroy the hierarchy and the hierarchy is the legal structure.
  There is no chunk size to vary.
- **The notes are not retrieved.** They are parsed one provision per chunk and applied by rule —
  the exclusion graph and Section XI Note 2(A). Nothing fetches a note by similarity, so
  varying a note chunk size compares two things the pipeline does not do.
- **Small-to-big is already here, and already measured.** `lexical_documents()` indexes every
  heading with the vocabulary of every line beneath it and returns the heading: retrieve on the
  big document, classify the small one. `MAX_DESCENDANT_CHARS` is its chunk-size knob, and the
  retrieval section above reports what removing the cap does (fused heading recall@5 falls 0.5
  points). Listing it as inapplicable was wrong — it is implemented, and the measurement exists
  under another heading.

What *is* a chunking decision here, and the spec names it in the same breath, is **inherited
context**: a tariff line reads "Of cotton" alone and is meaningless embedded that way, so every
node is embedded with its full chain. The spec states flatly that without it "retrieval dies".
That had never been measured. Rebuilding the same 30,000-node index from each line's own
description alone:

| | test top1@6 | test top5@6 | held-out top1@6 | held-out top5@6 |
| --- | ---: | ---: | ---: | ---: |
| inherited chain | **15.00%** | **22.50%** | **13.75%** | **24.70%** |
| own text only | 7.50% | 17.00% | 7.05% | 14.70% |

**Top-1 at six digits halves**: 15.00% to 7.50% on the test split and 13.75% to 7.05% on the
held-out 2,000, with top-5 falling alongside. At ten digits the fall is much smaller — 3.05% to
2.03% and 2.98% to 2.37%, so 20 to 33% rather than 50% — which is why the headline names the
digit level. Six and ten digits are different claims and the project does not merge them.

Both arms are built from the same 29,861-provision tree. They were not at first: the cached
index predated a parser fix and held 30,195 provisions, so "the same index with and without
context" compared two different schedules across a 342-provision gap. `VectorIndex.load` now
takes the candidate list it is being used for and refuses a cached index that does not match.

It is also the only claim from the source documents that this week's measurements confirmed
without qualification. The spec's "chemicals, textiles and machinery are contested" is two
thirds right and backwards on textiles here; its escalation-calibration test was missing and
turned out positive; its chunking ablation mostly does not apply. This one was simply correct.

## Escalation calibration: the refusal earns its keep, and most of what looks like signal is not

The engine answers 2.5% of the test split at 60%. That is one operating point with nothing to
turn, so this section asks two things: is the refusal worth having, and could a confidence
threshold do the job instead? Produced by `scripts/measure_calibration.py`.

The first draft of this section got the headline wrong in a way worth keeping on the record,
and the correction is at the end.

### The spec's own test: the refusal is not too cautious

`docs/HS-PROJECT-CONTEXT.md` defines escalation calibration directly — force answers on the
declined cases, and if accuracy is close to the auto-classified slice, the threshold is too
cautious. That test is one line and it is the clearest result here:

| | answered by the rules | forced on the declined | ratio |
| --- | ---: | ---: | ---: |
| test split | **75.0%** (4 cases, 95% low 30.1%) | 15.3% (196 cases, 95% low 10.9%) | 0.20 |
| held-out 2,000 | **29.4%** (17 cases, 95% low 13.3%) | 13.7% (1,983 cases, 95% low 12.2%) | 0.46 |

The cases the rules answer are two to five times as accurate as the ones they decline would
have been. **Only the test split establishes it.** There the intervals are clear of each other,
30.1% against a forced upper bound of about 21%. On the held-out 2,000 the rules answer 17
cases, and a 95% interval on 17 reaches down to 13.3% — below the forced-answer rate of 13.7% —
so that row is consistent with the refusal selecting and also with it not. Seventeen cases
cannot settle it. What can be said is that the earlier claim here, that the refusal was *not*
selecting, is contradicted on the set where the evidence is clean.

### The curve: one signal survives, and it is the margin

Accuracy of the proposer's top candidate over the most-confident slice, with the size of every
slice. Held-out 2,000, base rate 13.75%:

| requested coverage | 1% | 2.5% | 5% | 10% | 25% | 100% |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `margin_to_second` | **50.0%** (20) | **50.0%** (50) | 49.0% (100) | 41.0% (200) | 26.8% (500) | 13.8% |
| `top1_similarity` | 40.0% (20) | 40.0% (50) | 33.0% (100) | 32.0% (200) | 25.6% (500) | 13.8% |
| `description_terms` | 10.0% (20) | 18.9% (53) | 19.8% (111) | 19.7% (203) | 15.7% (534) | 13.8% |
| `has_composition` | 20.1% (239) | 20.1% (239) | 20.1% (239) | 20.1% (239) | 13.8% (2000) | 13.8% |
| `top_heading_share_of_20` | 17.6% (1255) | 17.6% (1255) | 17.6% (1255) | 17.6% (1255) | 17.6% (1255) | 13.8% |

**The gap between the top candidate and the runner-up is the one robust signal** — 41.0% over
200 cases, a 95% lower bound of 34.4% against a 13.75% base rate, and it replicates on the test
split (60.0% over 20 cases). It is the only signal that survives the gate on *both* sets.

Three measurement guards are doing visible work in that table, and each of them was added
because the number it guards against had already been believed somewhere in this project.

- **A requested coverage that falls inside a run of equal scores widens, and says so.**
  `has_composition` takes two values, so its smallest possible slice is every case carrying a
  composition: 239 of 2,000. There is no 1% slice to have. Cutting an arbitrary 20 out of the
  tied tier would let the sort order decide which cases count as confident, which is how the
  lexical reranker lost real accuracy earlier in this project.
- **`top_heading_share_of_20` is capped by a tuning constant, not by the goods.** The beam keeps
  at most `per_heading = 5` walks per heading, so at most 5 of the top 20 can share the winning
  heading and the signal takes exactly five values with a maximum of 0.25. Its floor of 1,255
  cases is an artefact of the beam. An earlier draft explained the floor as "agreement takes too
  few distinct values", which is true and not the reason.
- **"Informative" is a Wilson lower bound above the base rate, not a lift above a round
  number.** A lift threshold has no `n` in it: on the test split `top1_similarity` scores five
  correct of twenty, a lift of 1.67 that a permutation test puts at p = 0.16, and a 1.2 gate let
  it through. Under the interval gate the test split keeps only `margin_to_second`. On the
  held-out 2,000 all five signals clear it, including `has_composition` at 1.46 and
  `description_terms` at 1.43 — those are real at n ≈ 200–240, and an earlier draft of this
  section dismissed them as telling you nothing.

### What the rules select for, which is not confidence

Sizing each signal's slice to the number of cases the rules answered, and scoring each mechanism
on what it returns — the engine on the code it decided, a threshold on the top candidate:

| held-out 2,000 | n | accuracy | overlap with the rules | expected by chance |
| --- | ---: | ---: | ---: | ---: |
| the rules | 17 | 29.4% | — | — |
| `margin_to_second` | 17 | 52.9% | 0 cases | 0.14 cases |
| `has_composition` (whole tier) | 239 | — | nearly all of them | — |

**The margin's zero overlap with the rules is a null result, not a finding.** Two subsets of 17
drawn from 2,000 are expected to share 0.14 cases, so observing zero is the ordinary outcome
whether or not the mechanisms are related. A rank test on the earlier, larger answered set
agreed: the rules' cases sat at mean rank 1,071 under the margin against a null of 999.5
(p = 0.73). They are not measurably related in either direction.

What *is* measurable is the last row. **Nearly every case the rules answer lies inside the
239-case tier where a composition was extractable** — it was 23 of 24 before a fact-extraction
fix reduced the answered set to 17 — which is far beyond chance. That is the real selection the refusal performs: it answers cases where stage 1
found something typed to reason over, which is exactly what the KAG funnel predicted and the
opposite of a confidence threshold's criterion. The two mechanisms may well be complementary —
a threshold bets on the top candidate, the rules bet on having facts — but on this evidence
"complementary" is a hypothesis with a plausible mechanism, not a measured result.

A note on why the overlap column reads `tie-cut` for three signals: matching the rules' coverage
exactly means taking exactly as many cases as the rules answered, which for a two-valued
signal cuts a tier of 239. The
overlap is then an average over an arbitrary subset of equal scores, so it is withheld. A
published `0.083` for `has_composition` came from exactly that cut — the one signal that
predicts the rules' set best was reported as the least related.

### What is not claimed

**That the rules earn their keep by overriding a wrong top candidate.** On the test split the
engine answers four cases, gets three right, and the top candidate on those same four is right
*none* of the time — which looks decisive and is not: under a 15% base rate, zero-of-four
happens 52% of the time. On the held-out 2,000 the direction nearly reverses: the top candidate
is right on 4 of the 17 (23.5%), the rules on 5 (29.4%). One case in 17. The figure is now emitted as
`rules_operating_point.top1_accuracy_on_the_same_cases` so the claim is reproducible from the
artefact rather than quoted from a draft.

**That a threshold should replace the refusal.** The rules return a citation; a margin returns a
number, and a customs decision needs the former. The design the evidence supports is to answer
both slices and label which mechanism answered — but the union figures are policy-dependent
(`either_accuracy` prefers the engine's code where both fire, which is a choice, declared in the
field's own comment), and the slices are not measurably related, so this is a week-4 proposal
rather than a result.

**That these five signals are the best available.** `margin_to_second` and `top1_similarity` are
nearly independent (Spearman 0.25) and their combination was not tried. Two variants were and
are recorded as negative: normalising the margin by the top similarity scores slightly *worse*
(2.91 against 2.95 on held-out), and ranking by the number of distinct candidate prefixes is
unstable across sets (1.30 held-out, 0.64 on test).

### The correction

The first version of this section led with "the overlap is zero on both sets — that is not a
coincidence, and it explains the whole section", and built a mechanism on top of it: a threshold
picks cases where the top candidate looks good, the rules pick cases where it is wrong and
override it.

Both halves were wrong. The overlap is what chance gives, the override story rests on five
cases and does not survive the larger set, and the signal the draft said "tells you nothing
about being right" is the one that actually predicts which cases the rules answer. The
arithmetic was right throughout; what failed was asking whether a difference was larger than
the noise around it — the same failure as the +4.48ms tracing overhead and the retracted
reranker finding, and the third time in one week. The guards now in `eval/calibration.py` are
the response: a denominator on every number, a chance baseline beside every overlap, and an
interval rather than a ratio deciding what counts as a signal.

## Stage 1: a model reading the description, and the finding that it does not help

Four measurements pointed here. The rule engine answers a few per cent of cases and nearly all
of its answers come from one note that needs a composition by weight; the regex finds one in 24
of 200 descriptions; the KAG threshold layer decides nothing because only 4 of 21 evaluable
thresholds ask about a material the description quantifies; and the error analysis fails worst
in chapters decided by function rather than material. All four said `Facts` was empty and that
filling it was the lever.

It is not. Measured on the test split through `gpt-4o-mini`, against the regex it replaces:

| | with a percentage | with a mixture | engine decided | right | on decided |
| --- | ---: | ---: | ---: | ---: | ---: |
| regex | 24/200 (12.0%) | 13/200 (6.5%) | 5 | 3 | 60.0% |
| **gpt-4o-mini** | 25/200 (12.5%) | 12/200 (6.0%) | **3** | **3** | **100.0%** |

**On the attribute the rules need, a model is indistinguishable from a regex** — one case either
way. The reason is not a weak model or a weak prompt: Section XI Note 2(A) turns on which
material predominates *by weight*, and a weight percentage can only be extracted from a
description that states one. Nothing can read "60%" out of "a cotton t-shirt". The prediction
that this would happen was written down before the run, and the consequence is the useful part:
**the binding constraint is the rules, not the facts.**

### What the model is better at

Not finding percentages — being right, and seeing more.

It found a **material name in 111 of 200** descriptions and a **form in 54**, where the regex
finds neither. Those are real attributes that no rule currently consumes.

And it does not invent. The engine went from five answers at 60% to three at 100%: the same
three correct answers, with two wrong ones removed. Listing the cases rather than comparing the
totals showed why, and it was a bug in the thing being replaced:

```
"a men's 100% cotton, jersey knit pullover with a V-neckline"
  regex facts: {'cotton': 100.0, 's': 100.0}   ->  decided 6110.90.9014   WRONG
  model facts: {'cotton': 100.0}               ->  escalate
```

The pattern that reads "cotton 60%" also reads **"s 100%" out of the possessive**. A
single-material garment therefore carried two materials, counted as a mixture, and Note 2(A)
fired on it. Two of the engine's five answers on the test split were wrong for that reason, and
`"a woman's 100% nylon lace dress"` produced `{'nylon lace', 'polyester knit slip', 's'}`.
Materials must now be at least two characters, with
`test_a_possessive_does_not_become_a_material` to hold it.

Across the whole project that fix costs coverage and buys grounding: the adjudicated engine goes
from 5 answers at 60.0% to 4 at 75.0% on the test split, and from 24 at 29.2% to 17 at 29.4%
held-out. Seven held-out answers disappeared, of which five were wrong and **two were right by
accident** — a fabricated mixture firing a legal rule and landing correctly.

### The guard that matters, and what it caught

A percentage the description does not contain is worse than a missing one, because Note 2(A)
will decide a case on it and attach a citation. So every number the model returns is checked
against the source text before it reaches `Facts`, and one that is not there is dropped and
recorded.

It fired **7 times in 200 cases**, and every instance was the same shape:

```
high density polyethylene=100 does not appear in the description
steel=100 / carbon steel=100 / brass=100 does not appear in the description
```

The model inferred "100%" for goods described as made of one material. That is a reasonable
inference and a fabricated fact, and the threshold rules would have evaluated it as though the
description had said so. The match is bounded by non-digits, because a substring search finds
the "60" inside "160 g/m2".

### Cost

| | |
| --- | ---: |
| 200 classifications | **$0.0115** |
| per 1,000 classifications | **$0.06** |
| tokens | 53,751 |

Real spend from the ledger, not an estimate — and the estimate is worth recording as a
cautionary number. This README previously projected $3.50 per 1,000 on `gpt-4o` and about $0.21
on `gpt-4o-mini`, from an instruction prompt assumed to be ~900 tokens. The prompt is ~200
tokens and a call costs about $0.00006, so the projection was high by a factor of three even
after correcting for the model. `obs/cost.py` carries the published OpenAI rates and
`hsclass config` refuses to start a run against a model it has no price for.

### The architecture is intact, and asserted

The model never sees a tariff code. It is given a description and a schema and returns
attributes; stage 3 applies the rules and is unchanged. `test_the_model_is_never_asked_for_a_code`
checks the prompt for "hts", "heading", "subheading" and "classify it as" and fails if any
appears. The transport is a `Protocol`, so every test runs against a stub with no key and no
network, including the whole verification guard.

### The mechanism those named materials made possible, and the wall it hit

The 111 named materials looked like the opening. The schedule is full of subheadings reading
"Of cotton", "Of wool or fine animal hair", "Of other textile materials", and choosing between
those is GRI 6 — the terms of the subheadings — needing a *name* rather than a weight. It is
deterministic, it uses the fact the model reliably extracts, and it was the first mechanism here
whose input was not already exhausted.

It was built, with four guards each from a defect this engine has already had: one heading only,
exactly one match, never select a residual, every competitor considered. Then a fifth, found by
a test while building it — a subheading stating a threshold is not one that *names* a material,
and without that exclusion a suit of 40% silk was decided into "Containing 70 percent or more by
weight of silk" on the word "silk". Each rule owns its own kind of term.

**It reaches 36 of 200 cases and decides none of them.** The funnel, on the test split:

| gate | n |
| --- | ---: |
| cases | 200 |
| a material is named | 139 |
| the candidates share one heading | 55 |
| both | **36** |
| of those, exactly one candidate names the material | **0** |

Never two matches — always zero. The prediction recorded before the run was 10 to 30, and it
was wrong for a reason already written down two sections above: **the schedule names material
categories and descriptions name specific materials.** A sweater of "90% acrylic and 4%
spandex" faces "Of man-made fibers" and "Of other textile materials", and no string match
connects acrylic to man-made. Retrieval hit the identical wall, where "cardigan" appears nowhere
in a nomenclature that reads "Sweaters, pullovers ... and similar articles". Three mechanisms,
one vocabulary gap.

**Two ways to bridge it were measured, and both are closed.**

*Deriving the map from the notes.* Chapter 54 note 1 defines "synthetic" and "artificial" in
relation to fibers, naming polyamides, polyesters, polyolefins, polyurethanes, cupro, viscose
rayon and cellulose acetate, and declares those meanings hold throughout the schedule. The
schedule adds one usable equation of its own, "Of nylon or other polyamides". That is citable
law and it is already parsed — and it covers **29 of 228 material mentions, 12.7%**. The names
descriptions actually use are `polyester` (16), `cotton` (12), `spandex` (8), `nylon` (8),
`acrylic` (5), then a tail of 150 distinct names. Spandex and acrylic are derivable from
nothing in the schedule or its notes. Mining the schedule's own hierarchy for more — terms
sitting beneath a heading that says "synthetic" — yields "acrylic or modacrylic" and "aramids"
alongside "backpacking tents" and "diamonds", because unrelated breakouts sit under the same
parent. A map built from the clean sources would act on an eighth of the vocabulary, inside the
36 cases that reach the rule. Not worth building, and that was established before building it.

*Asking the model for the category instead of the material.* "Man-made fibres" rather than
"acrylic". Ground truth needs no judgement here — the gold code's own chain states the category
the schedule assigns — and the model scores **26 of 34, 76.5%**. The accuracy is not the
reason to decline it. The errors are:

```
"70% polyester and 30% wool"       said "wool"    schedule says "man-made fibres"
"ramie, wool, nylon, and acrylic"  said "wool"    schedule says "other textile materials"
"hemp and cotton"                  said "cotton"  schedule says "other textile materials"
```

**The schedule's category is not a property of the material. It is the outcome of applying
Section XI Note 2(A).** Polyester at 70% makes the garment "man-made fibres" because
predominance by weight decides; four materials with none predominating land in "other textile
materials"; hemp with cotton goes to "other" for the same reason. The model identified the
materials correctly every time and got the category wrong because the category is a legal
conclusion, not a fact about the goods.

So asking for it hands Note 2(A) to the model — the one rule this engine's coverage rests on,
and the thing the architecture exists to keep deterministic. The error pattern is the evidence
that it happens. Declined on that ground rather than on the 76.5%.

What is left is a curated material taxonomy from outside the schedule, which would be
uncitable, in a system whose output is a citation. The material-terms rule therefore ships
correct, guarded, ablatable and **inert**, and the vocabulary gap is recorded as a limit of the
data rather than a gap in the implementation.

## Multimodal: the ceiling on a product image, measured before sourcing one

Week 3 lists "multimodal on thin descriptions". The labelled data is text — CROSS letters
describe the photographs submitted to the officer without embedding them — so a product-image
slice has to be **constructed**, at a budget of roughly three days. Two sources were checked
before spending any of it.

**HSCodeComp has no image field.** It is the one expert-annotated alternative corpus — 632
products, 27 chapters, >98% inter-annotator agreement, and a published gap worth knowing: the
best agent reaches 46.8% against 95.0% for human experts. Its columns are `product_name`,
`product_attributes`, `price`, `currency_code`, `cate_lv1..5_desc` and `hs_code`. The paper
writes the input tuple as `(t, A, c, i, p, u, r)` and its strongest baseline is a vision-capable
model, so an image field looked likely; there is none in the released data, and the vision was
for reading web pages during search. **GLAMI-1M** remains viable — 1M fashion image–text pairs
over 191 human-labelled classes, 11 GB — but it pairs at *class* level, giving an image of *a*
pullover rather than *the* pullover in the ruling.

So the question worth answering first is how much an image could possibly buy. For every case
the pipeline gets wrong, `scripts/measure_visual_ceiling.py` finds the first segment where the
gold provision's chain parts from the top-1 candidate's and asks what the system needed to know
there. Section and chapter titles are dropped before comparing, because GRI 1 gives them no
legal force — leaving them in made a cross-chapter error report its divergence as "Chapter 29",
which names no fact at all.

| at the point the chains diverge | test-200 | held-out 2,000 | |
| --- | ---: | ---: | --- |
| **the text already said it** | 61.0% | **56.7%** | an image adds nothing |
| absent, and not visible | 15.8% | 20.6% | only the importer can supply it |
| **absent, and visible** | **6.2%** | **5.9%** | the only bucket an image serves |
| ambiguous or unclassified | 17.1% | 16.8% | |
| *errors with a comparable chain* | *146* | *1,541* | |

**An image's ceiling is about 6% of errors**, and it is an upper bound twice over: only the
first divergence is read, so settling it would not guarantee the rest of the chain resolves, and
a class-level image depicts the class rather than the goods. The stem match that decides
"already said it" is also imperfect — "Handsaws" does not match "Jab Saw" — which pushes cases
out of that bucket and into "unclassified", so the bias runs *in favour* of images and 6% is
generous rather than flattering to the conclusion.

Against that, **57–61% of errors are cases where the discriminating words are already in the
description and retrieval missed them anyway**: "Cast fittings", for goods described as "cast
carbon steel fittings made to ASTM A216". The lever is ranking, not another input modality —
which extends the stage 1 finding rather than contradicting it. The constraint was never the
facts, and it is not the facts' *format* either.

What is **not** claimed: that images are worthless in production. Invoice descriptions really
are thinner than the ruling prose ATLAS is built from, so this corpus understates the case the
spec had in mind. Bounding that needs no images at all — truncate the descriptions to
invoice-like thinness and measure what is lost, which is the honest next experiment and costs
nothing.

Two consistency checks fell out of the run. Top-1 at six digits reproduces the published 15.00%
and 13.75% exactly. And **184 of 2,000 held-out gold lines, 9.2%, do not exist in revision
2026HTSRev21 at all** — counted and reported rather than dropped, which is the schedule-version
problem appearing inside the evaluation data itself.

## CAG: the gate is closed by showing the instrument has no subject

Week 3's gate asks for "CAG latency and cost delta measured, accuracy confirmed unchanged". The
delta is **zero on all three**, and the reason is not that the saving would be small.

Cache-augmented generation keeps a large static prefix — the General Rules and the chapter notes
— resident in a model's context so that repeated classifications do not re-send it. **In this
architecture those texts are never sent to a model at all.** Stage 3 is deterministic and reads
the notes from a parsed structure; what reaches a model is the *citation* the engine recorded,
"Section XI Note 2(A)", never the provision's words.

Measured by `scripts/measure_caching.py`, which sends nothing and costs nothing:

| | |
| --- | ---: |
| binding note provisions a CAG design would keep resident | 2,030 |
| that corpus, in characters | 587,316 |
| …as tokens | **~146,829** *(estimated, chars/4)* |
| what stage 1 actually sends, mean input tokens per call | **230.0** |
| what stage 4 actually sends, mean input tokens per call | **261.5** |
| `cache_read_tokens` over 400 real calls | **0** |
| OpenAI's minimum cacheable prefix | 1,024 tokens |

Two exact facts settle it. The first is that `cache_read_tokens` — recorded on every call by
`obs/cost.py`, from the API's own usage field rather than estimated — is **zero across 400 real
calls**, because every prompt is a quarter of the length a prefix needs before it is eligible to
be cached at all. The second is the ratio: a prompt-based engine would carry roughly **560 times**
the context this one sends per classification. The corpus token count is an estimate and labelled
as one (`tiktoken` is not a dependency; this project runs ONNX, not the OpenAI stack), but the
numbers the gate turns on are not estimates.

So the honest entry is not "CAG was skipped" and not "CAG saved nothing". It is that **the
deterministic stage 3 already achieves what CAG is for**, by a different route and more
completely: the notes are not re-sent because they are never sent. `test_the_prompt_carries_note_citations_and_not_note_text`
pins that, so if a future change starts putting provision text into a prompt, the CAG question
reopens with a failing test rather than silently.

The adjacent claim is worth separating, because it is weaker. Token cost here is already
negligible — $0.06 per 1,000 extractions and $0.078 per 1,000 explanations — so even if there
were a prefix to cache, optimising it would be optimising the wrong number while the engine
answers 2.5% of cases. That argument is about priority. The one above is about applicability, and
it is the one that closes the gate.

## Observability: the costs that are not tokens

Every classification can now emit a trace an auditor can reconstruct it from, and the budget is
enforced rather than configured. This existed as two unused modules from week 1; wiring it in
was prompted by a failure that a dollar ceiling could not have seen.

### Why a cost ledger denominated in dollars was not enough

Adding the cross-encoder reranker changed the pipeline's cost like this:

| | without | with |
| --- | ---: | ---: |
| tokens spent | 0 | 0 |
| dollars spent | $0.00 | $0.00 |
| per classification | 16ms | ~1,900ms |
| process working set | 472MB | **2.7GB** |

The ledger priced the first two rows and was blind to the other two. The failure that followed
was real rather than hypothetical: two evaluation runs at once exhausted a 16GB machine and the
second died inside onnxruntime with `bad allocation`, naming neither memory nor the component
responsible. Diagnosing it took two wrong hypotheses and a memory probe that was itself broken
and returning zeros.

So `obs/resources.py` adds the two measurements that would have caught it, and keeps an
asymmetry between them that is the point of the module rather than a wrinkle in it:

- **Latency is attributable to a request, so it is checked.** `latency_budget_ms` raises
  `LatencyBudgetExceeded` naming the stage — "slow" is not actionable, "slow in propose" is. It
  is **unset by default**, because a budget that trips during an evaluation turns a slow
  configuration into a crashed one.

  "Checked" rather than "enforced", because the check runs at each stage *boundary*. Measured
  with a proposer sleeping 400ms under a 50ms budget, it raises after 401ms of real wall time:
  the breach is detected once the cost is fully paid. What it genuinely buys is the remaining
  stages — with a cross-encoder spending 1,900ms in propose, adjudication is skipped — plus a
  loud typed error naming the stage instead of a silent p99. What it cannot do is interrupt a
  running stage, so a hung `onnxruntime` call hangs forever and the budget never fires, and it
  cannot bound a request below its slowest single stage. The dollar ceiling has the same shape:
  `CostLedger.record` appends and *then* checks. Both are detectors.
- **Memory is not attributable, so it is never enforced.** The 2.7GB is an arena ONNX reserves
  within the first twenty classifications and then holds flat for the life of the process.
  Charging it to whichever request triggered the reservation would be a fiction; charging every
  request a share would be another. It is recorded as a process-level gauge, named
  `process_working_set_mb` so the scope is unmistakable where it is read, and there is
  deliberately no `Budget.memory_mb` — a per-request memory ceiling over a process-wide arena
  would fail on the first request and pass for every one after.

### What a trace contains

```bash
hsclass classify "a women's blouse of 70 percent silk and 30 percent cotton"
```

One JSON line per classification, carrying the schedule revision, the input, each stage with its
own duration, the decision with its rule path and cited notes, the token ledger, and the
process gauge. Stage 4, explanation, is **absent rather than empty**: a stage that always reports
nothing reads as a stage that ran and found nothing to say.

Measured on the test split:

| | |
| --- | ---: |
| traces written | 200 of 200 cases |
| traces with all three stages timed | 200 |
| overhead, median of four alternating rounds | **+1.74ms (+6.5%)** |
| decisions changed by tracing | **0** |

**The overhead number is weaker than it looks, and the honest statement is a bound.** Tracing's
directly measured cost is **0.54ms** per request, 98% of it the file append (0.53ms; a held
handle is over a hundred times cheaper). The end-to-end A/B over four alternating rounds of 200
cases puts the median at **+1.6ms (+6%)** on a 26ms baseline. Those two do not reconcile, and an
independent re-measurement reproduced both the 1.6ms and the gap — one of its four rounds came
out **negative** (−0.76ms), and the spread on the untraced arm was 3.2ms, twice the effect being
measured. So: tracing costs under about 3ms per request, probably well under; the component
figure is the one with a mechanism behind it, and the A/B cannot resolve it further on this
machine.

That is a correction to an earlier draft of this section, which reported +1.74ms with "a
consistent sign in every round". The sign is not consistent, and the first attempt at this
measurement was worse still: a single sequential A/B gave **+4.48ms (+17.4%)**. Each round of
this is the same lesson — an A/B whose effect is smaller than its noise reports the noise — and
publishing 1.74ms as the answer after criticising 4.48ms for exactly that reason was the same
mistake one step quieter.

Either way it misses the 5% budget set before building, against a 27ms denominator that is the
cheapest configuration the project has; against a reranked request it is 0.09%. The dominant
cost is the per-request file open-append-close, and it is kept deliberately: a trace buffered in
memory is lost exactly when the process dies, which is when it is most worth having.
`test_the_trace_is_written_even_when_the_request_fails` is what holds that trade.

### Reading one back

The stages were timed at **0.0ms each** in the first version, because the work happened before
the stage's context manager opened and the timed block wrapped only the bookkeeping. That was
found by reading a trace back rather than by the tests, which checked that stages were present.
A duration of zero is not a missing measurement — it reads as a fast stage, which is a wrong one,
and it would have been wrong about `propose`, the stage that costs 1.9 seconds with a reranker
attached.

Reading that same trace back also showed `Section XI Note 1(c)` cited twice, because a Section
note excludes goods from several chapters and the shortlist spanned two of them. In a trace that
reads as an officer relying on an authority twice over. Citations are now deduplicated.

## Stage 4: writing the reasoning, and the gate that has to exist for it to be safe

The last stage, and the one where the whole architecture could be given away. Stages 1 and 2
are allowed to be wrong because stage 3 checks them; stage 3 is deterministic. Stage 4 writes
the reasoning — and a fluent model handed a code and asked why will produce a reason whether
or not the rules ever reached it. "Under GRI 3(b), on essential character" attached to a code
the engine resolved at GRI 1 is worse than no explanation at all: it is a legal citation for
reasoning that never happened, and it reads exactly like the real thing.

So the model never says what decided the case. It is given the decision and the authorities
the decision already holds, and **every citation in what it writes is checked back against
that decision before the text is returned.** Prose naming an authority the decision does not
hold is refused, recorded, and replaced by a deterministic rendering. Same discipline as the
`verify` guard in stage 1, where a percentage absent from the description is dropped rather
than passed to the engine: the check is a gate, not a score.

**The deterministic renderer is the product. The model is an enhancement.** `render` alone is
a complete, citable explanation of any decision, needs no key, no network and no budget, and
names every rule in `rule_path` because it is built from `rule_path`. If the model is
unavailable the system still explains itself.

### What "correct" means here, stated before building

Not an accuracy. An explanation is correct when it is **faithful**: every rule, note and
heading it cites is one the decision actually holds. Three numbers, kept apart because they
answer different questions — fabrications by the renderer, which must be zero; fabrications
the gate catches in the model's prose, which prices the risk; and completeness, the share of
the engine's own applied rules the prose names, because omitting the deciding rule is not
false but is useless as an audit record.

What stage 4 **cannot** move, and is not reported as moving: agreement with the judgement
oracle. That is a property of stage 3's rule path. Stage 4 only describes the path.

### The first version did not hold the claim, and a cold read proved it

`unsupported` was not a faithfulness gate. It was a **citation-format** gate: it caught a
fabrication only when the fabrication was spelled the one way the grammar recognised. Sixteen
of eighteen adversarial probes walked through it. Worse, `render` and the model's own prompt
both wrote codes in the undotted form, so the system was *teaching the model* the format that
evaded its heading check.

| prose, against a decision resolved at GRI 1 on 6109.10.0012 | first gate |
| --- | --- |
| `The goods fall in 6405.20.6060, other footwear.` | **passed** |
| `HTS 6110.30.3020 provides for these goods.` | **passed** |
| `subheading 6109909090` (undotted — the form the prompt shows) | **passed** |
| `subheading 6109.90.9090` (dotted — the form it never shows) | caught |
| `Under Rule 3(b) the goods take the identity of the dominant component.` | **passed** |
| `Classification follows the component giving them their fundamental character.` | **passed** |
| `The predominant fibre decides the subheading.` | **passed** |
| `No heading describes them, so they go with the goods they most closely resemble.` | **passed** |

Fourteen of fifteen are caught now, and every one is a regression test. `\d{4}` with nothing
anchoring its end had been reading the first four digits of a ten-digit code, so a
neighbouring statistical suffix — the exact answer Section XI Note 2(A) exists to prevent —
read as its own heading and passed.

**Three of the gaps were in the fixes, not the original**, and each was found by re-running a
measurement rather than by re-reading code:

- the literal `predominat` cannot match **"predominant"** — the word is `predomin`+`ant`, the
  pattern wanted `predomin`+`at`. It matched the renderer's own "predominating" and nothing a
  model or a shipper actually writes.
- `_bare` stripped the word "Note" itself, so no label could ever match and every correct
  paraphrase of a held authority was counted as invention.
- `REFUSAL_RE` demanded "referred to" with no words in between and "further determination"
  rather than "further determine", so **two of eight sampled refusals were the gate's own
  bug** — prose that plainly handed the case over ("it was necessary to refer the case to a
  classifier for a final decision") was scored as though it had answered. Found by reading
  the refused text, which first required keeping it: recovering it by calling the model
  again returns a *different* generation, so the text judged and the text read were never
  the same one.

### Quoting a cross-reference is not relying on it

The first `authorities_of` included the schedule text of every candidate code, so the renderer
could quote an inherited chain verbatim. That turned every cross-reference into a licence:
**7,331 of 29,861 provisions (24.6%)** name a heading other than their own in their chain,
so a decision on 0207.11.0020 — chicken cuts, whose text reads "of the poultry of heading
0105" — would have let an explanation claim the goods are classified in heading 0105, which is
live poultry. Schedule text is now excused as *quotation* rather than granted as *authority*,
which is a different thing and the distinction the gate turns on.

### What the gate catches, and the trade-off underneath it

Over the 200-case test split, with the engine deciding 4 and escalating 196:

| | renderer | model, default prompt | model, refusal-first prompt |
| --- | ---: | ---: | ---: |
| fabricated citations | **0 / 200** | — | — |
| refused by the gate | — | 76 and 84 / 200 (**38–42%**) | **0 / 200** |
| applied rules the prose names | 100% *by construction* | **86.5–88.5%** | **1.5%** |

The model's two columns are ranges because **`gpt-4o-mini` is not deterministic at
temperature 0**, and two runs of the same prompt through the same gate refused 76 and 84 of
the same 200 descriptions. A single run's figure is not reproducible, so none is quoted as
*the* rate. That also means a refusal count cannot be used to compare two versions of the
gate: an earlier pair of runs refused 5 and then 4 across a change that made the gate
strictly stricter, which is only possible if the prose changed underneath it.

Almost every refusal is one thing: **the model explaining an escalated case as though a code
had been chosen.** "The classification will be determined by the most appropriate heading that
encompasses the nature of the product" — for goods the engine refused to classify. The refusal
is this system's product, and stage 4 was quietly softening it into a near-answer.

The obvious suspicion is the prompt, which opens with "a classification that has already been
decided". So a second prompt says what to do when nothing was decided. **It works, and it is
worse**: refusals fall to zero and completeness collapses from 88.5% to 1.5%. Told not to say
which heading applies, the model stops giving legal reasoning at all and writes a bare
refusal. Faithfulness bought that way is not worth having, because an explanation that cites
nothing cannot be audited. Both prompts are in the source and both are re-measurable
(`--refusal-first`); the default is kept and the second is a recorded negative result.

The pair is the finding: **at this model size the two properties trade against each other,
and the deterministic renderer has both.** The model buys readability for roughly 60% of
cases at $0.078 per 1,000, and the gate is what makes that safe rather than merely cheap.

### Two numbers here were published wrong, and both are corrected

- **"0 fabrications over 200 cases" was true and misleading.** `render`'s escalated branch
  quotes no schedule text, so the surface that *can* fabricate is exercised once per decided
  case — **4 times, not 200**. The report now prints that denominator beside the zero. This is
  the project's own rule about saying how many cases a number rests on, broken here.
- **A reported 100% rule completeness for the renderer was tautological** and is withdrawn.
  `render` joins `rule_path` into its text verbatim and the metric then re-parsed that same
  string, so it could only fall below 100% if the grammar failed to read back a label the
  renderer had just copied. It measured regex round-tripping and sat in the report beside a
  real measurement looking like one. The renderer does name every applied rule — but that is a
  property of how it is built, not a result, and it is stated that way now.

### The judgement oracle was undercounting, and its headline moves

Widening the grammar to catch a rule named without the token "GRI" changed the oracle too,
since it reads CBP reasoning with the same parser. That is the point of one grammar, and it
cuts both ways:

| | before | after |
| --- | ---: | ---: |
| rulings naming a GRI beyond 1 | 903 | **964** |
| of those, the engine decided | 9 | 10 |
| agreed on the deciding rule | 4 | **3** |

Sixty-one more rulings name a rule the narrow grammar could not see — "General Interpretative
Rule 3(c)", "Rule 2(a) of the General Rules of Interpretation". Agreement falls because the
deciding rule is the *furthest* one reached, and the narrow grammar had been missing the
furthest one. Verifying the widening against real ruling text is also what exposed a false
positive worth keeping out: an **"Additional U.S. Rule of Interpretation 1(c)"** is a
different instrument from a General Rule, and 1(c) is not even a GRI subparagraph.

**3 of 10 is far too small to carry any weight**, and no claim is made on it. The denominator
is the real finding: the engine decides 10 of 964 cases where the ruling named a rule beyond
GRI 1, which is the same coverage story the rest of this README tells.

### A bug in stage 3, found by reading stage 4's output

Not an explanation problem. For "a men's knit pullover made of 60% cotton and 40% modal" the
engine chose **6110.20.1010**, whose own chain reads "Of cotton — **Containing 36 percent or
more by weight of flax fibers**". The goods contain no flax, and the stated percentages sum to
100, so flax is necessarily zero — but `conditions.py` saw no `flax` key, returned *unevaluable*
rather than false, and the provision survived by untestability. The correct line is 6110.20.2069.
Note that at six digits the engine was already right: this is a ten-digit error, and the two are
different claims.

Reading real output found it; no test did. **It is not fixed**, and the attempt is written up
under [making a threshold evaluable](#making-a-threshold-evaluable-built-measured-reverted) —
reading an absent material as zero contradicted the *correct* provision in 103 train rows, 71 of
them because a shipper writes spandex where the schedule writes "elastomeric yarn". What the
measurement said about that is worth more than the bug.

## Production concerns

- **Tracing** — every classification is reconstructable after the fact: candidates considered,
  rules fired, notes consulted, schedule version, per-stage timings, token cost and a
  process-level resource gauge, written as one JSON line. Built in week 1 and **connected to
  nothing until week 3**, which this heading used to obscure by claiming it was built in from
  the start: the modules were, the wiring was not, and an unused tracer is not observability.
- **A cost ceiling per request** — built, unit-tested, and **with no call site in the pipeline**,
  because stages 1 to 4 currently make no model calls at all. Every trace records
  `"total_usd": 0`. Saying so is the finding rather than an embarrassment: a ledger that cannot
  fire is exactly why latency and memory had to be measured instead.
- **A latency budget per request**, checked at each stage boundary and naming the stage that
  blew it. Unset by default — see the observability section for what it can and cannot do.
- **A known gap**: the incident that motivated all of this was a *process*-level condition — two
  runs, 16GB, an ONNX arena each. Nothing here checks available memory before loading a model,
  which is where that check belongs and where it does not yet exist.
- **Schedule versioning** — stamped on every trace and report; a code without one is unusable.
- **Evals gated in CI** — `scripts/check_regression.py` fails the build when a metric falls below
  the committed floor, and refuses to compare across schedule versions.

## Layout

```
src/hsclass/
  config.py          pinned revision, paths, cost ceiling
  schedule/          tree model, export builder, validators
  eval/              metrics (6 vs 10 digit, coverage, per-chapter), runner
  eval/calibration.py    risk-coverage curves, and the rules against a threshold
  obs/               tracing, the cost ledger and the resource gauge
  obs/resources.py       the latency budget, and why memory is not budgeted
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
