"""Report valid successes, policy failures, infrastructure failures and group statistics."""

import json
from collections import Counter, defaultdict
from pathlib import Path


def _empty_bucket():
    return {
        "episodes": 0,
        "valid": 0,
        "successes": 0,
        "infrastructure_failures": 0,
        "incomplete": 0,
        "sampled_tokens": 0,
        "reasons": Counter(),
        "tool_codes": Counter(),
        "parse_repairs": Counter(),
        "actions": 0,
        "duration_ms": 0.0,
        "strict_support": 0,
        "extra_evidence": 0,
    }


def _add_episode(bucket, episode):
    bucket["episodes"] += 1
    bucket["sampled_tokens"] += episode.get("total_output_tokens", 0)
    for action in episode.get("actions") or []:
        bucket["actions"] += 1
        bucket["duration_ms"] += action.get("duration_ms", 0.0)
        observation = action.get("observation") or {}
        bucket["tool_codes"][observation.get("code", "unknown")] += 1
        for repair in action.get("parse_repairs") or []:
            bucket["parse_repairs"][repair] += 1
    verdict = episode.get("verdict")
    if not verdict:
        bucket["incomplete"] += 1
        bucket["reasons"]["incomplete"] += 1
        return
    bucket["reasons"][verdict.get("reason", "unknown")] += 1
    if not verdict.get("train_eligible"):
        bucket["infrastructure_failures"] += 1
        return
    bucket["valid"] += 1
    if verdict.get("reward") == 1.0:
        bucket["successes"] += 1
        bucket["strict_support"] += int(bool(verdict.get("strict_support")))
        if verdict.get("reason") == "all_checks_passed_extra_evidence":
            bucket["extra_evidence"] += 1


def _finish_bucket(bucket):
    valid = bucket["valid"]
    bucket["success_rate"] = bucket["successes"] / valid if valid else None
    bucket["strict_support_rate"] = bucket["strict_support"] / bucket["successes"] if bucket["successes"] else None
    bucket["extra_evidence_rate"] = bucket["extra_evidence"] / bucket["successes"] if bucket["successes"] else None
    bucket["mean_actions"] = bucket["actions"] / bucket["episodes"] if bucket["episodes"] else None
    bucket["mean_duration_ms"] = bucket["duration_ms"] / bucket["episodes"] if bucket["episodes"] else None
    bucket["reasons"] = dict(bucket["reasons"])
    bucket["tool_codes"] = dict(bucket["tool_codes"])
    bucket["parse_repairs"] = dict(bucket["parse_repairs"])
    return bucket


def _group_statistics(groups):
    """Group-level view is the signal GRPO actually consumes."""
    total = len(groups)
    zero_std = sum(1 for item in groups.values() if item["zero_std"])
    all_zero = sum(1 for item in groups.values() if item["all_zero"])
    all_one = sum(1 for item in groups.values() if item["all_one"])
    pass_at_k = sum(1 for item in groups.values() if item["any_success"])
    return {
        "groups": total,
        "zero_std_groups": zero_std,
        "all_zero_groups": all_zero,
        "all_one_groups": all_one,
        "zero_std_rate": zero_std / total if total else None,
        "all_zero_rate": all_zero / total if total else None,
        "all_one_rate": all_one / total if total else None,
        "pass_at_k": pass_at_k / total if total else None,
        "mean_group_reward": (sum(item["mean"] for item in groups.values()) / total) if total else None,
        "k": max((item["size"] for item in groups.values()), default=0),
        "zero_std_by_family": dict(
            Counter(
                item["family"] for item in groups.values() if item["zero_std"]
            )
        ),
    }


def summarize(run):
    run = Path(run).resolve()
    manifest_path = run / "finflow-run.json"
    if not manifest_path.is_file():
        raise ValueError("RL run manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_environment = manifest.get("environment")
    expected_reward = manifest.get("reward")
    buckets = defaultdict(_empty_bucket)
    families = defaultdict(_empty_bucket)
    attempts = defaultdict(_empty_bucket)
    difficulties = defaultdict(_empty_bucket)
    groups = defaultdict(lambda: {"rewards": [], "family": "unknown", "difficulty": "unknown"})
    version_mismatch = 0
    for path in sorted((run / "episodes").glob("*/episode.json")):
        episode = json.loads(path.read_text(encoding="utf-8"))
        if (expected_environment and episode.get("environment") != expected_environment) or (
            expected_reward and episode.get("reward_version") != expected_reward
        ):
            version_mismatch += 1
            continue
        policy = episode.get("policy") or {}
        phase = policy.get("phase", "unknown")
        attempt = policy.get("attempt") or (Path(episode["run_manifest"]).stem if episode.get("run_manifest") else "unknown")
        family = (episode.get("grouping") or {}).get("template_family", "unknown")
        difficulty = (episode.get("grouping") or {}).get("difficulty", "unknown")
        key = episode["split"] + "/" + phase
        _add_episode(buckets[key], episode)
        _add_episode(families[key + "/" + family], episode)
        _add_episode(attempts[attempt], episode)
        _add_episode(difficulties[key + "/" + difficulty], episode)
        verdict = episode.get("verdict")
        if not verdict or not verdict.get("train_eligible"):
            continue
        group_key = (attempt, episode["split"], phase, policy.get("optimizer_step"), episode["case_id"])
        group = groups[group_key]
        group["family"] = family
        group["difficulty"] = difficulty
        group["rewards"].append(verdict["reward"])
    for group in groups.values():
        rewards = group["rewards"]
        group["size"] = len(rewards)
        group["mean"] = sum(rewards) / len(rewards)
        group["zero_std"] = len(set(rewards)) == 1
        group["all_zero"] = all(reward == 0.0 for reward in rewards)
        group["all_one"] = all(reward == 1.0 for reward in rewards)
        group["any_success"] = any(reward == 1.0 for reward in rewards)
    for bucket in buckets.values():
        _finish_bucket(bucket)
    for bucket in families.values():
        _finish_bucket(bucket)
    for bucket in attempts.values():
        _finish_bucket(bucket)
    for bucket in difficulties.values():
        _finish_bucket(bucket)
    report = {
        "run": str(run),
        "format": "finflow-rl-report-v2",
        "groups": dict(buckets),
        "families": dict(families),
        "attempts": dict(attempts),
        "difficulties": dict(difficulties),
        "group_stats": _group_statistics(groups),
        "version_mismatch": version_mismatch,
        "versions": {"environment": expected_environment, "reward": expected_reward},
        "grouping": "estimated: attempt/split/phase/optimizer_step/case_id",
        "note": (
            "Online diagnostics, not a frozen benchmark; repeated or resampled cases within one step "
            "may merge into one estimated group."
        ),
    }
    return report
