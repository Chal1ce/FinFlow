---
title: SFT data generation
nav_order: 5.2
---

# Evidence-grounded SFT data

FinFlow generates three types of Chinese text SFT examples from verified, locally approved v7 releases:

| Recipe | Input | Output and checks |
| --- | --- | --- |
| `document_qa` | Approved original text | Questions, answers and exact quotations; independent evidence review |
| `extraction` | Approved original text | JSON field extraction; every value must occur in its evidence quotation |
| `table_calculation` | Table OCR, surrounding context and original image from an approved visual candidate | Two-operand calculation, recomputed in code and reviewed against the image |

This guide covers **text SFT**: training inputs contain context and questions, not images.
Original table images are used for review only. Optional [visual SFT](sft-vision.md) exports real image inputs.
Preference pairs, retrieval negatives, multi-turn conversations and model training are not implemented.
Generated image descriptions, translations and rewrites are not treated as new factual evidence.

## 1. Configure models

Use the existing project environment; no additional dependencies are needed. Set credentials only in local `.env`:

```dotenv
FIN_DOC_SFT_GENERATE_API_URL=https://your-provider.example/v1
FIN_DOC_SFT_GENERATE_API_KEY=YOUR_LOCAL_KEY
FIN_DOC_SFT_GENERATE_MODEL=YOUR_GENERATION_MODEL
FIN_DOC_SFT_REVIEW_API_URL=https://your-provider.example/v1
FIN_DOC_SFT_REVIEW_API_KEY=YOUR_LOCAL_KEY
FIN_DOC_SFT_REVIEW_MODEL=YOUR_REVIEW_MODEL
```

Both endpoints must support Chat Completions. Generation and review use separate calls.
The review model must accept images when `table_calculation` is enabled.
Optional `*_MODEL_VERSION`, `*_TIMEOUT` and `*_MAX_TOKENS` settings are listed in `.env.example`.
Existing CPT credentials are not reused implicitly.

```sh
cp -n config/sft.json config/sft.local.json
python -m training.sft_cli --config config/sft.local.json preflight
```

Preflight makes no model requests and does not verify connectivity or visual capability.
The standalone SFT entry point does not require OCR or a CPT tokenizer, but requires the original
local data directory, database and v7 release.

## 2. Build a dataset

Read `release.release_id` from a daily flywheel summary. The source must still have
`source_policy.<source_name>.training` set to `approved` in the selected flywheel configuration.
Use `--flywheel-config config/flywheel.local.json` when using a local policy file.

```sh
python -m training.sft_cli \
  --config config/sft.local.json \
  --flywheel-config config/flywheel.local.json \
  --data-root data \
  build --dataset-id sft-001 --release YOUR_V7_RELEASE_ID
```

Global options precede `build`. Only candidates accepted both in the v7 release and the current
local database are eligible. Republish v7 evidence after an upstream review decision changes.
A copied release without matching local lineage records is not sufficient.

The pipeline:

1. Verifies release, candidate and evidence checksums and current source admission and candidate decisions.
2. Freezes train/validation assignments by logical work and identical context before generation.
3. Generates candidates, then checks JSON, exact quotations, extracted values and arithmetic.
4. Independently reviews support, answerability, units, dates and instruction following; table review includes the image.
5. Exports only accepted examples and records rejected, uncertain, skipped and failed results.

The generator may return zero examples. Empty or over-limit evidence is excluded with an audit reason;
tables and conversations are never silently truncated or split into separate examples.

## 3. Recipe and arithmetic limits

`config/sft.json` enables all three text tasks with up to two samples per evidence/task pair:

- `max_jobs`: maximum newly completed generation jobs per invocation; completed cache entries do not count.
- `max_model_requests`: per-invocation SFT request cap, separate from CPT; cached responses do not count.
- `max_seconds`: checked before starting each model request; an in-flight request may continue until its role timeout.
- `max_evidence_chars`, `max_question_chars`, `max_answer_chars`: character limits.
- `validation_fraction`: initial assignment ratio; previously frozen or CPT-assigned works retain their split.
- `generation_prompt_version`, `review_prompt_version`: prompt revision identifiers.

These are workload and length limits, not a provider price table or currency budget.
Exports retain complete `messages`; the downstream trainer supplies its tokenizer, chat template,
length validation and packing policy.

Supported calculations use two operands with identical units:

