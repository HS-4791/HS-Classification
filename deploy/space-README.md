---
title: HS Tariff Classification
emoji: 📦
colorFrom: gray
colorTo: green
sdk: docker
app_port: 8000
pinned: false
---

# HS tariff classification

A product description in, a US customs code and the legal reasoning that produced it out — or a
refusal with the shortlist a human classifier would work from.

Candidates come from 18,253 past CBP rulings; the General Rules of Interpretation are then
applied **deterministically**, with no model in the decision. The rules settle very few cases and
refuse the rest, which is the design: a wrong tariff code is a penalty, so the system says when it
does not know. Every answer is labelled with which of the two it is.

Measured **on this container**, over HTTP, against the official 200-case ATLAS test split at
schedule revision 2026HTSRev21 — not borrowed from a different configuration:

| | all 200 | excluding leaked duplicates |
| --- | ---: | ---: |
| precedent top-1, 6-digit | 52.5% (105/200) | 46.3% (74/160) |
| precedent top-1, 10-digit | 46.0% (92/200) | 40.0% (64/160) |
| settled by the rules | 3 of 200 | 1 of those 3 correct |

For scale, the published fine-tuned LLaMA-3.3-70B baseline on this split is 57.5% at six digits
and 40.0% at ten. 40 of the 200 test descriptions appear verbatim in the training data, 9 of
them with a *different* code, so both columns are reported.

**Three cases is not an accuracy**, and the rules' row is given as counts for that reason. One of
the two misses is worth stating plainly: *"a chisel"* resolved at GRI 1 to 8209.00.0030, cermet
tool plates, because no candidate offered to the rule engine was 8205.30, chisels. A
deterministic engine over a bad shortlist is not unsure — it is confidently wrong.

The page will also tell you when a suggested code **is not a line in this revision at all**. That
happens on 35 of the 200 cases, because CBP rulings are historical and the schedule is not: 22
renumbered statistical suffixes, 11 whose eight-digit tariff line is gone too, 2 that resolve to
nothing. 19 of the 35 are still right at six digits, which is why six- and ten-digit accuracy are
reported as separate claims here rather than averaged.

Source, including every measurement script and report:
https://github.com/HS-4791/HS-Classification
