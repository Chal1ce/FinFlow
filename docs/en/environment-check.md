---
title: Environment check record
parent: Reference and design
nav_order: 4
---

# Environment check record

This real check from **2026-10-01** demonstrates missing configuration. It is not
a successful end-to-end run or a statement about other machines. This page
summarizes the [Chinese record](https://chal1ce.github.io/FinFlow/zh/environment-check.html).

| Item | Recorded state |
| --- | --- |
| New checkout `.env` and virtual environment | Missing |
| Previous checkout's OCR and text-model fields | Present; connectivity not verified |
| Vision model URL, key, model name | Missing |
| Independent reviewer URL, key, model name | Missing |
| Target tokenizer | Missing |

Both `preflight` and `run` returned `failed`, exit code **2**. Startup stopped at
preflight, making no discovery/OCR/model requests and creating no training data
or state database. Old configuration was loaded read-only; credentials were not copied.

![Actual preflight and startup failure; screenshot text is in Chinese](../assets/images/env-check-2026-10-01.png)

The screenshot presents actual CLI responses through an HTML layout with local
absolute paths and secrets omitted. An empty URL fails both presence and URL
validation; the messages do not mean an existing URL was entered incorrectly.

## Complete configuration

Follow [Quick start](quick-start.md), configure OCR and text governance, then fill:

```dotenv
FIN_DOC_VISION_API_URL=
FIN_DOC_VISION_API_KEY=
FIN_DOC_VISION_MODEL=
FIN_DOC_PRETRAIN_REVIEW_API_URL=
FIN_DOC_PRETRAIN_REVIEW_API_KEY=
FIN_DOC_PRETRAIN_REVIEW_MODEL=
FIN_DOC_PRETRAIN_TOKENIZER=/absolute/path/to/target-model/tokenizer.json
```

Vision and visual review need image support; the tokenizer must match the target
model. Run preflight, confirm small targets, budgets and source admission, then
run once. Passing local checks still requires real service and quality validation.
Install scheduling after inspecting real outputs.
