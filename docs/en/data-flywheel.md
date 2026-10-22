---
title: Daily data flywheel
nav_order: 3
---

# Daily data flywheel
{: .no_toc }

Optional extension: the [small-model refiner](text-refiner.md) adds a disabled-by-default CPT branch with `.env` configuration, audit/apply modes and a separate rewrite switch.

## On this page
{: .no_toc }

1. TOC
{:toc}

The entry point is `workflow.flywheel_cli`. It performs discovery → persistent queue
→ download and PDF validation → OCR → visual extraction → text governance → model
candidates and independent review → v7 evidence release → text CPT deltas.
It generates training files; it does not start GPU training. Use `training.cli`
only for legacy v5/v6 release exports.

## 1. Environment and models

Use a project virtual environment on macOS or Linux:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp -n .env.example .env
```

The launcher selects `PYTHON_BIN`, `.venv/bin/python`, `.venv-flywheel/bin/python`,
then system `python3`, in that order. Add real connections to `.env`.

| Role | Configuration prefix; each needs `_API_URL`, `_API_KEY`, `_MODEL` | Enabled when |
| --- | --- | --- |
| Visual description | `FIN_DOC_VISION` | `methods` includes `visual` |
| Independent reviewer | `FIN_DOC_PRETRAIN_REVIEW` | Required for formal candidates; must support images for visual review |
| Translation | `FIN_DOC_PRETRAIN_TRANSLATE` | `methods` includes `translate` |
| Rewriting | `FIN_DOC_PRETRAIN_REWRITE` | `methods` includes `rewrite` |
| Text metadata governance | Existing `LLM_API_URL`, `LLM_API_KEY`, `LLM_MODEL` | `governance_backend=auto` with a key; `none` disables it |

Role configurations also support `_MODEL_VERSION`, `_TIMEOUT`, and `_MAX_TOKENS`.
Change the model version when a provider changes a model under the same name so
cached results remain traceable. Supply an OpenAI-compatible base URL without
credentials or query parameters. API keys are read from the environment and are
not stored in processing records.

```dotenv
FIN_DOC_FLYWHEEL_OCR_BACKEND=local
FIN_DOC_PRETRAIN_TOKENIZER=/absolute/path/to/target-model/tokenizer.json
FIN_DOC_PRETRAIN_MAX_TOKENS=2048
FIN_DOC_FLYWHEEL_MAX_TASKS=100
FIN_DOC_FLYWHEEL_MAX_MODEL_REQUESTS=50
FIN_DOC_FLYWHEEL_MAX_SECONDS=7200
```

Local OCR uses `PADDLEOCR_LOCAL_API_URL`; cloud OCR uses the existing cloud fields.
The default recipe disables page orientation correction and unwarping to preserve
crop coordinates, while keeping table recognition enabled. Override requests with
`FIN_DOC_FLYWHEEL_OCR_OPTIONS`. When old OCR options or coordinate transforms cannot
be verified, extraction records require review instead of guessing a crop.
Prefer region images returned by OCR. Page visualizations in `outputImages` are
not automatically document figures; see the [official OCR result fields](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PP-StructureV3.html).

```sh
python -m workflow.flywheel_cli preflight
```

Preflight checks local dependencies, configuration, and tokenizer files without
requesting services. Authentication, image support, context limits, and response
formats still require a real run.

## 2. Sources and admission

Set targets in `config/collection.json`; generation, quality, and budgets in
`config/flywheel.json`. Academic discovery tracks recent windows and historical
backlogs separately, with a two-day recent overlap and stable-ID deduplication.
Each window keeps its own query, filters, page size, and cursor. A cursor advances
only after a complete page is recorded. Discovery limits apply to whole pages,
so a non-multiple `max_results` may return a partial-page excess. Processing still
respects the task budget. OpenAlex page size is limited to 100; an optional
`OPENALEX_API_KEY` is sent in an Authorization header, not a persisted URL.

The defaults are `original` and `visual`. To enable more methods:

```json
"methods": ["original", "visual", "translate", "rewrite"]
```

`source_policy` maps source names to training admission and allowed methods; it is empty by default.
After confirming the actual permitted use, configure the source, for example:

```json
"source_policy": {
  "cninfo": {"training": "approved", "usage_scope": "internal-only"}
}
```

`source_policy` uses source names as keys. The `training` and `allowed_methods` fields control training admission and candidate methods.

With `require_ocr_pass=true`, OCR warnings hold candidates for review. Original,
visual, translated, and rewritten candidates are assessed separately. Technical
failures have bounded retries; rejections, truncation, split conflicts, and review
holds do not repeatedly call providers. There is no mock fallback in this workflow.
Visual review uses the actual image and recorded OCR context. Numeric differences
are review signals, not a sole admission rule.

## 3. Daily operations

```sh
python -m workflow.flywheel_cli run
python -m workflow.flywheel_cli run --no-discover
python -m workflow.flywheel_cli status
python -m workflow.flywheel_cli status --watch
python -m workflow.flywheel_cli report --latest --format markdown
python -m workflow.flywheel_cli audit --status needs_review --limit 20
python -m workflow.flywheel_cli audit --method visual --limit 10

