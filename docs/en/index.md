---
title: Introduction
nav_order: 1
permalink: /
---

# Financial documents to traceable training data

FinFlow collects financial disclosures and related papers, runs OCR and governance,
stores images and tables, generates multimodal descriptions, and publishes reviewed
text for continual pretraining (CPT). SQLite tracks tasks, versions, and lineage.

![Pipeline overview; labels are in Chinese](../assets/images/pipeline-overview.svg)

The diagram follows collection → OCR → text and visual governance → generation →
independent review → evidence release and training data.

Start with [Quick start](quick-start.md), then [Daily data flywheel](data-flywheel.md).
Run all commands from the repository root. Keep credentials in your local `.env`;
collection targets belong in `config/collection.json`, and recipes and budgets in
`config/flywheel.json`.

## Choose an entry point

| Goal | Entry point | Guide |
| --- | --- | --- |
| Daily collection, visual processing, and CPT export | `workflow.flywheel_cli` | [Daily data flywheel](data-flywheel.md) |
| Feishu / Telegram / Discord / Slack or assistant tools | `integrations.cli` | [App integrations and MCP](app-integrations.md) |
| Process a local PDF or existing financial OCR | `workflow.cli` | [Detailed reference guides](reference.md) |
| Discover sources or download PDFs separately | Collection and download CLIs | [Detailed reference guides](reference.md) |
| Export text from existing v5/v6 releases | `training.cli` | [Existing release export](pretraining.md) |

## Recommended reading

1. [Install and configure models](quick-start.md).
2. [Review source admission and generation methods](data-flywheel.md#2-sources-and-admission).
3. [Run, review, retry, and trace](data-flywheel.md#3-daily-operations).
4. [Understand releases and snapshots](data-flywheel.md#5-releases-and-training-data).
5. [Enable scheduling after a small real run](data-flywheel.md#6-daily-scheduling).

## Documentation coverage

The English edition covers the main workflow, training outputs, environment checks,
documentation authoring, and deployment. Detailed standalone OCR, acquisition,
governance, CLI, and operations guides remain available in Chinese through
[Reference and design](reference.md). The language switch opens the corresponding
page when available; otherwise it opens the English home page.

The project produces training files. A downstream training program runs model training.
Legacy v5/v6 exports and v7 flywheel exports use different admission and segmentation
policies; use the matching entry point.