| Operation | Formula |
| --- | --- |
| `sum` | a + b |
| `difference` | a − b |
| `ratio` | a / b |
| `percentage` | a / b × 100% |
| `growth_rate` | (a − b) / b × 100%, requiring b > 0 |

Values and units require exact evidence quotations. Decimal arithmetic uses `ROUND_HALF_UP` and
0–6 decimal places. No generated code is executed, units are not converted and percentage operands
are unsupported. Review must still establish row/column meaning, period and reporting scope:
correct arithmetic alone does not establish correct operand selection.

## 4. Outputs, audit and lineage

Output directory: `data/training/sft/datasets/<dataset-id>/`.

| File | Contents |
| --- | --- |
| `train.jsonl`, `validation.jsonl` | Text-only `sample_id` and `messages` for conversation-aware training programs |
| `train.vision.jsonl`, `validation.vision.jsonl`, `images.jsonl` | Visual exports and image inventory; empty for text-only recipes |
| `samples.jsonl` | Full examples with task, split, generation/review artifacts, quotations and calculation details |
| `evidence.jsonl` | Context, source, work identity, chunk/table position and upstream decision |
| `audit.jsonl` | Review results, deterministic validation failures, exclusions and duplicates |
| `jobs.jsonl` | Generation jobs and local checkpoint references |
| `manifest.json`, `checksums.sha256` | Recipe, model identities, input release, counts, status and integrity inventory |

Each text training row has three messages: `system`, `user` with evidence/task, and `assistant`.
`sample_id` is metadata and must not be concatenated into the prompt. These are synthetic examples;
automated review is not human validation. Inspect samples before training.

```sh
python -m training.sft_cli verify data/training/sft/datasets/sft-001
python -m workflow.flywheel_cli --data-root data trace --sample-id YOUR_SFT_SAMPLE_ID
```

Lineage reaches model responses, upstream candidates, text/table evidence and OCR/PDF artifacts.
Training rows contain the context and can be used independently for text training. Full audits refer
to the original data directory; preserve it and the release when archiving.

## 5. Resume and splits

Model responses and completed jobs are cached by inputs, model identity and recipe.
Network failures or request/time limits publish the already accepted examples with `partial` status
and exit code 1; configuration/fatal failures use exit code 2.
Continue with a **new dataset ID and the same recipe/release** to reuse completed work and process pending jobs.

An existing dataset ID returns its verified historical snapshot; it is never appended to.
The current engine/export is `sft-v2`: upgrading changes recipe identity, so use a new dataset ID. Existing v1 packages remain unchanged.
Each new export contains cumulative results for the selected release and recipe, not a daily delta.
Do not concatenate successive resume snapshots without deduplication.
Rejected/uncertain results are not regenerated indefinitely. Change a prompt version or model to
create a new recipe while keeping old audit records. There is no SFT manual-approval command yet;
upstream human candidate approval does not approve derived SFT examples.

Splits are persisted before generation and inherit existing CPT assignments. Identical context, identical image bytes and
same-work derivatives share a split. New CPT builds also respect SFT assignments. Historical conflicts
exclude affected SFT evidence with `split_conflict`. There are only train/validation splits, no independent
test set. Validation may be used during recipe development and is not a final blind benchmark.
This does not automatically detect every semantic near-duplicate, republication or unlinked document revision.

## 6. Daily generation

Enable the following in the local flywheel configuration:

```json
"sft": {"enabled": true, "config": "config/sft.local.json"}
```

Relative paths resolve against the parent of the directory containing the flywheel configuration,
usually the project root. Run the existing daily command:

```sh
python -m workflow.flywheel_cli --config config/flywheel.local.json run
```

SFT runs after evidence publication and CPT generation, using remaining flywheel time and its own
request cap. Completed jobs reuse cached results. Daily summaries and channel reports include `sft_dataset`.
SFT failures make the daily run `partial` while preserving published CPT data.
The default is `enabled=false`; this feature does not install scheduling or call models by itself.

## Implementation status

The first three text-SFT recipes, resumable caches, arithmetic checks, independent review, shared splits
and artifact lineage are implemented. Static checks and documentation builds are not functional tests
or real-model acceptance. Those validations have not yet been performed for the new SFT implementation.

## Additional generation strategies

Direct generation remains the default. Optional grounded Self-Instruct, answer-first, Evol-Instruct and CodecLM workflows are documented in [Generation methods](training-methods.md). See also [Data mixtures](data-mixtures.md).
