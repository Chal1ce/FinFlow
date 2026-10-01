---
title: Configuration troubleshooting
parent: Reference and design
nav_order: 4
---

# Configuration troubleshooting

This guide covers startup checks and missing configuration. See [Quick start](quick-start.md) for installation and basic setup.

## Run preflight

Activate the project Python environment and run from the repository root:

```sh
python -m workflow.flywheel_cli preflight
```

For a custom recipe, use the same global `--config` argument as the run command:

```sh
python -m workflow.flywheel_cli --config config/flywheel.local.json preflight
```

Preflight checks local dependencies, enabled model-role configuration, and tokenizer files without calling remote services. Authentication, image support, and output quality still need confirmation during operation.

## Common missing configuration

| Check | Action |
| --- | --- |
| Missing `.env` | Create local configuration from `.env.example`; add missing fields to an existing file |
| Missing Python dependencies | Activate the project virtual environment and install project dependencies |
| Missing OCR configuration | Configure the selected local or cloud OCR backend |
| Missing text governance configuration | Set the role's URL, key, and model name |
| Missing vision configuration | Configure the vision role and use a service with image support |
| Missing reviewer configuration | Configure the independent reviewer; visual review requires image support |
| Missing tokenizer file | Point to the tokenizer matching the target model |
| Invalid API URL | Check for an empty value and the required scheme and endpoint |

An empty URL can fail both presence and format checks. See [Daily data flywheel](data-flywheel.md) for role configuration.

## Complete configuration

Preserve an existing `.env`. If it does not exist, create it with:

```sh
cp -n .env.example .env
```

Vision, reviewer, and tokenizer fields include:

```dotenv
FIN_DOC_VISION_API_URL=
FIN_DOC_VISION_API_KEY=
FIN_DOC_VISION_MODEL=
FIN_DOC_PRETRAIN_REVIEW_API_URL=
FIN_DOC_PRETRAIN_REVIEW_API_KEY=
FIN_DOC_PRETRAIN_REVIEW_MODEL=
FIN_DOC_PRETRAIN_TOKENIZER=/absolute/path/to/target-model/tokenizer.json
```

Keep keys in local configuration and out of Git. The tokenizer must match the target model.

Run preflight again, then follow [Daily data flywheel](data-flywheel.md). Configure training admission for each source in `source_policy`.
