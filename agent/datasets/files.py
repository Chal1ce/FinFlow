"""Shared immutable case bindings for synthetic and governed inputs."""

import json

from agent.environment.cases import (
    ENVIRONMENT_VERSION,
    REWARD_VERSION,
    ResearchCase,
    digest,
    scoped_path,
    validate_contract,
    write_json,
)


def load_cases(dataset):
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != "finflow-research-dataset-v1" or type(manifest.get("synthetic")) is not bool:
        raise ValueError("expected an executable research task dataset, not SFT answers or design trajectories")
    groups, identities = {}, set()
    for split in ("train", "validation", "test"):
        for binding in manifest["splits"].get(split, []):
            if digest(scoped_path(dataset, binding["path"])) != binding["sha256"]:
                raise ValueError("dataset case manifest hash mismatch")
            case = ResearchCase.load(dataset, binding["path"])
            if case.manifest["case_id"] in identities:
                raise ValueError("duplicate case identity in task dataset")
            identities.add(case.manifest["case_id"])
            if case.manifest["split"] != split or case.manifest["synthetic"] != manifest["synthetic"]:
                raise ValueError("case split/synthetic flag differs from dataset manifest")
            if case.manifest["versions"]["environment"] != ENVIRONMENT_VERSION or case.manifest["versions"]["reward"] != REWARD_VERSION:
                raise ValueError("case environment/reward version is not supported")
            for key in ("source_group", "base_snapshot_group", "variant_family"):
                group = (key, case.manifest["grouping"][key])
                if group in groups and groups[group] != split:
                    raise ValueError("source/snapshot/variant group occurs in both training and validation")
                groups[group] = split
            yield split, binding, case


def write_case(root, case, values):
    for kind, value in values.items():
        validate_contract(value, kind)
        relative = f"cases/{case['case_id']}/{kind}.json"
        write_json(root / relative, value)
        case["files"][kind] = {"path": relative, "sha256": digest(root / relative)}
    case["files"]["tool_registry"] = {"path": "tools.json", "sha256": digest(root / "tools.json")}
    validate_contract(case, "case")
    relative = f"cases/{case['case_id']}/case.json"
    write_json(root / relative, case)
    return {"path": relative, "sha256": digest(root / relative)}
