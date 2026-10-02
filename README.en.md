# FinFlow

[中文](README.md) · **English**

[Chinese documentation](https://chal1ce.github.io/FinFlow/zh/) · [English documentation](https://chal1ce.github.io/FinFlow/en/)

Turn financial documents into traceable pretraining data.

FinFlow connects source discovery, OCR, text and visual governance, multimodal
descriptions, optional translation/rewriting, and independent quality review.
PDFs, images, tables, and derived files stay on local disk; SQLite tracks tasks,
versions, and lineage. A target tokenizer segments accepted text for continual
pretraining (CPT).

## Features

- Collection from CNINFO, SSE, Crossref, and OpenAlex; manual PDF input.
- PaddleOCR integration, cleaning, metadata normalization, and quality checks.
- Image/table crops in one asset table, retaining table OCR and visual descriptions.
- Independent review, target-tokenizer segmentation, and grouped splits.
- Incremental runs, bounded retries, recovery, deduplication, lineage, immutable releases.
- Feishu commands, PDF imports, human visual review, reports, and an MCP interface for assistants.

```mermaid
flowchart LR
    source[Discovery / local PDF] --> raw[PDF and origins]
    raw --> ocr[OCR]
    ocr --> text[Text governance]
    ocr --> visual[Image / table descriptions]
    text --> candidate[Original / translation / rewrite]
    visual --> candidate
    candidate --> review[Independent review]
    review --> release[Evidence and CPT data]
    state[SQLite tasks / versions / lineage] -.-> raw
    state -.-> candidate
    state -.-> release
```

FinFlow generates training files. A downstream training program trains the model.

## Quick start

Python 3.10+ is required. The daily flywheel currently runs on macOS/Linux:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp -n .env.example .env
```

Add missing fields to an existing `.env` without overwriting it. Follow the
[flywheel guide](docs/en/data-flywheel.md) to configure OCR, vision, independent
review, and the target tokenizer. Confirm source admission in
[config/flywheel.json](config/flywheel.json). Defaults use original text and visual
descriptions; translation/rewriting require additional configuration.

```sh
python -m workflow.flywheel_cli preflight
python -m workflow.flywheel_cli run
python -m workflow.flywheel_cli status
```

Preflight checks local configuration without service requests. See
[scheduling](docs/en/data-flywheel.md#6-daily-scheduling) for cron/launchd templates.
For manual financial workflows or existing OCR, use `python -m workflow.cli list`.

## Documentation

| Goal | Guide |
| --- | --- |
| Installation and choosing a workflow | [Quick start](docs/en/quick-start.md) |
| Models, admission, review, retries, lineage, releases | [Daily data flywheel](docs/en/data-flywheel.md) |
| Feishu operations or OpenClaw / Hermes access | [App integrations and MCP](docs/en/app-integrations.md) |
| Existing v5/v6 exports | [Existing release export](docs/en/pretraining.md) |
| Actual missing configuration | [Environment check](docs/en/environment-check.md) |
| Detailed OCR, acquisition, governance, CLI, operations | [Reference guides; detailed pages in Chinese](docs/en/reference.md) |
| Add bilingual pages or images | [Documentation authoring](docs/en/docs-authoring.md) |
| Publish the website | [GitHub Pages](docs/en/github-pages.md) |

## Project status

149 offline tests passed before the public repository migration. The new application
integration has passed static checks; live channel/client validation is pending. Real OCR/model
integration and quality still require configured validation. Scheduler templates
are not installed automatically. The formal flywheel has no mock fallback;
unconfirmed source usage or quality issues remain reviewable.

Development commands:

```sh
python -m pip install -e '.[dev]'
python -m pytest -q
python -m ruff check .
```

The [design plan](docs/data-flywheel-plan.md) is currently in Chinese.
