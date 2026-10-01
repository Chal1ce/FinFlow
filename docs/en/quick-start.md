---
title: Quick start
nav_order: 2
---

# Quick start

Python 3.10 or later is required. Run commands from the repository root.
The daily flywheel currently requires macOS or Linux because it uses Unix file locks;
native Windows is not supported for this entry point.

## 1. Install

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp -n .env.example .env
```

If `.env` already exists, add missing fields without overwriting it.

## 2. Choose a workflow

| Input or goal | Guide |
| --- | --- |
| Daily collection, visual processing, and training data | [Daily data flywheel](data-flywheel.md) |
| Local financial PDF or existing OCR | [Reference guides](reference.md) |
| Text export from v5/v6 releases | [Existing release export](pretraining.md) |

## 3. Configure and run

Configure OCR, the vision model, the independent reviewer, and the target tokenizer
as described in [Model configuration](data-flywheel.md#1-environment-and-models).
Confirm [training admission](data-flywheel.md#2-sources-and-admission) in
`config/flywheel.json`.

```sh
# Local configuration checks; no service requests
python -m workflow.flywheel_cli preflight

# Run once after configuration passes
python -m workflow.flywheel_cli run
python -m workflow.flywheel_cli status
```

For a real example of missing configuration, see the [Environment check record](environment-check.md).

## 4. Schedule daily runs

Complete a small real run and inspect visual descriptions and accepted candidates
before installing [cron or launchd](data-flywheel.md#6-daily-scheduling).

## Read and search

Use the sidebar on desktop or the menu on mobile. Each language has its own search
index. Switch languages using the header; commands and environment variable names
are identical across editions.
