---
title: Optional small-model refiner
nav_order: 5.8
---

# Optional small-model text refiner

The plugin is disabled by default. Configure its model, operating mode and rewrite policy in the same `.env`. FinFlow does not install an inference server, download weights or train a model automatically.

## 1. Position and scope

```text
OCR → image/table archival → governance → original / existing transforms → review
                                      ↘ optional refiner → refined candidate → review
Accepted candidates → CPT deltas → deduplication / mixtures / releases
Original evidence → existing SFT pipeline
```

This is an additional CPT branch after governance. It does not replace the existing cleaner or overwrite text and images. It can backfill existing governed text as well as process new documents. Plugin settings have a separate identity and do not invalidate OCR/governance caches.

- `audit`: save decisions, output comparisons and lineage without producing training candidates.
- `apply`: valid results become `method=refined` candidates subject to independent review, source admission and existing release rules.
- `delete`: exclude only this plugin candidate; retain source files, original candidates and historical data.
- `rewrite`: requires a separate switch; rewritten derivatives never become SFT factual evidence.

Existing methods continue running. This plugin therefore does not filter the entire training pool. Original and refined text may coexist: existing exact deduplication merges identical text with its origins, optional near deduplication handles approximate matches, and mixtures can group by `method`. Disabling the plugin does not withdraw previously accepted or published data.

## 2. Configuration

Append to your existing `.env`, retaining OCR, reviewer, training tokenizer and source settings. Do not overwrite the file.

```dotenv
FIN_DOC_REFINER_ENABLED=true
FIN_DOC_REFINER_MODE=audit
FIN_DOC_REFINER_ALLOW_REWRITE=false
FIN_DOC_REFINER_API_URL=http://127.0.0.1:8000/v1
FIN_DOC_REFINER_API_KEY=local-placeholder
FIN_DOC_REFINER_MODEL=your-served-refiner
FIN_DOC_REFINER_MODEL_VERSION=your-checkpoint-revision
FIN_DOC_REFINER_TIMEOUT=120
FIN_DOC_REFINER_MAX_TOKENS=3072
FIN_DOC_REFINER_MAX_INPUT_CHARS=12000
FIN_DOC_REFINER_MIN_RETAIN_RATIO=0.5
FIN_DOC_REFINER_PROTECT_FINANCIAL=true
FIN_DOC_REFINER_SYSTEM_PROMPT_FILE=
```

Use an OpenAI Chat Completions-compatible service able to produce the operation protocol below. An unauthenticated local service still needs a nonempty placeholder key for SDK/preflight requirements; authenticated services require their actual key. Pin the model version to a checkpoint/revision.

The default system prompt is FinFlow's own protocol adaptation. `SYSTEM_PROMPT_FILE` optionally loads a UTF-8 system prompt from an absolute path or a project-root-relative path. Its contents, not just its name, participate in the plugin identity. A checkpoint needs the appropriate system prompt, chat template and serving configuration; changing the model name alone is not a paper reproduction.

The character limit is not a model token limit. Inputs first use the existing training-tokenizer splitter. Oversized plugin inputs are recorded as skipped, never silently truncated. Configure the server context window to fit the system prompt, numbered source and output allowance. Plugin calls share the daily model-request budget with other flywheel roles.

```bash
python -m workflow.flywheel_cli preflight
python -m workflow.flywheel_cli run --no-discover
python -m workflow.flywheel_cli refiner-status --limit 20
python -m workflow.flywheel_cli audit --method refined
python -m workflow.flywheel_cli report
```

`--no-discover` skips source discovery but still drains existing work and registers/governs local OCR. Omit it for daily discovery as well.

After inspecting results, change `FIN_DOC_REFINER_MODE=apply`. Set `FIN_DOC_REFINER_ALLOW_REWRITE=true` separately if needed. Restart persistent channel processes after changing configuration.

