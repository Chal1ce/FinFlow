---
title: Cross-version near deduplication
nav_order: 10.6
---

# Cross-version near deduplication

A persistent local SQLite index compares historical and new CPT/SFT packages. Deterministic character MinHash/LSH
retrieves candidates; hashed shingle Jaccard confirms matches at a default threshold of 0.9.
This detects lexical similarity, not factual or semantic equivalence. No model calls, downloads or source-package edits.
See [DataTrove's staged MinHash workflow](https://github.com/huggingface/datatrove/blob/main/examples/minhash_deduplication.py)
for background. FinFlow implements an independent local pipeline, without distributed execution or compatible signatures.

## 1. Append versions to the index

```sh
python -m training.near_dedup_cli --index data/indexes/cpt-near.sqlite \
  --config config/near-dedup.json index --inputs data/training/snapshots/cpt-v1

python -m training.near_dedup_cli --index data/indexes/cpt-near.sqlite \
  --config config/near-dedup.json index --inputs data/training/snapshots/cpt-v2
```

Inputs must be nonempty verified CPT v2 or SFT v2 packages; do not mix types in one invocation.
Prefer cumulative CPT snapshots. Deltas referencing earlier samples require their dependency packages too.
The current pool loader limits each invocation to one million unique records; the index supports multiple batches.
Repeated content reuses fingerprints while retaining additional origins. Failed batches roll back.
Use a dedicated database outside immutable packages, never pipeline.db. Configuration and algorithm versions bind the index;
use a new path when changing thresholds, dimensions or limits.

## 2. Freeze decisions for a selected pool

```sh
python -m training.near_dedup_cli --index data/indexes/cpt-near.sqlite \
  --config config/near-dedup.json scan \
  --inputs data/training/snapshots/cpt-v1 data/training/snapshots/cpt-v2 \
  --output data/reports/near-cpt-v2
```

Scanning uses a read-only index; index every selected input first. Output must be a new directory outside inputs.
Files include `decisions.jsonl` (keep/exclude, content hash, representative, reason), `relations.jsonl` (matched pairs,
Jaccard, history-only flag, matched origins), `origins.jsonl` (all current origins, including excluded records),
`manifest.json` and `checksums.sha256` (input identities, indexed packages, settings and counts).

The earliest indexed direct match in the current pool becomes the representative. A batch orders records by stable content ID.
A≈B and B≈C does not imply A≈C. Matching stays within each split, and validation records are all retained.
Matches absent from the selected pool are report-only. Include historical packages in both scan and mixture inputs to actually filter across versions.
Approximate relationships remain separate from exact provenance; excluded origins are never relabeled as exact origins of the representative.

## 3. Apply during mixture construction

Add this field to an existing mixture recipe:

```json
{"near_dedup_report": "data/reports/near-cpt-v2"}
```

The recipe must use the identical input packages. Changed inputs require a new scan.
Use `training.mixture_cli plan/build/verify` as usual. Exclusions apply before quotas; validation is retained.
The output carries the frozen `near-dedup/` report, so verification does not require the index database.
Shortfalls use existing `allow_shortfall` and `redistribute` settings. Representatives enter the candidate pool but remain subject to later quota/quality selection.

Fixed and temperature mixtures are supported. DoReMi/RegMix weights refer to their original experimental pool;
combining them with this filter is currently rejected. Enable [daily training publication](training-publication.md) in `.env` to maintain dedicated indexes and apply frozen decisions automatically. The standalone commands remain available.
Without `near_dedup_report`, mixture behavior is unchanged.

## Conservative rules and limits

- Normalize NFKC, case and whitespace. Text shorter than `min_chars` (default 80) only matches normalized exact text.
- Different numeric sequences or built-in unit sequences prevent automatic merging. This is not factual validation; extend rules for Chinese number words or other units.
- CPT is partitioned by tokenizer identity. SFT is partitioned by task, system prompt, evidence context and original image hashes before comparing question/answer text.
- Context extraction recognizes FinFlow's material/task format. Visual samples only compare text attached to the same image; no perceptual image hashing.
- LSH may miss pairs. Hashed shingle similarity is not exhaustive semantic deduplication.
- Candidate or character limit overflow exits 2, without silently truncating or publishing incomplete results. Current pools and relations are held in memory; larger corpora need batching or streaming extensions.
- No independent test set or contamination-checking feature is introduced. Downstream projects may customize these separately.
