"""Environment-only options for daily training publication (no provider secrets)."""

from __future__ import annotations

import json
import math
import os


def env_json(name, default):
    try:
        return json.loads(os.environ[name]) if name in os.environ else default
    except (ValueError, TypeError):
        raise ValueError(f"{name} must contain valid JSON") from None


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower() not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true/false or 1/0")
    return value.lower() in {"true", "1"}


def overlay(policy, name):
    values = env_json(name, {})
    if not isinstance(values, dict):
        raise ValueError(f"{name} must be a JSON object")
    return {**policy, **values}


def release_options():
    # Imported lazily: near_dedup imports the flywheel configuration module.
    from training.near_dedup import settings
    from training.mixture_data import FIELDS

    enabled = env_bool("FIN_DOC_RELEASE_ENABLED")
    options = {
        "enabled": enabled,
        "near_dedup": env_bool("FIN_DOC_RELEASE_NEAR_DEDUP", True),
        "near_options": settings(env_json("FIN_DOC_RELEASE_NEAR_OPTIONS", {})),
        "coverage_targets": env_json("FIN_DOC_RELEASE_COVERAGE_TARGETS", []),
        "tracks": {},
    }
    targets = options["coverage_targets"]
    if not isinstance(targets, list) or any(
        not isinstance(t, dict)
        or set(t) != {"field", "label", "min_samples"}
        or t.get("field") not in FIELDS
        or not isinstance(t.get("label"), str)
        or type(t.get("min_samples")) is not int
        or t["min_samples"] < 0
        for t in targets
    ):
        raise ValueError("FIN_DOC_RELEASE_COVERAGE_TARGETS must list field/label/min_samples targets")
    for kind in ("cpt", "sft"):
        config = env_json("FIN_DOC_RELEASE_" + kind.upper() + "_MIXTURE", {})
        defaults = {
            "schema_version": "finflow-mixture-v1",
            "method": "temperature",
            "alpha": 1.0,
            "unit": "tokens" if kind == "cpt" else "samples",
            "group_fields": ["source_name"] if kind == "cpt" else ["task"],
            "budget": None,
            "seed": 42,
            "max_per_family": None,
            "redistribute": True,
            "allow_shortfall": False,
        }
        if not isinstance(config, dict) or set(config) - (set(defaults) | {"weights"}):
            raise ValueError(f"unsupported {kind} daily mixture options")
        config = {**defaults, **config}
        if config["schema_version"] != "finflow-mixture-v1" or config["method"] not in {"fixed", "temperature"}:
            raise ValueError("daily publication supports fixed/temperature; learned weights require a frozen experiment pool")
        if config["unit"] not in ({"tokens", "samples"} if kind == "cpt" else {"samples"}):
            raise ValueError("invalid daily mixture unit")
        fields = config["group_fields"]
        if (
            not isinstance(fields, list)
            or not fields
            or any(not isinstance(f, str) for f in fields)
            or len(set(fields)) != len(fields)
            or not set(fields) <= FIELDS
        ):
            raise ValueError("invalid daily mixture group_fields")
        for key in ("budget", "max_per_family"):
            if config[key] is not None and (type(config[key]) is not int or config[key] <= 0):
                raise ValueError(f"{kind} {key} must be null or a positive integer")
        if type(config["seed"]) is not int or any(type(config[k]) is not bool for k in ("redistribute", "allow_shortfall")):
            raise ValueError("invalid daily mixture seed/boolean options")
        if type(config["alpha"]) not in (int, float) or not math.isfinite(config["alpha"]) or not 0 <= config["alpha"] <= 2:
            raise ValueError("daily mixture alpha must be in [0,2]")
        if config["method"] == "fixed":
            weights = config.get("weights")
            if (
                not isinstance(weights, dict)
                or not weights
                or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in weights.values())
                or sum(weights.values()) <= 0
            ):
                raise ValueError("fixed daily mixture requires nonnegative weights with a positive sum")
        options["tracks"][kind] = config
    return options