Set `FIN_DOC_REFINER_ENABLED=false` to disable: plugin model/prompt settings are not read; no refinement jobs are created or claimed; no refinement calls or additional inference dependencies are required. Existing plugin jobs remain available when the same configuration is restored. Jobs belonging to other plugin recipes are not claimed. Reviews of candidates already generated continue in the existing review queue.

## 3. Protocol and guards

Input lines have stable markers such as `<lid:1>`. Example output:

```text
<extract>
rm 1-2
<edit>
sub 4: "Click to view advertisement"
```

The parser accepts `<extract>` followed by `<keep>`, `<delete>`, `<edit>` or `<rewrite>`. It also accepts ReScraper's decision-first format: a leading operation tag, extraction, then the same operation tag. `rm N` / `rm N-M` delete lines. `sub N: "…"` deletes a uniquely matched JSON string in the original line; it is not a replacement command. All indices reference the original numbered input, never renumbered intermediate text.

Out-of-range or overlapping operations, ambiguous substrings, conflicting tags, unknown instructions and truncated responses are rejected. Malformed programs never become candidate text; generated code is never executed. Deletion operations preserve remaining character order.

- Segments containing table, formula or image structures are skipped; existing visual processing remains active. Detection combines block types and text markers and still depends on OCR labels.
- Default heuristic guards check numeric, currency, date-number, common-unit and negation signals. Removing protected content or changing these signals produces `needs_review`. You can disable the heuristic for other domains; independent candidate review still applies.
- Non-delete results must retain at least 0.5 of the input character count by default. This configurable ratio measures length, not semantic faithfulness.
- Sources without approved training use, or inputs failing required OCR quality, do not trigger refiner calls.

These checks do not prove factual correctness. Segment boundaries can lose context, particularly accounting scope, table notes and references. The plugin does not inspect image pixels or automatically fine-tune on Chinese domain data.

## 4. Lineage, recovery and reporting

`processed/refiner/<task_uid>/` stores `response.json` and `result.json`; pre-model skips have no model response. Results include source line/character ranges, source chunk versions, parsed operations, output text, guard signals and configuration identity. Offsets always address governed source evidence, not raw PDF positions or rewritten output. Follow source chunks and OCR lineage for pages and regions.

`refiner-response → refiner-result → training-candidate → training-decision` uses SQLite artifact records and multiple parent edges. Publishing apply candidates includes their ancestor artifacts in the evidence package. Audit/delete/skipped records without candidates remain local. Obtain artifact IDs with `refiner-status`, then inspect them with `trace --artifact-id`.

Task identity binds the source artifact and segment, main pipeline recipe, model revision, prompt contents, mode and guard policy. Identical settings reuse completed tasks and responses. Transport errors use existing retries; daily request exhaustion defers work. Switching audit to apply creates a new task and calls the model again rather than silently publishing an earlier proposal.

Invalid output stays `needs_review` without a candidate. Manual retry reuses an existing response. To regenerate after fixing the service or prompt, update the model revision or prompt contents. Paused jobs remain visible in total queue counts but do not count toward the current executable queue status.

Run reports include operation/status counts, candidate count, cached-response reuse and input/output character counts. Skip, invalid and delete are distinct so failure is not reported as cleaning benefit. Provider-reported usage remains grouped by role; no supplier cost estimates are added.

## 5. Attribution and reproduction boundary

See the [ReScraper paper](https://arxiv.org/abs/2609.34287), [official code](https://github.com/cxcscmu/ReScraper) and [model](https://huggingface.co/cx-cmu/ReScraper). The original method uses a specifically fine-tuned Qwen3-0.6B to jointly extract and refine English web text.

FinFlow independently implements a strict operation adapter as an additional branch over governed financial OCR text. It does not reproduce the original HTML renderer, teacher-data construction, two-stage SFT training or experimental scale. The default prompt is a FinFlow adaptation and does not establish checkpoint quality on Chinese financial text. Alternative models must implement the protocol and use a suitable system prompt. Consult each upstream repository for its license.

This plugin has received static checks only; functional tests and live-model acceptance remain outstanding.