python -m workflow.flywheel_cli review-candidate \
  --candidate-id CANDIDATE_ID --decision accepted \
  --reviewer REVIEWER --reason 'Checked the original image and source'

python -m workflow.flywheel_cli retry --task-id TASK_ID
python -m workflow.flywheel_cli trace --sample-id SAMPLE_ID
python -m workflow.flywheel_cli trace --candidate-id CANDIDATE_ID
```

`--no-discover` processes backlog and existing OCR without discovering new sources.
Human decisions preserve reviewer, reason, input hash, and prior decision. Human
acceptance still requires source approval. Unresolved image coordinates retain OCR,
page number, bbox, and reason; correct extraction instead of accepting text as a
replacement for image localization.

Use `report --latest --format markdown` for readable diagnostics for the latest
run on the selected date. JSON reports also expose top-level `errors` and
`stop_reasons`: stage failures and budget stops are separate. Error summaries use
fixed messages rather than raw provider exceptions, payloads, or credentials.
Legacy download failures can be enriched from the current download manifest;
these details are labeled `details_from=current_download_manifest`.

### Live progress

For a local run, open another terminal with the same environment and run:

```sh
python -m workflow.flywheel_cli --config config/flywheel.local.json --data-root data status --watch
```

The view refreshes every five seconds and shows the outer run, current document,
stage, elapsed time, last activity, and task counts by stage within the selected
scope. Change the interval with `--interval 10`. Ctrl-C stops viewing; processing
continues. The viewer exits when the run finishes or its process has stopped.
Tasks are added dynamically, so there is no overall completion percentage or ETA.

New workers print progress to stderr and atomically save snapshots to
`manifests/progress/<run_id>.json`. Governance reports completed/total counts for
table summaries, document metadata, and chunk context, plus model requests,
responses, failures, cache hits, and the role awaiting a response. Use
`run --no-progress` to suppress console progress; final JSON stays on stdout.
Snapshots contain counters and identifiers, without prompts, responses, or credentials.

Workers started before this update can keep running. The viewer uses existing
tasks, steps, and cached responses to show their current stage, last completed
model step, and saved response count. That count is a lower bound of successfully
persisted responses, not a live request total; it excludes cache reuse and unsaved
responses. Detailed counters are available from the next run. Use
`status --format text` for one readable snapshot; a single `status` still defaults
to JSON, with a new `progress` field. Status reads local files and the database
without migrations or OCR/model calls.

The default `processing_scope` is `all`, including backfill of existing OCR and
queued work from other sources. For a small run in a shared data root, set
`"processing_scope": "collection"` in the local flywheel JSON. Backfill and task
claims then follow enabled sources and financial stock/year/report-type targets.
Disabling scholarly discovery also excludes its existing queued work. Nothing is
deleted: `queue` remains a global snapshot, while `active_queue` reflects the
selected execution scope. Publication and asset inventories retain their existing
global behavior. Changing scope does not change the processing recipe identity.

Task states are `pending`, `running`, `succeeded`, `retry_wait`, `failed`, `deferred`,
`rejected`, and `needs_review`; replaced attempts become `superseded`. Leases and
a process lock prevent duplicate execution and allow interrupted work to resume.

Exit codes: `0` success or no change; `1` partial completion/backlog; `2` configuration
or whole-run failure. Summaries include errors, queue and visual/candidate states,
model requests/usage, elapsed time, release, and dataset paths. Model request limits
are shared across roles within a run; they are not a persistent daily spending cap.
Missing provider usage is recorded as missing.

Run limits are cooperative: active requests use their own timeouts. Responses are
saved atomically for reuse after interruption. A process may terminate after a
provider charges a request but before its response is saved, requiring another call.

## 4. Local storage and lineage

The default root is `data`; override with `FIN_DOC_DATA_ROOT` or global `--data-root`.
The flywheel database is always `state/pipeline.db` under that root.

| Table or directory | Contents |
| --- | --- |
| `processing_task` | Tasks, dependencies, retries, leases, results |
| `visual_asset` | Images and tables together, with type, source, page, bbox, hash, path, metadata |
| `visual_description` | Versioned descriptions |
| `artifact_edge` | Multiple-parent dependency DAG; cycles are rejected |
| `training_candidate` | Method, content identity, admission, decision |
| `training_sample/training_origin` | Deduplicated samples, all origins, actual split |
| `media/blobs` | Content-addressed images; each occurrence retains its own record |
| `media/tables`, `media/contexts`, `media/unresolved` | Table OCR/Markdown, context evidence, unresolved locations |
| `processed/governed_versions` | Versioned text governance |
| `processed/visual_descriptions`, `processed/transforms` | Raw model responses, prompts, input identities |
| `training/candidates`, `training/decisions` | Candidates and model/human decisions |
| `manifests/daily` | Daily summaries |
| `manifests/progress` | Outer-run stages, substage counters, and model request counts |

Before adding flywheel tables to an existing database, the migration creates
`pipeline.db.before-flywheel.bak` using SQLite's backup API. Migration adds tables;
it does not delete old ones. Operations that write to the database trigger the
migration; `status` and preflight do not. Existing OCR can be backfilled without calling OCR again.
Stop scheduling before restoring a database and preserve the current database,
WAL, and complete data root. Corpus files alone cannot reconstruct every queue
state or human decision.

## 5. Releases and training data

v7 evidence packages live at `published/flywheel/RELEASE_ID`: text, visuals,
descriptions, candidates, decisions, artifact DAG, and evidence files.
`artifacts.jsonl` maps original paths to packaged `evidence` paths. SHA-256 checks
verify file integrity; they are not source authentication or digital signatures.

Training uses frozen candidates from verified v7 packages. Deltas live at
`training/pretrain/DELTA_ID` and contain:

- `corpus.jsonl`: samples with `sample_id`, `text`, `split`, and `token_count`.
- `provenance.jsonl`: sample origins and lineage.
- `excluded.jsonl`: excluded records and reasons.
- `manifest.json`: dataset metadata and recipe.
- `checksums.sha256`: integrity checks.

Counts use the target tokenizer on final text, excluding special tokens added by
downstream training. Reserve room for those tokens in the training program.
No new samples means no empty dataset; new origins alone produce
`training/origin_deltas`. No changes produce `no_change`. Files written before an
interrupted database commit are verified and re-indexed on the next run.

Original, translated, rewritten, and visual candidates from one logical work share
a split. Duplicate-connected works share it too. New connections between already
published train and validation groups isolate conflicting candidates for review.
Published files are preserved. Deduplication and grouping are recipe-specific
(tokenizer, token limit, split ratio, redaction version). Distinct accepted model
versions are appended; older content is not automatically removed.

```sh
python -m workflow.flywheel_cli snapshot \
  --dataset-id finance-cpt-v1 --delta DELTA_1 --delta DELTA_2 \
  --origin-delta OPTIONAL_LINEAGE_DELTA
