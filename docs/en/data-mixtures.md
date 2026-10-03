---
title: Data mixtures and selection
nav_order: 5.5
---

# Data mixtures and selection

`training.mixture_cli` consumes immutable CPT v2 or SFT v2 packages, preserves lineage and publishes a new training mixture.
It makes no model calls. CPT and SFT use separate recipes. Original validation records remain intact and are not selected by training quotas.

## Preview and publish

```bash
cp config/mixture-cpt.json config/mixture-cpt.local.json
# Edit inputs, grouping and budget. Relative paths are relative to the working directory (repository root).
python -m training.mixture_cli --config config/mixture-cpt.local.json plan
python -m training.mixture_cli --config config/mixture-cpt.local.json \
  build --output data/training/mixtures/cpt-001
python -m training.mixture_cli verify data/training/mixtures/cpt-001
```

For CPT, prefer a complete cumulative snapshot from `workflow.flywheel_cli snapshot`. Missing historical samples referenced by
selected deltas cause an error. For SFT, start with `config/mixture-sft.json`; text and vision packages may be combined. Overlapping
snapshots are deduplicated by content while preserving origins. Content, images or known document families crossing splits cause an error.

## Parameters

| Setting | Meaning |
| --- | --- |
| `group_fields` | One or more of `method`, `task`, `strategy`, `source_name`, `language`, `modality` |
| `method` | `fixed`, `temperature`, `doremi`, `regmix` |
| `unit` | CPT: `tokens` or `samples`; SFT: `samples` |
| `budget` | Positive total training quota |
| `max_per_family` | Optional maximum selected sample count per known document family, including derivatives |
| `seed` | Stable group and within-group ordering seed |
| `redistribute` | Reallocate unused quota to nonzero-weight groups with remaining capacity; default false |
| `allow_shortfall` | Allow publication below budget; default false |

Multiple labels are joined with `|`, e.g. `document_qa|answer_first`. Missing labels become `unknown`; conflicting labels from
multiple origins of identical content become `mixed`, never duplicated counts. Language is not inferred when absent from source
metadata. Groups are mutually exclusive combinations, not arbitrary simultaneous marginal constraints.

Fixed-weight example:

```json
{"method": "fixed", "group_fields": ["method"], "weights": {"original": 0.7, "translate": 0.2, "distill": 0.1}}
```

These are illustrative ratios. Weights must explicitly name every available training group; zero is allowed. Use a temperature
plan first to inspect actual group names.

Temperature sampling uses `p_i = n_i^alpha / sum(n_j^alpha)`: alpha=1 preserves scale, alpha=0.5 raises smaller groups' relative
share, and alpha=0 assigns uniform weights to nonempty groups. See [XLM](https://github.com/facebookresearch/XLM/blob/main/xlm/utils.py).
The selected unit determines n; these probabilities are not quality scores.

CPT counts use the frozen target tokenizer. Token mixtures require identical tokenizer recipes across inputs. Samples are never
truncated to fill quotas. Availability, whole-sample granularity, family caps and diversity filters can produce shortfalls.
`plan` reports available/target/selected quantities, actual shares and shortfall; partial plans return exit code 1. `build` rejects
shortfalls unless explicitly allowed, then preserves partial status. Selection is always without replacement.

## Export contract

- `manifest.json`: input identities, recipe, target/actual proportions, tokenizer and status.
- `records.jsonl`: selection ID, content hash, group, original payload, known origins and document family.
- CPT: `train.jsonl`, `validation.jsonl`.
- SFT: also `train.vision.jsonl`, `validation.vision.jsonl`, original `images/` and `images.json`.
- `checksums.sha256`: complete file inventory and hashes.

Mixtures use `finflow-mixture-dataset-v1`, not the original SFT verifier. Load actual PIL images using
`training.mixture.load_vision_records(path, split="train")`. Origins reference original sample/artifact IDs; complete historical
auditing still needs the source data root. Snapshot admission is historical: this command does not query live policy revocations.
Republish an approved snapshot when source policy changes.

## Quality and diversity

Optional selection configuration:

```json
{"selection": {"scores_file": "data/scores/quality.jsonl", "metric": "jaccard", "threshold": 0.9}}
```

Each score JSONL row requires mixture-pool `id`, `content_hash`, `quality` and `complexity` (finite 0–5).
Cosine mode also requires finite, nonzero `embedding` vectors of identical dimension. Scores must cover the entire training pool,
not just a quota-selected subset. Inspect IDs through `training.mixture_data.load_pool(inputs, group_fields)` or a sufficiently
large unfiltered mixture's `records.jsonl`.

Within each group, rank by quality × complexity, then reject candidates whose similarity to already selected examples meets the
threshold. Family limits still apply. Jaccard uses full-text character trigrams, comparing assistant answers for SFT so repeated
context does not dominate similarity. Cosine consumes externally computed scores/embeddings; no DEITA models are downloaded.
This is [DEITA-inspired](https://github.com/hkust-nlp/deita); lexical similarity is not semantic similarity. Direct pairwise comparisons
target bounded local pools; partition large datasets first.

## Learned weights

Run [DoReMi / RegMix](mixture-experiments.md), then copy `config/mixture-learned.json` and set the corresponding method and weights
file with identical inputs/grouping. Learned CPT weights are applied as token quotas and bound to the exact frozen pool.
Proxy training samples with replacement; static dataset export does not, so learned weights may still exceed available capacity.
