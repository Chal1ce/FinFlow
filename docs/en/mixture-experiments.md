---
title: DoReMi and RegMix
nav_order: 5.6
---

# DoReMi and RegMix experiments

These commands train real small causal language models and learn mixture weights from losses. The implementation is a single-device,
from-scratch Transformer adaptation, not a reproduction of the papers' scale or reported benchmark results. Daily pipelines never
launch these experiments automatically. Large target-model training remains downstream.

## 1. Dependencies and data

```bash
python -m pip install -e '.[mixture-training]'
cp config/mixture-experiment.json config/mixture-experiment.local.json
```

Set existing CPT v2 snapshot paths, grouping, the same tokenizer.json and its actual `eos_token_id`.
The template EOS is deliberately null: supply the correct ID, not an arbitrary zero. Every domain must already have training and
validation data. Preparation never moves training documents into validation.

```bash
python -m training.mixture_experiments --config config/mixture-experiment.local.json preflight
python -m training.mixture_experiments --config config/mixture-experiment.local.json \
  prepare --output data/training/domains/finance-001
```

Preflight checks path/dependency presence; prepare validates packages, grouping, tokenizer, splits and EOS. Domain packages contain
tokenized train/validation blocks, source sample IDs, token offsets, tokenizer, manifest and checksums.
Training uses document-local fixed-length blocks: **short training tails are dropped** to keep token budgets comparable across mixture
trials. Validation tails are retained and padded labels are masked. The manifest reports discarded tokens by domain. If a domain has
no full training blocks, use shorter sequences or more source data and prepare a new package. The default preparation cap is ten million
target tokens. The current trainer loads the domain package into memory and targets proxy-sized subsets.

## 2. DoReMi

```bash
python -m training.mixture_experiments --config config/mixture-experiment.local.json \
  doremi --data data/training/domains/finance-001 --output data/experiments/doremi-001
```

The command:

1. Trains a reference model using `initial_weights`, uniform by default.
2. Reinitializes the proxy identically and freezes the reference.
3. Samples equal-sized full-block batches from every domain and computes tokenwise `max(proxy_loss - reference_loss, 0)`.
4. Averages by domain, applies exponentiated weight ascent and uniform smoothing, then trains the proxy under the updated weights.
5. Exports the arithmetic mean of domain weights over all proxy updates to `weights.json`.

Stratified equal-sized batches avoid nonuniform-sampling correction. For DoReMi, `batch_size` is **per domain**: each step processes
domain_count × batch_size × sequence_length targets. Reference training uses batch_size as the total batch. Their token budgets
are reported separately; equal steps do not mean equal compute.

Configure `proxy.doremi_eta`, `doremi_smoothing`, `reference_steps` and `steps`. Files include `reference.pt/json` and `doremi.pt/json`,
with model/optimizer state, accumulated weights, steps, losses and domain exposure counts.
Sources: [pinned implementation](https://github.com/sangmichaelxie/doremi/blob/7cde52d1848737aa967ecbdb9e643cf334de160d/doremi/trainer.py),
[paper](https://arxiv.org/abs/2305.10429).

## 3. RegMix

```bash
python -m training.mixture_experiments --config config/mixture-experiment.local.json \
  regmix --data data/training/domains/finance-001 --output data/experiments/regmix-001 --max-trials 4
```

Repeat to continue. `--max-trials` limits newly completed design trials per invocation. Once all design trials finish, the command
also fits the regressor and runs one confirmation proxy. Omit the flag to run to completion.

The command:

1. Designs uniform, natural-size, square-root, single-domain and Dirichlet mixtures with a fixed seed.
2. Trains each with identical initialization, step count, full sequence length and frozen validation blocks.
3. Fits LightGBM to the fixed validation objective; holds out complete mixture experiments to report RMSE, mean-baseline RMSE and Pearson.
4. Searches candidate mixtures for low predicted loss.
5. Actually trains a confirmation proxy. If it fails to beat the best measured design trial, exports that measured trial instead.

Defaults are 32 design trials and 4096 random search candidates. Trial count must be at least `max(12, domain_count+3)`.
These are starter settings, not the original paper scale or a statistical guarantee. Outputs include `design.json`, `trial-*.pt/json`,
`regressor.txt`, `confirmation.pt/json`, `regression-report.json` and `weights.json`. The report preserves held-out diagnostics,
the proposal, confirmation loss and final selection rationale.
Sources: [pinned repository](https://github.com/sail-sg/regmix/tree/dd9d1c3b2d7c1756b1a90f0ad7603068e9856cc6),
[paper](https://arxiv.org/abs/2407.01492).

## 4. Objectives, devices and resume

- `validation_weights` names all domains, allowing zeros; default objective is the uniform mean of domain losses.
- Validation blocks are chosen by stable IDs, capped at `eval_blocks_per_domain`, and shared across trials.
- `proxy.device`: cpu (default), cuda or mps. Hardware must already be available. No models or tokenizers are downloaded.
- Architecture: small pre-norm causal Transformer, tied input/output embeddings, learned positions, zero dropout and AdamW.
- `checkpoint_every` defaults to 50 updates. Rerun the same config/output to resume; updates after the last checkpoint are repeated.
- Experiment identity includes the data manifest, config, implementation version and PyTorch version. Changes require a new directory.
- Deterministic algorithms are enabled; unsupported device operations fail explicitly. Bitwise equivalence across hardware/library versions is not promised.
- Proxy training uses replacement and reports actual target tokens and effective domain epochs. Static exports use no replacement and report shortfalls.

`run.json` records validation block IDs, recipe and method attribution. Held-out RegMix trials measure the regressor's predictive
ability; validation losses still tune mixtures and are **not untouched final test results**. Improvements on a target model require
separate downstream training and evaluation.

## 5. Build the training mixture

```bash
cp config/mixture-learned.json config/mixture-learned.local.json
# Choose method=doremi or regmix, the weights file, and the identical input pool/grouping.
python -m training.mixture_cli --config config/mixture-learned.local.json plan
python -m training.mixture_cli --config config/mixture-learned.local.json \
  build --output data/training/mixtures/learned-001
```

See [Data mixtures and selection](data-mixtures.md) for capacity handling, images and export contracts. The algorithm workflows are
implemented; this repository does not yet provide real-corpus experiment results or reproduced paper scores for FinFlow.
