---
title: Daily training releases and recovery
nav_order: 5.7
---

# Daily training releases, recovery and reports

The optional daily path is **collection → OCR/governance → CPT/SFT → cumulative snapshot → near deduplication → mixture → coverage report → release**.
Keep provider credentials and operating options in the project-root `.env`. Final publication and SFT are disabled by default.

## One environment file

Add fields to your existing `.env`; do not overwrite it. Process environment variables take precedence. Restart persistent channel processes after edits.
Python parses this file as data; do not source JSON configuration through a shell.

```dotenv
FIN_DOC_RELEASE_ENABLED=true
FIN_DOC_RELEASE_NEAR_DEDUP=true
FIN_DOC_REPORT_TIMEZONE=Asia/Shanghai
FIN_DOC_SFT_ENABLED=false
FIN_DOC_FLYWHEEL_POLICY={"methods":["original","visual"],"source_policy":{"your-source":{"training":"approved","usage_scope":"internal-only"}}}
FIN_DOC_RELEASE_CPT_MIXTURE={"method":"temperature","alpha":1.0,"group_fields":["source_name"],"unit":"tokens","budget":null,"seed":42,"max_per_family":null,"redistribute":true,"allow_shortfall":false}
FIN_DOC_RELEASE_SFT_MIXTURE={"method":"temperature","alpha":1.0,"group_fields":["task"],"unit":"samples","budget":null,"seed":42,"max_per_family":null,"redistribute":true,"allow_shortfall":false}
FIN_DOC_RELEASE_NEAR_OPTIONS={"threshold":0.9,"ngram":5,"num_perm":64,"bands":16,"min_chars":80,"max_candidates":10000,"max_chars":200000}
FIN_DOC_RELEASE_COVERAGE_TARGETS=[{"field":"method","label":"original","min_samples":100}]
```

Replace `your-source` with a source approved for your intended training use. Configure the existing model and tokenizer variables from `.env.example`.
JSON must fit on one line. `FIN_DOC_FLYWHEEL_POLICY` overrides top-level fields of the shipped recipe: nested objects such as `source_policy` are replaced completely.
Include all sources you intend to retain. Existing dedicated environment variables take precedence over these overrides.
Templates remain versioned defaults; daily operation does not require editing several JSON files.

For SFT, configure the required roles and add:

```dotenv
FIN_DOC_SFT_ENABLED=true
FIN_DOC_SFT_CONFIG=config/sft.json
FIN_DOC_SFT_POLICY={"tasks":["document_qa","extraction","visual_qa"],"strategies":["direct"],"max_jobs":20,"max_model_requests":40}
```

The recipe path is optional. `visual_qa` requires vision generation and review models. `FIN_DOC_SFT_POLICY` applies to both standalone and daily SFT.
Publication option changes do not invalidate upstream generation caches.

### Mixtures

Daily publication supports `fixed` and `temperature`. Fixed `weights` keys must match the actual pool groups.
CPT uses training-tokenizer tokens or samples; SFT uses samples. Validation remains complete and is neither approximately deduplicated nor mixed.
`budget:null` targets all eligible training units; defaults use `alpha:1`, no family cap and redistribution.
Whole-sample granularity, quotas or family caps may produce a shortfall. By default the candidate mixture and diagnostic remain available but latest does not advance.
`allow_shortfall:true` explicitly permits a nonempty shortfall release; the deficit remains visible.
DoReMi/RegMix remain explicit experiments on frozen pools, without daily proxy training or reuse of mismatched historical weights.

## Run and resume

```sh
python -m workflow.flywheel_cli preflight
python -m workflow.flywheel_cli run
python -m workflow.flywheel_cli publish-training
python -m workflow.flywheel_cli publish-training --sft-dataset data/training/sft/datasets/sft-example
python -m workflow.flywheel_cli status
python -m workflow.flywheel_cli report --date 2026-10-04
```

`publish-training` resumes publication only, without collection, OCR or model calls. It still needs the same data root, source policy, tokenizer file and local dependencies;
SFT recipe identities include model configuration. An explicit SFT snapshot must be complete, match the recipe and remain approved.
Existing schedulers can keep calling `scripts/run_flywheel.sh`. No scheduler is installed automatically.

