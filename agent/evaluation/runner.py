"""Run frozen agent cases with a policy and summarize pass@1/pass@k."""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from agent.datasets.files import load_cases
from agent.environment.cases import digest, write_json
from agent.environment.research import ResearchEpisode

from .policies import PolicyError


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def run_episode(policy, case, *, logs=None, strict_evidence=True):
    """Execute one case without a training framework; returns the typed episode and messages."""
    episode = ResearchEpisode(
        case,
        logs=logs,
        policy={"kind": type(policy).__name__, "phase": "evaluation", "on_policy": False},
        strict_evidence=strict_evidence,
    )
    messages = episode.initial_messages()
    try:
        for _ in range(case.task["budgets"]["max_actions"]):
            if episode.done:
                break
            text = policy.act(messages)
            messages.append({"role": "assistant", "content": text})
            observation = episode.step(text)
            if not episode.done:
                messages.append({"role": "user", "content": _json(observation)})
    except PolicyError:
        episode.finish("policy_exhausted")
    if not episode.done:
        episode.finish("step_limit")
    return episode, messages


def _bootstrap(values, *, rounds=2000):
    if not values:
        return None
    rng = random.Random(0)
    means = []
    for _ in range(rounds):
        means.append(sum(rng.choice(values) for _ in values) / len(values))
    means.sort()
    return [means[int(0.025 * rounds)], means[int(0.975 * rounds) - 1]]


def _empty():
    return {
        "cases": 0,
        "valid_cases": 0,
        "runs": 0,
        "valid_runs": 0,
        "run_successes": 0,
        "case_successes": 0,
        "strict_support": 0,
        "reasons": Counter(),
        "actions": 0,
        "turns": 0,
    }


def _finish(bucket):
    bucket["pass_at_1"] = bucket["run_successes"] / bucket["valid_runs"] if bucket["valid_runs"] else None
    bucket["pass_at_k"] = bucket["case_successes"] / bucket["valid_cases"] if bucket["valid_cases"] else None
    bucket["strict_support_rate"] = bucket["strict_support"] / bucket["run_successes"] if bucket["run_successes"] else None
    bucket["mean_actions"] = bucket["actions"] / bucket["runs"] if bucket["runs"] else None
    bucket["mean_turns"] = bucket["turns"] / bucket["runs"] if bucket["runs"] else None
    bucket["reasons"] = dict(bucket["reasons"])
    return bucket


def _add_case(bucket, result):
    bucket["cases"] += 1
    valid = False
    success = False
    for run in result["runs"]:
        bucket["runs"] += 1
        bucket["actions"] += run["actions"]
        bucket["turns"] += run["turns"]
        verdict = run["verdict"]
        if not verdict:
            bucket["reasons"]["incomplete"] += 1
            continue
        bucket["reasons"][verdict.get("reason", "unknown")] += 1
        if not verdict.get("train_eligible"):
            continue
        valid = True
        bucket["valid_runs"] += 1
        if verdict.get("reward") == 1.0:
            bucket["run_successes"] += 1
            success = True
            bucket["strict_support"] += int(bool(verdict.get("strict_support")))
    if valid:
        bucket["valid_cases"] += 1
        bucket["case_successes"] += int(success)


def evaluate(
    dataset,
    output,
    *,
    policy_factory,
    policy_name,
    policy_metadata=None,
    split="test",
    samples=1,
    limit=None,
    strict_evidence=True,
):
    dataset, output = Path(dataset).resolve(), Path(output).resolve()
    if not dataset.is_dir():
        raise ValueError("evaluation dataset directory does not exist")
    if output.exists() or dataset in output.parents or output in dataset.parents:
        raise ValueError("evaluation needs a new directory outside the frozen source dataset")
    if type(samples) is not int or samples < 1:
        raise ValueError("samples must be a positive integer")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be a positive integer")
    cases = [case for current, _, case in load_cases(dataset) if current == split]
    if limit is not None:
        cases = cases[:limit]
    if not cases:
        raise ValueError(f"dataset has no cases in split {split!r}")
    total, families, difficulties = _empty(), defaultdict(_empty), defaultdict(_empty)
    case_rows = []
    for case in cases:
        runs = []
        for index in range(samples):
            logs = output / "cases" / case.manifest["case_id"] / f"run-{index}"
            episode, _ = run_episode(policy_factory(case), case, logs=logs, strict_evidence=strict_evidence)
            runs.append(
                {
                    "index": index,
                    "status": episode.status,
                    "reward": episode.reward,
                    "verdict": episode.verdict,
                    "actions": len(episode.actions),
                    "turns": sum(1 for action in episode.actions if action.get("action")),
                    "episode_id": episode.episode_id,
                }
            )
        family = case.manifest["grouping"]["template_family"]
        difficulty = case.manifest["grouping"].get("difficulty", "unknown")
        row = {
            "case_id": case.manifest["case_id"],
            "split": split,
            "family": family,
            "difficulty": difficulty,
            "runs": runs,
        }
        case_rows.append(row)
        _add_case(total, row)
        _add_case(families[family], row)
        _add_case(difficulties[difficulty], row)
    for bucket in (total, *families.values(), *difficulties.values()):
        _finish(bucket)
    run_values = [
        1.0 if run["verdict"] and run["verdict"].get("reward") == 1.0 else 0.0
        for row in case_rows
        for run in row["runs"]
        if run["verdict"] and run["verdict"].get("train_eligible")
    ]
    case_values = []
    for row in case_rows:
        valid = [run for run in row["runs"] if run["verdict"] and run["verdict"].get("train_eligible")]
        if valid:
            case_values.append(1.0 if any(run["verdict"].get("reward") == 1.0 for run in valid) else 0.0)
    summary = {
        **total,
        "families": {key: value for key, value in sorted(families.items())},
        "difficulties": {key: value for key, value in sorted(difficulties.items())},
        "ci95_pass_at_1": _bootstrap(run_values),
        "ci95_pass_at_k": _bootstrap(case_values),
        "k": samples,
    }
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    result = {
        "format": "finflow-rl-eval-v1",
        "dataset": str(dataset),
        "dataset_version": manifest.get("version"),
        "dataset_sha256": digest(dataset / "manifest.json"),
        "split": split,
        "policy": policy_name,
        "policy_metadata": policy_metadata or {},
        "samples": samples,
        "strict_evidence": strict_evidence,
        "summary": summary,
        "cases": case_rows,
    }
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "eval.json", result)
    return result


def load_replay(path):
    rows = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        case_id, actions = row.get("case_id"), row.get("actions")
        if not isinstance(case_id, str) or not isinstance(actions, list):
            raise ValueError("replay rows need case_id and actions")
        if case_id in rows:
            raise ValueError("duplicate replay case_id")
        rows[case_id] = actions
    return rows
