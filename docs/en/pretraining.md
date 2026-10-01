---
title: Existing release export
parent: Training data
nav_order: 1
---

# Export pretraining text from existing releases

This guide covers `training.cli` and v5/v6 packages. For v7, target-tokenizer
segmentation, visual descriptions, and model-generated candidates, use the
[daily flywheel](data-flywheel.md#5-releases-and-training-data).

The legacy exporter reads integrity-verified local packages, redacts email/phone
patterns, deduplicates body text, and writes auditable, model-independent JSONL.
It does not call models or provide SFT, evaluation datasets, or a training framework.

Verify every input package before creating a new dataset:

```sh
python -m workflow.cli --data-root data verify-release \
  --batch-id fin-2026-08-11 --release-id RELEASE_ID

python -m training.cli --data-root data build-pretrain \
  --dataset-id financial-governed-v1 \
  --release fin-2026-08-11/RELEASE_ID \
  --release research-2026-08-11/RELEASE_ID
```

Repeat `--release` for multiple packages. An existing dataset ID causes failure
instead of overwriting data. Files are written atomically under
`data/training/pretrain/financial-governed-v1/`:

| File | Contents |
| --- | --- |
| `corpus.jsonl` | `schema_version`, stable `sample_id`, redacted `text` |
| `provenance.jsonl` | Release/document/asset IDs, origins, rules, OCR quality, text hashes, redaction counts, admission |
| `excluded.jsonl` | Empty or duplicate text exclusions |
| `manifest.json` | Inputs, counts, redaction rules, quality summary, dataset policy |
| `checksums.sha256` | SHA-256 hashes of the other four files |

Titles, source metadata, and retrieval annotations are not inserted into training
text. Deduplicated text retains all origins in provenance.
v6 provides complete governed body snapshots (`full_governed_document`). Verified
v5 packages reconstruct text from `chunks.jsonl` and are marked
`lossy_chunk_reconstruction`. OCR `needs_review` documents are retained, with
quality signals in provenance and manifests rather than in training text.

These exports use `usage_scope=internal-only` and `rights_status=unknown`.
Redaction replaces emails with `<EMAIL>` and mainland Chinese mobile/common
landline numbers with `<PHONE>`. Confirm actual source permissions and applicable
usage before training; pattern redaction does not establish those permissions.
