---
title: Training data
nav_order: 5
---

# Training data

FinFlow creates text training files. Your downstream training program trains the model.

## Daily v7 flywheel outputs

Original text, visual descriptions, and optional translations/rewrites pass independent
review and are segmented with the target tokenizer. Related content shares a
train/validation split; cross-day deduplication preserves all origins.

See [configuration](data-flywheel.md#1-environment-and-models),
[source admission](data-flywheel.md#2-sources-and-admission), and
[releases and snapshots](data-flywheel.md#5-releases-and-training-data).

## Existing v5/v6 financial releases

Use `training.cli` to export body-text corpora from existing packages; see
[Existing release export](pretraining.md). This entry point does not call translation
or rewriting models and does not use the flywheel's target-tokenizer recipe.

## Supervised fine-tuning data

Approved v7 original text and tables can also generate document QA, structured extraction
and table-calculation examples. Outputs retain complete messages, evidence, review and lineage
without CPT-style text segmentation. See [SFT data generation](sft.md) for the standalone CLI,
daily integration, resumable caches and split limitations.

## Visual SFT

Optional `config/sft-vision.json` supports visual QA and table-image-to-JSON transcription.
Exports package original images, typed messages and visual evidence metadata, with separate text/vision training files.
See [Visual SFT](sft-vision.md).