### Durable records and lineage

The existing `state/pipeline.db` stores:

| Record kind | Purpose |
| --- | --- |
| `training-input` | Verified package identities and candidate/sample parents |
| `training-stage` | Stage inputs, attempts, status, output hash and artifact |
| `training-index` | Persistent near-dedup index registration |
| `training-export-sample` | Exported sample IDs mapped to original lineage |
| `training-release` / `training-latest` | Immutable versions and the database projection of latest |
| `daily-start` / `daily` | Starting inventory, interruption recovery and run reports |

Stage keys bind input inventories and recipes. Completed outputs are verified before reuse. If a directory rename completed before the database write,
recovery registers the artifact and its parents. Missing or changed registered outputs stop publication. Back up the database and files together.
CPT combines all deltas and lineage deltas for the current tokenizer/chunking recipe. SFT uses a cumulative snapshot for the selected evidence release; daily SFT snapshots are not concatenated.
Completed SFT exports with identical evidence, recipe and source policy are reused. Partial generation resumes through existing job/response caches in another immutable export.

Near-dedup indexes are separated by kind, recipe and settings and retain historical fingerprints. Historical matches outside the current pool remain report-only;
removal requires a directly similar retained representative in the current pool. See [near deduplication](near-dedup.md).
Upstream evidence identity excludes downstream SFT, reports and publication artifacts, avoiding recursive version churn.
Time limits are checked between stages; an in-progress verification, copy or MinHash calculation may exceed the soft deadline.

## Immutable versions

```text
data/
  training/pretrain/                 # Original daily CPT deltas
  training/origin_deltas/
  training/snapshots/daily-<hash>/
  training/mixtures/cpt-<hash>/       # May have a quota shortfall
  training/releases/release-<hash>/
    manifest.json
    checksums.sha256
    cpt/
    sft/                            # If enabled, includes original images
    reports/cpt/
    reports/sft/
  training/latest.json
  indexes/daily-<kind>-<recipe>.sqlite
  reports/daily/YYYY-MM-DD.json
  reports/daily/YYYY-MM-DD.md
```

```sh
python -m workflow.flywheel_cli verify-training data/training/releases/release-ACTUAL_HASH
python -m workflow.flywheel_cli trace --sample-id EXPORTED_SAMPLE_ID
```

Each version contains independently verifiable mixture packages and reports. Training files/images are portable; full historical tracing needs the original database and source artifacts.
Recorded source paths are audit metadata, not required loading paths on another machine. Identical inputs/options reuse a version. Historical versions are neither overwritten nor deleted.

`latest.json` advances by atomic replacement only after the complete version and nested packages/reports verify. Incomplete SFT, empty selections, disallowed shortfalls or integrity failures preserve the previous version.
The file pointer is the publication commit point; the next execution repairs its database projection if a process exited between the pointer replacement and database update.
Withdrawn source/candidate approval blocks publication of historical CPT inputs until a reviewed corpus is rebuilt; old versions are not retroactively deleted.
When SFT is enabled both tracks publish together, while original daily CPT deltas remain independently available.

## Complete reports

Per-run JSON lives at `manifests/daily/<run_id>.json`; calendar-day JSON/Markdown uses `FIN_DOC_REPORT_TIMEZONE` under `reports/daily/`.
The CLI, channel report commands and MCP share aggregation semantics:

- Task status, errors, queue backlog, candidate review and image/table inventories; model requests and raw usage without cost estimates.
- Newly registered candidates, visual assets, CPT samples/origins and SFT samples. Recovery registrations count as additions, not fresh model generation.
- Stage built/reused/indexed results, timing, paths and failure reasons.
- Current version, input/removed/selected samples, train/validation sizes, target/actual quotas, groups and shortfalls.
- Version reports include coverage targets and changes against the previous version. Tokenizer changes disable incompatible token comparisons.

Multiple runs on one day sum task/request/addition counters, while queue and dataset inventories remain latest snapshots.
Caught failures attempt to persist a report. Abrupt termination is recorded as interrupted on the next locked run; unpersisted request/task counts are unavailable.
Independent test sets, contamination checks, supervised-token SFT mixtures, automatic gap-filling generation and target-model training are not implemented.
