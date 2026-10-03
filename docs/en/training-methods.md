---
title: Training-data methods
nav_order: 5.4
---

# Training-data generation methods

Tasks define the skill, strategies define candidate generation, and mixture recipes select approved examples.
Existing recipes default to `direct`. New methods require explicit configuration.

## CPT transformations

Add any of these names to `methods` in `config/flywheel.local.json`:

| Method | Output |
| --- | --- |
| `distill` | Information-dense prose retaining necessary qualifications |
| `textbook` | Educational organization of concepts and rules explicitly explained by the source |
| `knowledge_list` | Facts retaining subjects, metrics, values, units and periods |
| `diverse_qa` | Diverse reading-comprehension QA text supported by the document |

Configure the shared `FIN_DOC_PRETRAIN_SYNTHESIZE_API_URL`, `_API_KEY`, `_MODEL` role in `.env`.
Independent review continues to use `FIN_DOC_PRETRAIN_REVIEW_*`; translate/rewrite retain their existing roles.
Endpoints are OpenAI-compatible base URLs. Candidates are stored separately from original text and must not add outside facts.

These transformations adapt [Nemotron-CC task patterns](https://docs.nvidia.com/nemo/curator/curate-text/synthetic/nemotron-cc/tasks)
to FinFlow evidence, review and lineage, without installing NeMo. CPT QA is plain training text; it is not automatically approved SFT.

## Multi-step text SFT

```bash
cp config/sft-methods.json config/sft-methods.local.json
python -m training.sft_cli --config config/sft-methods.local.json \
  --flywheel-config config/flywheel.local.json preflight
python -m training.sft_cli --config config/sft-methods.local.json \
  --flywheel-config config/flywheel.local.json \
  build --dataset-id sft-methods-001 --release YOUR_RELEASE_ID
```

Configure `tasks` and `strategies` separately. Non-direct strategies currently support `document_qa` only.
Extraction, verified calculations and transcription retain specialized direct contracts. Every enabled task/strategy must have
a compatible counterpart, or preflight fails.

| Strategy | Implemented stages | Adaptation boundary |
| --- | --- | --- |
| `direct` | Generate → deterministic checks → independent review | Existing baseline |
| `self_instruct` | Expand curated seeds → screen instructions → generate answers → review | One source-bound expansion; no cross-document self-bootstrapping seed pool |
| `answer_first` | Select verbatim answer → inverse question → screen → review original answer | API adaptation; no reverse-model training or iterative Humpback fine-tuning |
| `evol_instruct` | Basic questions → constraints/scope/comparison evolution → information-gain and answerability screening → answers/review | Confined to current evidence; no open-domain topic expansion |
| `codeclm` | Encode use case/skills/rubric → decode instructions → rubric refinement → answers/review | Optional target-model comparison; no target-model fine-tuning |

Sources: [Self-Instruct](https://github.com/yizhongw/self-instruct), [Evol-Instruct](https://arxiv.org/abs/2304.12244),
[Instruction Backtranslation](https://arxiv.org/abs/2308.06259),
[CodecLM](https://research.google/blog/codeclm-aligning-language-models-with-tailored-synthetic-data/).

### Configuration

- `seed_instructions`: optional 1–100 seed instructions, up to 2000 characters each; five task shapes are built in.
  Do not put validation answers in the seed library.
- `strategy_seed`: seed/operator selection seed, default 42.
- `evolution_rounds`: 1–5, default 2.
- `contrastive_filter`: false by default; true requires `codeclm` and `FIN_DOC_SFT_TARGET_*`.
- `contrastive_min_gap`: integer teacher-minus-target rubric score gap, 1–5, default 1.

Generation uses `FIN_DOC_SFT_GENERATE_*`; instruction screening and final review use `FIN_DOC_SFT_REVIEW_*`.
For contrastive filtering, the target receives only source evidence and the question. The reviewer scores both answers against
the rubric; the teacher answer still requires final factual review. Score gaps are selection signals, not proof of downstream improvement.

Jobs retain intermediate responses, stage artifacts, model roles and final review. Samples expose `strategy` and `strategy_stages`.
Recipe identities include strategy/seed settings and model identities; the engine is now `sft-v3`, while export contracts remain SFT v2.
Existing packages remain immutable. Multi-step methods consume more requests; after a partial run, use a new dataset ID with the same
recipe to reuse cached responses. Do not concatenate cumulative resume snapshots. Seeds stabilize workflow choices, not remote model
outputs; cached responses preserve what actually happened.

## Additional visual tasks

Copy `config/sft-vision-methods.json` to a local recipe to enable:

- `visual_description`: visible structure, labels, units and trends; no guessed precise values from ambiguous charts.
- `visual_conversation`: 2–`max_conversation_turns` coherent QA turns about the same image; default maximum 4, supported 2–8.

Use the existing `FIN_DOC_SFT_VISION_GENERATE_*` and `FIN_DOC_SFT_VISION_REVIEW_*` roles. Review checks each turn against the
original image and rejects contradictions or unsupported inferences. Only the first user message includes the image placeholder.
Per-turn limits and the total `max_answer_chars` cap apply. Existing visual QA and table JSON transcription can run alongside them.
Category design follows [LLaVA's data guide](https://github.com/haotian-liu/LLaVA/blob/main/docs/Data.md); using actual vision APIs
is an adaptation of that workflow.

## Selection and publication

Generation counts do not determine final training proportions. Review and deduplicate before [mixture selection](data-mixtures.md).
[DoReMi / RegMix](mixture-experiments.md) run explicit local proxy training independently of daily SFT/API calls.
`training/method_cards.py` records attribution and adaptation boundaries; relevant SFT/experiment manifests include these cards.
