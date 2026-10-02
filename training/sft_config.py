"""Local SFT recipes and provider-independent generation limits."""

from __future__ import annotations

import json
import math
from pathlib import Path
from urllib.parse import urlsplit

from config import load_config
from workflow.flywheel_config import ModelRole, digest

TEXT_TASKS = {"document_qa", "extraction", "table_calculation"}
VISION_TASKS = {"visual_qa", "table_structure"}
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
        }[task]
    )


class SFTConfig:
    def __init__(self, path):
        load_config()  # Load .env with the project's parser, never through a shell.
        self.path = Path(path).resolve()
        self.policy = json.loads(self.path.read_text(encoding="utf-8"))
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
        self.roles = {role: ModelRole.load(role) for role in roles}
        self.model_limit = self.policy.get("max_model_requests", 40)
        self.version = digest({"policy": self.policy, "models": {k: v.identity() for k, v in self.roles.items()}, "engine": "sft-v2"})

    def preflight(self):
        errors = []
        if self.policy.get("schema_version") != "sft-recipe-v1":
            errors.append("schema_version must be sft-recipe-v1")
        tasks = self.policy.get("tasks", [])
        if not isinstance(tasks, list) or not tasks or any(not isinstance(t, str) or t not in TASKS for t in tasks):
            errors.append("tasks must be a nonempty list selected from: " + ", ".join(sorted(TASKS)))
        elif len(set(tasks)) != len(tasks):
            errors.append("tasks must not contain duplicates")
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
