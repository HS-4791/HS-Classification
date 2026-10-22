# HS tariff classification

Classify a product into its customs tariff code, return the legal reasoning path that produced
it, and escalate rather than guess when the nomenclature is genuinely ambiguous.

Scored against real binding rulings issued by US Customs, so the ground truth is an authority's,
not the author's.

> **Status: week 1 of 4 complete.** The schedule is parsed from the PDF and reconciles against
> the published export at **99.84%**. The vector-only baseline is measured and published below;
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

Stages 1 and 2 are allowed to be wrong, because stage 3 checks them against the rules. Stage 4
writes the explanation from a decision already made, which is why it cannot invent a
justification for an answer the rules did not reach.

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

Across all 4,534 pages: **35,703 of 35,805 rows reconcile exactly — 99.72%**. Chapter 61, the
chapter the running example lives in, is at 100.00%, and that is enforced in CI.

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

- **Chapter 99** uses a different column layout (it carries effective-date columns), so its rate
  fields do not line up with the ordinary grid. Rows match; rate *fields* do not. Chapters 98 and
  99 are US special provisions that reference goods of other chapters, not ordinary nomenclature.
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

Published baselines this will be scored beside: arXiv `2509.18400` (ATLAS), `2412.14179`
(independent benchmark), `2605.14857` (deterministic agentic workflow).

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
  retrieval/         embedding backends and the index        (week 1, next)
  pipeline/          the four stages                         (weeks 2-3)
scripts/             fetch the pinned release, the CI regression gate
data/manifests/      committed: checksums and expected tree shape
data/raw/            gitignored: reproducible via scripts/fetch_schedule.py
reports/             eval output, committed
docs/                the spec and the domain primer
```
