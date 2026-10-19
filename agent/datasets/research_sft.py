"""Execute reference tools to export offline Agent SFT demonstrations, never RL rollouts."""

from __future__ import annotations

import json
from pathlib import Path

from agent.environment.cases import ResearchCase, digest, scoped_path, write_json
from agent.environment.research import ResearchEpisode
from agent.verifiers.research import rounded


def _demonstration(case, logs):
    episode = ResearchEpisode(case, logs=logs, policy={"kind": "deterministic_reference", "on_policy": False})
    messages = episode.initial_messages()

    def call(name, arguments):
        action = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False, separators=(",", ":"))
        observation = episode.step(action)
        messages.append({"role": "assistant", "content": action})
        if not observation["ok"]:
            raise ValueError("reference strategy encountered a tool error")
        if not episode.done:
            messages.append({"role": "user", "content": json.dumps(observation, ensure_ascii=False, separators=(",", ":"))})
        return observation["data"]

    # This exporter currently targets one report and one result per task.
    if len(case.fixture["documents"]) != 1:
        raise ValueError("reference SFT exporter currently supports one report per case")
    checks = [check for check in case.oracle["checks"] if check["kind"] == "research_result"]
    if len(checks) != 1:
        raise ValueError("reference SFT exporter currently supports one numeric result per case")
    check = checks[0]
    document = case.fixture["documents"][0]
    hits = call("search_documents", {"query": document["report_title"]})["matches"]
    required = set(check["evidence_alternatives"][0])
    for table in document["tables"]:
        if required & {cell["cell_id"] for cell in table["cells"]}:
            if table["table_id"] not in {hit["table_id"] for hit in hits}:
                raise ValueError("reference table was not returned by the first search page")
            call("read_table", {"table_id": table["table_id"]})

    def execute(program):
        if "cell_ref" in program:
            return program["cell_ref"]
        operands = [execute(item) for item in program["operands"]]
        return call("calculate", {"operation": program["operation"], "operands": operands})["calculation_id"]

    calculation = execute(check["reference_program"])
    value = rounded(episode.nodes[calculation]["value"], check["decimal_places"])
    result = {key: check[key] for key in ("metric_id", "unit", "entity_id", "scope", "periods")}
    result["value"] = format(value, f".{check['decimal_places']}f")
    call(
        "submit_answer",
        {
            "answer_status": "answered",
            "results": [result],
            "evidence_refs": sorted(required),
            "explanation": "按所列公司、期间及报表口径，使用已读取的支持单元格计算，最终按要求保留小数位。",
        },
    )
    if episode.reward != 1.0:
        raise ValueError("executed reference demonstration did not pass the private verifier")
    return {
        "messages": messages,
        "finflow": {
            "case_id": case.manifest["case_id"],
            "episode_id": episode.episode_id,
            "synthetic": case.manifest["synthetic"],
            "on_policy": False,
            "kind": "executed_reference_sft",
            "reward": episode.reward,
        },
    }


def export_research_sft(dataset, output):
    dataset, output = Path(dataset).resolve(), Path(output).resolve()
    if output.exists() or dataset == output or dataset in output.parents or output in dataset.parents:
        raise ValueError("SFT export needs a new directory outside the source RL dataset")
    manifest_path = dataset / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "finflow-research-dataset-v1":
        raise ValueError("expected an executable research dataset")
    output.mkdir(parents=True)
    counts = {}
    for split in ("train", "validation"):
        count = 0
        with (output / f"{split}.jsonl").open("w", encoding="utf-8") as handle:
            for binding in manifest["splits"].get(split, []):
                if digest(scoped_path(dataset, binding["path"])) != binding["sha256"]:
                    raise ValueError("source case hash mismatch")
                case = ResearchCase.load(dataset, binding["path"])
                if case.manifest["split"] != split:
                    raise ValueError("source case split mismatch")
                row = _demonstration(case, output / "episodes")
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                count += 1
        counts[split] = count
    if not counts["train"]:
        raise ValueError("source dataset has no training cases")
    result = {
        "format": "finflow-research-sft-v1",
        "source_dataset_sha256": digest(manifest_path),
        "rows": counts,
        "on_policy": False,
        "files": {split + ".jsonl": digest(output / (split + ".jsonl")) for split in counts},
    }
    write_json(output / "manifest.json", result)
    return {**result, "output": str(output)}
