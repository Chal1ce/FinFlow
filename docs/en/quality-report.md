---
title: Data quality and coverage
nav_order: 10.5
---

# Data quality and coverage

Generate offline JSON and Markdown reports from checksum-verified CPT v2 or SFT v2 packages.
CPT lineage-only packages are supported but may contain no training samples. No model credentials or calls are required.

```sh
python -m training.quality_report \
  --input data/training/snapshots/cpt-all \
  --output data/reports/cpt-all

python -m training.quality_report \
  --input data/training/snapshots/cpt-next \
  --baseline data/reports/cpt-all \
  --targets config/coverage-targets.json \
  --output data/reports/cpt-next
```

The output must be a new directory outside the immutable input package. It contains `report.json`, `report.md`,
`manifest.json` and `checksums.sha256`. Invalid input checksums stop publication; CLI failures exit with code 2.

## Metrics and interpretation

- Counts include samples, unique content, exact duplicate copies/rate, logical works and images. CPT token counts use the stored target tokenizer counts.
- Coverage dimensions: split, source, source language label, method, task, strategy and modality. Missing labels are `unknown`.
- A sample with multiple sources counts once per source; label totals can exceed sample/token totals.
- SFT supervised token counts remain unavailable until a training tokenizer, chat template and loss mask are fixed.
- Language reflects source metadata, not detected language of translated or generated text.
- Audit entries aggregate statuses and reason fields, omitting free-form judge explanations. Entries lack a unique candidate denominator, so acceptance rate is `null`.
- Checks cover identical content, logical works and images across splits within the package. This is not semantic deduplication, held-out test contamination detection or factual evaluation.
- Reports store content hashes for comparison but never copy training text or model responses.

## Comparing versions and coverage targets

`--baseline` accepts an existing verified report directory. Kind, scope and tokenizer must match.
Daily deltas cannot be compared against cumulative snapshots. The report lists added, removed, retained and split-changed content,
metric deltas, coverage deltas and recipe changes. Token growth is not evidence of model improvement.

Targets are a JSON array of minimum sample counts; the example demonstrates syntax, not recommended training proportions:

```json
[{"field":"task","label":"table_calculation","min_samples":100}]
```

Supported fields: `source_name`, `language`, `method`, `task`, `strategy`, `modality`.
Each target returns `actual_samples` and `missing_samples`. Targets do not automatically schedule generation.

## Daily runs

The flywheel automatically reports each newly produced CPT and optional SFT package under
`data/reports/training/<run_id>/cpt/` and `sft/`. Daily JSON includes paths and metrics in `quality_reports`.
Missing packages are `not_available`. Report failures mark the run `partial`, record the error type and retain published training data.
Daily CPT is usually a delta; SFT is a snapshot for the selected evidence and recipe. Do not add their counts as daily new samples.
A successfully generated report does not certify factual quality; inspect its `warnings` too.
