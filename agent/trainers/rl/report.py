"""Report valid successes, policy failures and infrastructure failures separately."""

import json
from collections import Counter
from pathlib import Path


def summarize(run):
    run = Path(run).resolve()
    if not (run / "finflow-run.json").is_file():
        raise ValueError("RL run manifest is missing")
    groups = {}
    for path in sorted((run / "episodes").glob("*/episode.json")):
        episode = json.loads(path.read_text(encoding="utf-8"))
        phase = (episode.get("policy") or {}).get("phase", "unknown")
        key = episode["split"] + "/" + phase
        group = groups.setdefault(
            key,
            {
                "episodes": 0,
                "valid": 0,
                "successes": 0,
                "infrastructure_failures": 0,
                "incomplete": 0,
                "sampled_tokens": 0,
                "reasons": Counter(),
            },
        )
        group["episodes"] += 1
        group["sampled_tokens"] += episode["total_output_tokens"]
        verdict = episode.get("verdict")
        if not verdict:
            group["incomplete"] += 1
            group["reasons"]["incomplete"] += 1
        elif not verdict["train_eligible"]:
            group["infrastructure_failures"] += 1
            group["reasons"][verdict["reason"]] += 1
        else:
            group["valid"] += 1
            group["successes"] += int(verdict["reward"] == 1.0)
            group["reasons"][verdict["reason"]] += 1
    for group in groups.values():
        group["success_rate"] = group["successes"] / group["valid"] if group["valid"] else None
        group["reasons"] = dict(group["reasons"])
    report = {
        "run": str(run),
        "format": "finflow-rl-report-v1",
        "groups": groups,
        "note": "Online rollout diagnostics, not a frozen benchmark; incomplete/infra excluded from valid denominator.",
    }
    return report
