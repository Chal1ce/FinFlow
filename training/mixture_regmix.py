"""RegMix adaptation: designed mixtures -> proxy losses -> LightGBM -> search -> confirmation."""

from __future__ import annotations

import json
import math

import numpy as np
import lightgbm as lgb

from core.flywheel_files import sha256, write_json
from training.mixture_proxy import Experiment


def settings(config, domains):
    values = {"trials": 32, "search_candidates": 4096, "estimators": 200, "num_leaves": 7, "learning_rate": 0.05}
    supplied = config.get("regmix", {})
    if not isinstance(supplied, dict) or set(supplied) - set(values):
        raise ValueError("unknown RegMix setting")
    values.update(supplied)
    for name in ("trials", "search_candidates", "estimators", "num_leaves"):
        if type(values[name]) is not int or values[name] < 2:
            raise ValueError(name + " must be an integer >=2")
    if values["trials"] < max(12, domains + 3):
        raise ValueError("RegMix trials must be >= max(12, domain_count + 3)")
    rate = values["learning_rate"]
    if type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0:
        raise ValueError("RegMix learning_rate must be positive and finite")
    return values


def run(config, data, output, *, max_trials=None):
    experiment = Experiment(config, data, output, "regmix")
    options = settings(config, len(experiment.names))
    if max_trials is not None and (type(max_trials) is not int or max_trials < 1):
        raise ValueError("max_trials must be positive")
    seed, names = experiment.p["seed"], experiment.names
    rng = np.random.default_rng(seed)
    count = len(names)
    natural = np.array([experiment.manifest["tokens"]["train:" + k] for k in names], dtype=float)
    natural /= natural.sum()
    temperature = np.sqrt(natural)
    temperature /= temperature.sum()
    # Baselines, domain vertices, and random interior mixtures span the simplex.
    design, seen = [], set()

    def add(weights):
        key = tuple(np.round(weights, 14))
        if key not in seen:
            seen.add(key)
            design.append(weights)

    for weights in [np.full(count, 1 / count), natural, temperature, *np.eye(count)]:
        add(weights)
    while len(design) < options["trials"]:
        add(rng.dirichlet(np.full(count, [0.3, 1.0, 3.0][len(design) % 3])))
    design = np.array(design)
    design_path = experiment.root / "design.json"
    specification = {"domains": names, "mixtures": design.tolist(), "seed": seed}
    if design_path.exists() and json.loads(design_path.read_text()) != specification:
        raise ValueError("RegMix design changed")
    write_json(design_path, specification)
    results, completed = [], 0
    for index, weights in enumerate(design):
        name = f"trial-{index:04d}"
        existing = (experiment.root / (name + ".json")).exists()
        if not existing and max_trials is not None and completed >= max_trials:
            return {
                "status": "partial",
                "completed_trials": len(results),
                "total_trials": len(design),
                "message": "run the same command/output to resume; no fitted weights have been published",
            }
        model, result = experiment.train(name, dict(zip(names, weights.tolist())))
        del model
        results.append(result)
        completed += int(not existing)
    losses = np.array([r["validation"]["loss"] for r in results], dtype=float)
    if not np.isfinite(losses).all():
        raise ValueError("nonfinite proxy validation targets")
    # Hold out complete mixture experiments; this diagnoses regression generalization, not downstream model quality.
    permutation = rng.permutation(len(design))
    nheld = max(3, len(design) // 5)
    held, train = permutation[:nheld], permutation[nheld:]
    params = {
        "objective": "regression",
        "metric": "l2",
        "num_leaves": options["num_leaves"],
        "learning_rate": options["learning_rate"],
        "min_data_in_leaf": 2,
        "min_data_in_bin": 1,
        "verbosity": -1,
        "num_threads": experiment.p["threads"],
        "seed": seed,
        "deterministic": True,
        "force_col_wise": True,
    }

    def fit(x, y):
        return lgb.train(params, lgb.Dataset(x, label=y), num_boost_round=options["estimators"])

    diagnostic = fit(design[train], losses[train])
    predictions = diagnostic.predict(design[held])
    rmse = float(np.sqrt(np.mean((predictions - losses[held]) ** 2)))
    baseline_rmse = float(np.sqrt(np.mean((losses[train].mean() - losses[held]) ** 2)))
    correlation = None
    if np.std(predictions) > 0 and np.std(losses[held]) > 0:
        correlation = float(np.corrcoef(predictions, losses[held])[0, 1])
    regression = fit(design, losses)
    regression.save_model(str(experiment.root / "regressor.txt"))
    candidates = [*design]
    for i in range(options["search_candidates"]):
        candidates.append(rng.dirichlet(np.full(count, [0.3, 1.0, 3.0][i % 3])))
    candidates = np.array(candidates)
    predicted_losses = regression.predict(candidates)
    if not np.isfinite(predicted_losses).all():
        raise ValueError("nonfinite regression predictions")
    best_index = int(np.argmin(predicted_losses))
    proposed = candidates[best_index]
    # Always train a confirmation proxy. A regression prediction alone never wins over measured evidence.
    model, confirmation = experiment.train("confirmation", dict(zip(names, proposed.tolist())))
    del model
    observed_index = int(np.argmin(losses))
    use_proposed = confirmation["validation"]["loss"] < float(losses[observed_index])
    selected = proposed if use_proposed else design[observed_index]
    report = {
        "regression": {
            "implementation": "LightGBM",
            "version": lgb.__version__,
            "params": params,
            "sha256": sha256(experiment.root / "regressor.txt"),
            "held_out_trial_indices": held.tolist(),
            "held_out_rmse": rmse,
            "held_out_mean_baseline_rmse": baseline_rmse,
            "held_out_pearson": correlation,
            "beats_mean_baseline": rmse < baseline_rmse,
        },
        "trials": [
            {
                "trial": i,
                "weights": dict(zip(names, design[i].tolist())),
                "validation": r["validation"],
                "actual_token_counts": r["token_counts"],
                "checkpoint_sha256": r["checkpoint_sha256"],
            }
            for i, r in enumerate(results)
        ],
        "search": {
            "candidate_count": len(candidates),
            "predicted_loss": float(predicted_losses[best_index]),
            "proposed_weights": dict(zip(names, proposed.tolist())),
        },
        "confirmation": confirmation["validation"],
        "confirmation_checkpoint_sha256": confirmation["checkpoint_sha256"],
        "selection": "confirmed_proposal" if use_proposed else "best_observed_trial",
        "best_observed_trial": observed_index,
        "validation_scope": "mixture tuning; not an untouched final evaluation set",
    }
    write_json(experiment.root / "regression-report.json", report)
    return experiment.export(dict(zip(names, selected.tolist())), report)
