# Working agreement

HS tariff classification: given a product description, return the customs code, the legal
reasoning path that produced it, and escalate rather than guess when the nomenclature is
genuinely ambiguous.

This file is the project's standing rules. If something here is wrong, change it here — do not
work around it in code, and do not rely on a conversation remembering it.

## The architecture invariant

**The model never decides.** Four stages, and only stage 3 adjudicates:

1. **Normalise** — extract typed attributes from text and image · probabilistic
2. **Propose** — vector retrieval plus hierarchical traversal generate candidates · probabilistic
3. **Adjudicate** — GRI rules over the notes · **deterministic, no model in it**
4. **Explain** — write the reasoning from a decision already made, or escalate

Stages 1 and 2 are *allowed* to be wrong, because stage 3 checks them. Stage 4 can only explain
a decision stage 3 already made — it must never be able to invent a justification for an answer
the rules did not reach.

Any change that lets a model influence stage 3 breaks the project's central claim. If a rule's
condition cannot be evaluated, the answer is to **escalate**, never to guess.

## Measurement discipline

- **Six-digit and ten-digit accuracy are different claims.** Report separately, never average.
  Six digits is internationally identical and is the primary claim; ten is US-only and harder.
- **A number that was not measured is not a result.** Do not write an accuracy, a latency or a
  cost into the README until a run produced it. "Not yet measured" is an acceptable entry.
- **Publish negative results.** The BGE query prefix made retrieval worse; hierarchical
  traversal costs accuracy. Both are in the README. A component that adds nothing is a finding.
- **Say how many cases a number rests on.** 3/5 correct is not "60% accuracy" without the 5
  beside it. Small-n results get an explicit caveat.
- **Escalated cases count as misses in the headline**, and coverage is reported alongside.
  92% over 80% of cases beats 85% over everything, but only if both numbers are shown.

## Verification before trust

The parser is not trusted because the code looks right; it is trusted because 35,746 of 35,805
rows match an independent source. Build the check first, then the thing.

- The published USITC JSON export is the **oracle** for the PDF parse, never the source. The
  PDF is the source, because most jurisdictions publish nothing else.
- Every parser bug so far was found by reconciliation, not by reading code: italics splitting
  rows, superscripts landing on the wrong line, `(con.)` headers eating real rows, the Section
  notes page parsed as a tariff table.
- Where a check cannot be automated, say so rather than implying it passed.

## Schedule versioning

A classification is only valid against a schedule version. USITC publishes a new revision
roughly every two weeks.

- The pinned revision lives in `.env` (`HTS_REVISION`) and is stamped on every trace, report and
  eval result.
- `data/manifests/<revision>.json` holds source URLs, SHA-256 of every file and the expected
  tree shape. `scripts/fetch_schedule.py --verify` fails if what the endpoint serves has moved.
- Accuracy measured against one revision is not comparable to another. The CI regression gate
  refuses to compare across versions rather than quietly doing it.

## Domain rules that constrain the code

- **Section and Chapter titles have no legal force.** The notes are binding. GRI 1 says so.
- **GRI 6: only subheadings at the same level are comparable.** This is why traversal is legally
  required, not merely efficient.
- **The rules apply in order and stop at the first that resolves.** Reaching Rule 3 where Rule 1
  would have answered is itself a classification error — and has already happened once here, by
  missing the tie-break written into Section XI Note 2(A).
- **`rule_path` records only rules that were actually evaluated.** Naming a rule the walk never
  reached is a fabrication in a trace that week 4 scores explanations against.
- **Chapters 98 and 99 are US special provisions**, not ordinary nomenclature. They re-describe
  goods classified elsewhere, so they are exempt from the structural rules and counted
  separately — explicitly, never by silently skipping them.

## Chunking

- **The tariff schedule is not chunked.** It is parsed into a tree, one node per line. Chunking
  by token count would destroy the hierarchy, and the hierarchy is the legal structure.
- **Notes are chunked one per provision.** Note 2(A) is a unit, 2(B) is another. A length-based
  split separates a condition from the qualifier that governs it.
- **Every node carries its inherited chain.** `6109.10` reads as "Of cotton" and is meaningless
  embedded alone.
- **Every note carries its scope and its parent's lead-in.** "This Chapter does not cover:" is
  ambiguous without knowing which chapter.

## How to work here

- **Never patch source files with regex scripts.** Every file-corruption incident in this repo
  came from that — NUL bytes written into `parse_pdf.py`, then a repair that doubled every line.
  Use proper edits.
- **Run the clean-install path before writing CI config.** A workflow that has never been
  executed is not a workflow.
- **State what "correct" means before building the thing**, so the result cannot be
  rationalised afterwards.
- **Run `/code-review` over a diff before committing it**, not after a week of accumulation. It
  is a different reasoning path from the one that wrote the code.
- Keep `make check` green: `ruff`, `pytest`, tree validation, chapter-61 reconciliation.

## Out of scope

Decided and not to be relitigated:

- **Valuation and origin.** Classification only. The documents mention all three; this answers one.
- **A classifier that always answers.** The refusal is the product.
- **Approximate nearest-neighbour search.** At ~30k nodes exact search is one matmul, and ANN
  would add a recall variable to every ablation and weaken the determinism claim.
- **PyTorch.** This machine's Application Control policy refuses to load its DLLs. Embeddings
  run through ONNX Runtime (`fastembed`). Anything requiring torch will not run here.

## Commands

```bash
make check                                   # lint, tests, validation, reconciliation
hsclass schedule show 6109.10.00             # a line with its inherited chain
python scripts/parse_full_schedule.py        # re-parse the PDF (~5 min)
python scripts/extract_notes.py              # notes + exclusion graph (~5 min)
python scripts/run_ablations.py              # all configurations, both eval sets
```

Long-running scripts write progress; run them unbuffered (`python -u`) and never pipe them
through `grep`/`tail`, which buffers until exit and has twice left a run looking hung.

## Data on disk

`data/raw/` and `data/processed/` are gitignored and reproducible from scripts. Committed:
`data/manifests/` (checksums, licences, expected shapes) and `reports/` (every measured result).