python -m workflow.flywheel_cli verify data/training/snapshots/finance-cpt-v1
```

Include the original delta for any earlier sample referenced by selected lineage.
Do not mix tokenizer recipes. Approximate deduplication is a bounded character
n-gram Jaccard report; it does not delete samples and records its coverage limits.

## 6. Daily scheduling

`scripts/run_flywheel.sh` selects the Python environment and appends output to
`logs/flywheel.log`; `scripts/run_collection.sh` is a compatibility entry point.
Do not install both the old collector schedule and the flywheel schedule.
Use the cron or launchd templates below to configure scheduling.

The cron template is `ops/fin-doc-flywheel.cron.example`, defaulting to 03:00 in the
host timezone. Replace its repository path and check the system timezone.

On macOS, edit the repository path in
`ops/com.chansn.fin-doc-flywheel.plist.example`, then install:

```sh
mkdir -p "$HOME/Library/LaunchAgents"
cp ops/com.chansn.fin-doc-flywheel.plist.example "$HOME/Library/LaunchAgents/com.chansn.fin-doc-flywheel.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.chansn.fin-doc-flywheel.plist"
```

The template runs at 03:00 and once when loaded. Disable it with:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.chansn.fin-doc-flywheel.plist"
```

macOS uses the system timezone. The persistent queue retains unfinished work while the host or OCR service is unavailable and resumes it later.

## 7. Optional SFT stage

Set `"sft": {"enabled": true, "config": "config/sft.local.json"}` in the local flywheel configuration
to generate QA, JSON extraction and table-calculation SFT after evidence publication and CPT.
It is disabled by default and requires separate generation/review role configuration; table review needs vision support.
Daily summaries expose `sft_dataset`. SFT requests are included in the total count; detailed SFT usage is nested there.
See [SFT data generation](sft.md) for standalone use, formats and resuming.
