"""Execute reference tools to export offline Agent SFT demonstrations, never RL rollouts."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path

from agent.datasets.files import load_cases
from agent.environment.cases import digest, write_json
from agent.environment.research import ResearchEpisode
from agent.verifiers.research import rounded


def _demonstration(case, logs, *, thorough=False, query_mode="title"):
    episode = ResearchEpisode(case, logs=logs, policy={"kind": "deterministic_reference", "on_policy": False})
    messages = episode.initial_messages()

    def call(name, arguments):
        if episode.done:
            raise ValueError("reference strategy exhausted the task action budget")
        action = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False, separators=(",", ":"))
        observation = episode.step(action)
        messages.append({"role": "assistant", "content": action})
        if not observation["ok"]:
            raise ValueError("reference strategy encountered a tool error")
        if not episode.done:
            messages.append({"role": "user", "content": json.dumps(observation, ensure_ascii=False, separators=(",", ":"))})
        return observation["data"]

    checks = [check for check in case.oracle["checks"] if check["kind"] == "research_result"]
    required = {ref for check in checks for ref in check["evidence_alternatives"][0]}
    for document in case.fixture["documents"]:
        required_tables = [table for table in document["tables"] if required & {cell["cell_id"] for cell in table["cells"]}]
        targets = document["tables"] if thorough else required_tables
        if not targets:
            continue
        query = {"query": document["report_title"], "entity_id": document["entity_id"], "period": document["report_period"]}
        if query_mode == "row":
            labels = [
                cell["row"]
                for table in targets
                for cell in table["cells"]
                if cell["cell_id"] in required
            ]
            if labels:
                query["query"] = labels[0]
        pending = {table["table_id"] for table in targets}
        while pending:
            page = call("search_documents", query)
            pending.difference_update(hit["table_id"] for hit in page["matches"])
            if pending and not page["next_cursor"]:
                raise ValueError("reference table is not discoverable through the public search tool")
            query = {**query, "cursor": page["next_cursor"]}
        for table in targets:
            call("read_table", {"table_id": table["table_id"]})

    def execute(program):
        if "cell_ref" in program:
            return program["cell_ref"]
        operands = [execute(item) for item in program["operands"]]
        return call("calculate", {"operation": program["operation"], "operands": operands})["calculation_id"]

    results = []
    for check in checks:
        calculation = execute(check["reference_program"])
        value = rounded(episode.nodes[calculation]["value"], check["decimal_places"])
        result = {key: check[key] for key in ("metric_id", "unit", "entity_id", "scope", "periods")}
        result["value"] = format(value, f".{check['decimal_places']}f")
        results.append(result)
    call(
        "submit_answer",
        {
            "answer_status": "answered",
            "results": results,
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
            "variant": {"thorough": thorough, "query_mode": query_mode},
        },
    }


def reference_actions(case):
    """Return the deterministic reference tool calls for one case.

    This is the same executed path used to build offline SFT, exposed for evaluation
    and replay without importing a model backend.
    """
    row = _demonstration(case, None)
    return [message["content"] for message in row["messages"] if message["role"] == "assistant"]


def _row_hash(row):
    return hashlib.sha256(json.dumps(row["messages"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def export_research_sft(dataset, output, *, variants=1):
    dataset, output = Path(dataset).resolve(), Path(output).resolve()
    if output.exists() or dataset == output or dataset in output.parents or output in dataset.parents:
        raise ValueError("SFT export needs a new directory outside the source RL dataset")
    if type(variants) is not int or not 1 <= variants <= 4:
        raise ValueError("variants must be an integer from 1 to 4")
    manifest_path = dataset / "manifest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".research-sft-", dir=output.parent) as temporary:
        staging = Path(temporary) / "dataset"
        staging.mkdir()
        counts = {"train": 0, "validation": 0}
        skipped_variants = Counter()
        with (
            (staging / "train.jsonl").open("w", encoding="utf-8") as train,
            (staging / "validation.jsonl").open("w", encoding="utf-8") as validation,
        ):
            for split, _, case in load_cases(dataset):
                if split == "test":
                    continue
                seen = set()
                for variant in range(variants):
                    try:
                        row = _demonstration(
                            case,
                            staging / "episodes",
                            thorough=variant % 2 == 1,
                            query_mode="row" if variant >= 2 else "title",
                        )
                    except ValueError as exc:
                        # The reference path is required. Optional exploration paths
                        # may exceed the task budget or need a different search query.
                        if variant == 0:
                            raise
                        skipped_variants[f"variant-{variant}: {exc}"] += 1
                        continue
                    row_hash = _row_hash(row)
                    if row_hash in seen:
                        continue
                    seen.add(row_hash)
                    row["finflow"]["variant_index"] = variant
                    (train if split == "train" else validation).write(json.dumps(row, ensure_ascii=False) + "\n")
                    counts[split] += 1
        if not counts["train"]:
            raise ValueError("source dataset has no training cases")
        result = {
            "format": "finflow-research-sft-v1",
            "source_dataset_sha256": digest(manifest_path),
            "rows": counts,
            "variants": variants,
            "skipped_variants": dict(sorted(skipped_variants.items())),
            "on_policy": False,
            "files": {split + ".jsonl": digest(staging / (split + ".jsonl")) for split in counts},
        }
        write_json(staging / "manifest.json", result)
        staging.rename(output)
    return {**result, "output": str(output)}


def _replay_success(case, actions):
    episode = ResearchEpisode(case)
    for action in actions:
        if episode.done:
            return False
        episode.step(action["raw"])
    return episode.reward == 1.0


def export_episode_sft(dataset, episodes, output, *, per_case=1):
    """Export successful on-policy episodes as SFT rows (rejection sampling).

    Only terminal episodes with reward 1, no invalid non-terminal action and a train
    split are reused. Each row is replayed against the frozen case before export.
    """
    dataset, episodes, output = Path(dataset).resolve(), Path(episodes).resolve(), Path(output).resolve()
    if output.exists() or dataset in output.parents or output in dataset.parents:
        raise ValueError("episode SFT export needs a new directory outside the source dataset")
    if type(per_case) is not int or per_case < 1:
        raise ValueError("per-case must be a positive integer")
    cases = {case.manifest["case_id"]: case for _, _, case in load_cases(dataset)}
    rows, skipped = [], Counter()
    selected = Counter()
    for path in sorted(episodes.glob("*/episode.json")):
        episode = json.loads(path.read_text(encoding="utf-8"))
        case = cases.get(episode.get("case_id"))
        if case is None:
            skipped["unknown_case"] += 1
            continue
        if case.manifest["split"] != "train":
            skipped["not_train_split"] += 1
            continue
        policy = episode.get("policy") or {}
        if not policy or policy.get("phase") != "train":
            skipped["not_on_policy_train"] += 1
            continue
        verdict = episode.get("verdict")
        if not verdict or not verdict.get("train_eligible") or verdict.get("reward") != 1.0:
            skipped["not_successful"] += 1
            continue
        actions = episode.get("actions") or []
        if not actions or not actions[-1].get("action") or actions[-1]["action"].get("name") != "submit_answer":
            skipped["not_terminal_submit"] += 1
            continue
        if any((item.get("observation") or {}).get("code") != "ok" for item in actions[:-1]):
            skipped["contains_invalid_action"] += 1
            continue
        if selected[case.manifest["case_id"]] >= per_case:
            skipped["per_case_limit"] += 1
            continue
        if not _replay_success(case, actions):
            skipped["replay_failed"] += 1
            continue
        messages = ResearchEpisode(case).initial_messages()
        for index, item in enumerate(actions):
            messages.append({"role": "assistant", "content": item["raw"]})
            if index < len(actions) - 1:
                messages.append(
                    {
                        "role": "user",
                        "content": json.dumps(item["observation"], ensure_ascii=False, separators=(",", ":")),
                    }
                )
        rows.append(
            {
                "messages": messages,
                "finflow": {
                    "case_id": case.manifest["case_id"],
                    "episode_id": episode["episode_id"],
                    "synthetic": case.manifest["synthetic"],
                    "on_policy": True,
                    "kind": "policy_success_sft",
                    "reward": 1.0,
                },
            }
        )
        selected[case.manifest["case_id"]] += 1
    if not rows:
        raise ValueError("no successful on-policy episodes to export: " + json.dumps(dict(skipped), ensure_ascii=False))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".research-episodes-", dir=output.parent) as temporary:
        staging = Path(temporary) / "dataset"
        staging.mkdir()
        (staging / "train.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        result = {
            "format": "finflow-research-sft-v1",
            "source_episodes": str(episodes),
            "rows": {"train": len(rows)},
            "on_policy": True,
            "skipped": dict(sorted(skipped.items())),
            "files": {"train.jsonl": digest(staging / "train.jsonl")},
        }
        write_json(staging / "manifest.json", result)
        staging.rename(output)
    return {**result, "output": str(output)}
