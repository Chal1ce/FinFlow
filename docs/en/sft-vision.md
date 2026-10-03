---
title: Visual SFT
nav_order: 5.3
---

# Image and table SFT

Generate training examples that actually include images from approved visual candidates and their original crops.
Existing [text SFT](sft.md) remains available; visual tasks use a separate recipe and model roles.

| Task | Input | Output |
| --- | --- | --- |
| `visual_qa` | Original image/table crop and question | An answer supported by visible evidence |
| `table_structure` | A complete, readable, simple rectangular table image and fixed instruction | JSON with `title`, `unit`, `columns`, `rows` |

Both generation and independent review receive the original image. Training messages exclude OCR text and
previous generated descriptions. Review also excludes surrounding OCR context: the answer must be supported
by the image available to the training student. OCR is only a fallible generation hint, never a source of
information outside the image.

## Configure and generate

```sh
cp -n config/sft-vision.json config/sft-vision.local.json
```

Set both roles in local `.env`; both models must accept image inputs:

```dotenv
FIN_DOC_SFT_VISION_GENERATE_API_URL=https://your-provider.example/v1
FIN_DOC_SFT_VISION_GENERATE_API_KEY=YOUR_LOCAL_KEY
FIN_DOC_SFT_VISION_GENERATE_MODEL=YOUR_VISION_GENERATOR
FIN_DOC_SFT_VISION_REVIEW_API_URL=https://your-provider.example/v1
FIN_DOC_SFT_VISION_REVIEW_API_KEY=YOUR_LOCAL_KEY
FIN_DOC_SFT_VISION_REVIEW_MODEL=YOUR_VISION_REVIEWER
```

A visual-only recipe does not require the text `FIN_DOC_SFT_GENERATE_*` / `FIN_DOC_SFT_REVIEW_*` roles.
Mixed recipes also require the corresponding text roles. Preflight checks local configuration;
it does not establish actual provider image support.

```sh
python -m training.sft_cli --config config/sft-vision.local.json preflight
python -m training.sft_cli \
  --config config/sft-vision.local.json \
  --flywheel-config config/flywheel.local.json \
  --data-root data \
  build --dataset-id vision-sft-001 --release YOUR_V7_RELEASE_ID
```

The original local v7 release and database are required. Source admission and upstream candidate decisions
must still be approved. First enable the flywheel `visual` method and complete visual candidate review.
After request/time limits, resume with a new dataset ID and the same recipe/release.

For daily generation, point the local flywheel configuration at this recipe:

```json
"sft": {"enabled": true, "config": "config/sft-vision.local.json"}
```

Daily SFT remains disabled by default. One daily invocation selects one recipe; its `tasks` may combine
all five text/vision tasks.

## Quality constraints

- Each visual example requires evidence regions, specified as integer rectangles in crop-relative 0–1000 coordinates.
  Regions and observations are audit metadata, not training input.
- Review must confirm facts, values, units, dates, image-only answerability and region locations; table transcription also requires completeness.
- Only simple rectangular tables are supported. Merged cells, ambiguous hierarchical headers, clipped images and unreadable text should be skipped or held for review.
- Preserve cell strings, signs and separators without conversion or imputation. Use null only for visibly empty cells, not unreadable content.
- Defaults allow 50 rows, 20 columns and 500 data cells. Oversized outputs are rejected rather than truncated.
- Images default to at most 10 MiB and 40 million pixels, must be single-frame PNG, and are checksum verified. Originals are not resized or modified.

Settings: `max_table_rows`, `max_table_columns`, `max_table_cells`, `max_image_bytes`, `max_image_pixels`.
Question/answer character limits also apply. Exclude large tables or adjust the recipe if complete output
cannot fit the model limit; do not concatenate truncated responses. Region and visual-fact correctness
still rely on model review and human sampling. Images are packaged verbatim without automatic privacy masking.

## Portable exports and loading

SFT v2 separates text and visual training files:

| File | Contents |
| --- | --- |
| `train.jsonl`, `validation.jsonl` | Text-only examples; empty for a visual-only recipe |
| `train.vision.jsonl`, `validation.vision.jsonl` | Visual examples with image references and typed messages |
| `images/<sha256>.png` | Original images copied into the dataset, deduplicated by content hash |
| `images.jsonl` | Hashes, dimensions, byte counts and original artifact lineage |
| `samples.jsonl` | Full examples, tasks, reviews and evidence regions |

Evidence, audit, jobs, manifest and checksum files retain their text-SFT roles.
The manifest adds modality counts, image count and export file names.
Moving the entire dataset directory preserves training image references; complete historical audits still require
the original data directory.

Illustrative structure; the hash and answer below are placeholders, not a generated example:

```json
{
  "sample_id": "sft-example",
  "images": ["images/IMAGE_SHA256.png"],
  "messages": [
    {"role": "system", "content": [{"type": "text", "text": "Answer using only the image."}]},
    {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": "What reporting period is shown?"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "The period printed in the image."}]}
  ]
}
```

On disk, `images` contains paths relative to the dataset directory. Load actual images before passing
records to a trainer. The repository provides a per-record PIL adapter without a transformers, datasets or TRL dependency:

```python
from training.sft_export import load_vision_records

records = load_vision_records("data/training/sft/datasets/vision-sft-001", split="train")
for example in records:
    # example contains messages and actual PIL.Image objects in images.
    # Pass the example to your selected multimodal training program.
    print(len(example["images"]))
```

The message structure follows [TRL's vision data format](https://huggingface.co/docs/trl/main/en/dataset_formats#vision-datasets):
typed content with a separate image list. Configure the target model's processor, chat template, image tokens
and batching in the downstream trainer. No training-framework integration is claimed to have been validated;
this feature does not launch training.

## Verification, splits and versioning

```sh
python -m training.sft_cli verify data/training/sft/datasets/vision-sft-001
python -m workflow.flywheel_cli --data-root data trace --sample-id YOUR_SFT_SAMPLE_ID
```

For v2, `verify` checks checksums, training/canonical sample consistency, image references, dimensions and hashes,
placeholder counts and images crossing train/validation splits. Legacy v1 text packages still receive checksum verification.

Assignments inherit logical-work and CPT constraints, with an additional identical-image constraint.
The same image under different sources or OCR contexts retains one split; historical conflicts exclude the affected group.
Different crops, resizes or re-encodings are outside this byte-level deduplication.
Sample identity includes image hashes, so identical questions on different images are not accidentally merged.

The engine is now `sft-v2`, producing a new recipe identity. Use a new dataset ID when generating.
Old exports and audits remain unchanged; identical model requests may still reuse response caches.
New text/vision exports remain cumulative snapshots, so successive resume snapshots must not be concatenated directly.

## Current status

Visual generation, original-image review, region metadata, portable image exports, PIL loading and extended
verification are implemented. Functional tests, real-model acceptance and downstream VLM training have not run.
No live or paid model requests were made during implementation.

## Descriptions and conversations

`config/sft-vision-methods.json` also enables visual_description and visual_conversation; see [Generation methods](training-methods.md). Only the first user message contains the image placeholder; later turns share that image. Original images remain packaged.
