"""Load a frozen research case and its bound files."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

from core.flywheel_files import write_json as write_json

AGENT_ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_VERSION = "finflow-research-0.2.0"
REWARD_VERSION = "research-binary-0.2.0"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def scoped_path(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or Path(relative).is_absolute():
        raise ValueError("case file must remain inside the dataset root")
    return path


def validate_contract(value, name):
    schema = json.loads((AGENT_ROOT / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        raise ValueError(f"invalid {name} contract: {errors[0].message}")


@dataclass(frozen=True)
class ResearchCase:
    manifest: dict
    task: dict
    fixture: dict
    oracle: dict
    registry: dict
    provenance: dict | None = None

    @classmethod
    def load(cls, root, relative, *, allow_design=False):
        manifest = json.loads(scoped_path(root, relative).read_text(encoding="utf-8"))
        validate_contract(manifest, "case")
        if manifest["track"] != "research" or (manifest["split"] == "design_demo" and not allow_design):
            raise ValueError("RL needs an executable research case, not an unexecuted design example")
        values = {}
        for name, binding in manifest["files"].items():
            path = scoped_path(root, binding["path"])
            if digest(path) != binding["sha256"]:
                raise ValueError(f"case file hash mismatch: {name}")
            values[name] = json.loads(path.read_text(encoding="utf-8"))
            if name != "tool_registry":
                validate_contract(values[name], name)
        task, fixture, oracle = (values[key] for key in ("task", "fixture", "oracle"))
        if task["task_id"] != manifest["task_id"] or oracle["task_id"] != task["task_id"]:
            raise ValueError("task/oracle identity mismatch")
        if task["context"]["scope_id"] != fixture["scope_id"] or fixture["track"] != "research" or oracle["track"] != "research":
            raise ValueError("research case scope/track mismatch")
        if fixture["synthetic"] != manifest["synthetic"]:
            raise ValueError("case and fixture synthetic flags disagree")
        provenance = values.get("provenance")
        if not manifest["synthetic"] and not provenance:
            raise ValueError("governed research cases require bound provenance")
        if provenance and (
            provenance["case_id"] != manifest["case_id"]
            or provenance["work_uid"] != manifest["grouping"]["source_group"]
            or provenance["split"] != manifest["split"]
        ):
            raise ValueError("case provenance identity/split mismatch")
        if set(task["allowed_tools"]) - {"search_documents", "read_table", "calculate", "submit_answer"}:
            raise ValueError("unsupported research tool")
        if any(check["kind"] not in {"research_result", "evidence_observed"} for check in oracle["checks"]):
            raise ValueError("this verifier supports numeric research tasks only")
        if not any(check["kind"] == "research_result" for check in oracle["checks"]):
            raise ValueError("research oracle has no numeric result")
        return cls(manifest, task, fixture, oracle, values["tool_registry"], provenance)
