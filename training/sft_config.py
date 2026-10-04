"""Local SFT recipes and provider-independent generation limits."""

from __future__ import annotations

import json
import math
from pathlib import Path
from urllib.parse import urlsplit

from config import load_config
from workflow.flywheel_config import ModelRole, digest
from training.sft_strategies import STRATEGIES, supported
from workflow.training_release_config import overlay

TEXT_TASKS = {"document_qa", "extraction", "table_calculation"}
VISION_TASKS = {"visual_qa", "table_structure", "visual_description", "visual_conversation"}
TASKS = TEXT_TASKS | VISION_TASKS


def task_supports(task, kind):
    return (
        kind
        in {
            "document_qa": {"text"},
            "extraction": {"text"},
            "table_calculation": {"table"},
            "visual_qa": {"image", "table"},
            "table_structure": {"table"},
            "visual_description": {"image", "table"},
            "visual_conversation": {"image", "table"},
        }[task]
    )


class SFTConfig:
    def __init__(self, path):
        load_config()  # Load .env with the project's parser, never through a shell.
        self.path = Path(path).resolve()
        self.policy = overlay(json.loads(self.path.read_text(encoding="utf-8")), "FIN_DOC_SFT_POLICY")
        if not isinstance(self.policy, dict):
            raise ValueError("SFT recipe must be an object")
        tasks = self.policy.get("tasks", [])
        if not isinstance(tasks, list) or any(not isinstance(t, str) for t in tasks):
            raise ValueError("SFT tasks must be a list of strings")
        roles = []
        if TEXT_TASKS.intersection(tasks):
            roles.extend(("sft_generate", "sft_review"))
        if VISION_TASKS.intersection(tasks):
            roles.extend(("sft_vision_generate", "sft_vision_review"))
        if self.policy.get("contrastive_filter", False):
            roles.append("sft_target")
        self.roles = {role: ModelRole.load(role) for role in roles}
        self.model_limit = self.policy.get("max_model_requests", 40)
        self.version = digest({"policy": self.policy, "models": {k: v.identity() for k, v in self.roles.items()}, "engine": "sft-v3"})

    def preflight(self):
        errors = []
        if self.policy.get("schema_version") != "sft-recipe-v1":
            errors.append("schema_version must be sft-recipe-v1")
        tasks = self.policy.get("tasks", [])
        if not isinstance(tasks, list) or not tasks or any(not isinstance(t, str) or t not in TASKS for t in tasks):
            errors.append("tasks must be a nonempty list selected from: " + ", ".join(sorted(TASKS)))
        elif len(set(tasks)) != len(tasks):
            errors.append("tasks must not contain duplicates")
        strategies = self.policy.get("strategies", ["direct"])
        if (
            not isinstance(strategies, list)
            or not strategies
            or any(not isinstance(s, str) or s not in STRATEGIES for s in strategies)
            or len(set(strategies)) != len(strategies)
        ):
            errors.append("strategies must be unique names from: " + ", ".join(sorted(STRATEGIES)))
        elif any(not any(supported(s, task) for task in tasks) for s in strategies):
            errors.append("non-direct strategies require document_qa")
        elif any(not any(supported(s, task) for s in strategies) for task in tasks):
            errors.append("every task must have a compatible strategy; use direct for non-QA tasks")
        seeds = self.policy.get("seed_instructions")
        if seeds is not None and (
            not isinstance(seeds, list)
            or not 1 <= len(seeds) <= 100
            or any(not isinstance(s, str) or not s.strip() or len(s) > 2000 for s in seeds)
        ):
            errors.append("seed_instructions must contain 1..100 nonempty strings (max 2000 characters)")
        if type(self.policy.get("strategy_seed", 42)) is not int:
            errors.append("strategy_seed must be an integer")
        if self.policy.get("max_conversation_turns", 4) == 1:
            errors.append("max_conversation_turns must be in [2,8]")
        if type(self.policy.get("contrastive_filter", False)) is not bool:
            errors.append("contrastive_filter must be boolean")
        if self.policy.get("contrastive_filter") and (not isinstance(strategies, list) or "codeclm" not in strategies):
            errors.append("contrastive_filter requires the codeclm strategy")
        for name, default, upper in (
            ("max_jobs", 20, 10000),
            ("max_model_requests", 40, 100000),
            ("samples_per_task", 2, 10),
            ("max_evidence_chars", 12000, 100000),
            ("max_answer_chars", 6000, 100000),
            ("max_question_chars", 2000, 10000),
            ("max_image_bytes", 10485760, 104857600),
            ("max_image_pixels", 40000000, 100000000),
            ("max_table_rows", 50, 500),
            ("max_table_columns", 20, 100),
            ("max_table_cells", 500, 10000),
            ("evolution_rounds", 2, 5),
            ("contrastive_min_gap", 1, 5),
            ("max_conversation_turns", 4, 8),
        ):
            value = self.policy.get(name, default)
            if type(value) is not int or not 1 <= value <= upper:
                errors.append(f"{name} must be an integer in [1,{upper}]")
        duration = self.policy.get("max_seconds", 600)
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            errors.append("max_seconds must be positive and finite")
        fraction = self.policy.get("validation_fraction", 0.05)
        if type(fraction) not in (int, float) or not math.isfinite(fraction) or not 0 <= fraction < 1:
            errors.append("validation_fraction must be in [0,1)")
        for name in ("generation_prompt_version", "review_prompt_version"):
            if not isinstance(self.policy.get(name), str) or not self.policy[name].strip():
                errors.append(f"{name} is required")
        for role, model in self.roles.items():
            if not model.endpoint or not model.key or not model.model:
                errors.append(f"{role}: API_URL, API_KEY and MODEL are required")
            endpoint = urlsplit(model.endpoint)
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
            ):
                errors.append(f"{role}: API_URL must be an HTTP base URL without credentials/query/fragment")
            if not math.isfinite(model.timeout) or model.timeout <= 0 or model.max_tokens <= 0:
                errors.append(f"{role}: timeout and max_tokens must be positive")
        return {
            "status": "failed" if errors else "success",
            "errors": errors,
            "recipe_uid": self.version,
            "tasks": tasks,
            "network_requested": False,
            "image_roles_required": [role for role in self.roles if "vision" in role]
            + (["sft_review"] if "table_calculation" in tasks else []),
        }
