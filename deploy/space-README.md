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
applied **deterministically**, with no model in the decision. The rules answer about 2% of cases
and refuse the rest, which is the design: a wrong tariff code is a penalty, so the system says
when it does not know.

Measured on the official 200-case ATLAS test split, schedule revision 2026HTSRev21:

| | all 200 | excluding leaked duplicates |
| --- | ---: | ---: |
| precedent top-1, 6-digit | 52.50% | 46.25% |
| precedent top-1, 10-digit | 46.00% | 40.00% |
| rules, accuracy when they answer | — | 41.67% (held-out) |

For scale, the published fine-tuned LLaMA-3.3-70B baseline on this split is 57.5% at six digits
and 40.0% at ten. 40 of the 200 test descriptions appear verbatim in the training data, 9 of
them with a *different* code, so both columns are reported.

Source: https://github.com/HS-4791
