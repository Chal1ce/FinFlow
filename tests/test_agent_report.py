import json

from agent.environment.cases import ENVIRONMENT_VERSION, REWARD_VERSION
from agent.trainers.rl.report import summarize


def _episode(
    case_id,
    step,
    reward,
    *,
    phase="train",
    family="revenue_growth",
    difficulty="easy",
    attempt="a1",
    strict=True,
    environment=ENVIRONMENT_VERSION,
):
    return {
        "format": "finflow-research-episode-v1",
        "episode_id": f"{case_id}-{phase}-{step}-{reward}",
        "case_id": case_id,
        "split": "train",
        "grouping": {"template_family": family, "difficulty": difficulty},
        "environment": environment,
        "reward_version": REWARD_VERSION,
        "run_manifest": f"/run/attempts/{attempt}.json",
        "policy": {"phase": phase, "attempt": attempt, "optimizer_step": step, "request_id": f"req-{case_id}-{step}-{reward}"},
        "verdict": {
            "reward": reward,
            "train_eligible": True,
            "reason": "all_checks_passed" if reward == 1.0 else "numeric_value",
            "strict_support": strict and reward == 1.0,
        },
        "actions": [
            {"raw": "{}", "observation": {"code": "ok"}, "duration_ms": 1.0},
            {"raw": "{}", "observation": {"code": "invalid_action"}, "duration_ms": 2.0, "parse_repairs": ["fence"]},
        ],
        "total_output_tokens": 10,
    }


def _write(tmp_path, episodes):
    run = tmp_path / "run"
    (run / "episodes").mkdir(parents=True)
    (run / "attempts").mkdir()
    (run / "attempts" / "a1.json").write_text("{}", encoding="utf-8")
    (run / "finflow-run.json").write_text(
        json.dumps(
            {
                "environment": ENVIRONMENT_VERSION,
                "reward": REWARD_VERSION,
                "recipe": {},
                "rows": {},
            }
        ),
        encoding="utf-8",
    )
    for index, episode in enumerate(episodes):
        directory = run / "episodes" / f"episode-{index}"
        directory.mkdir()
        (directory / "episode.json").write_text(json.dumps(episode), encoding="utf-8")
    return run


def test_report_group_statistics(tmp_path):
    episodes = []
    episodes += [_episode("case-a", 1, reward) for reward in (1.0, 0.0, 0.0, 0.0)]
    episodes += [_episode("case-b", 1, reward) for reward in (0.0, 0.0, 0.0, 0.0)]
    episodes += [_episode("case-c", 1, reward) for reward in (1.0, 1.0, 1.0, 1.0)]
    episodes += [
        _episode("case-d", 2, reward, phase="validation", family="cashflow_share", difficulty="medium")
        for reward in (1.0, 1.0, 0.0, 0.0)
    ]
    episodes.append(_episode("case-old", 1, 1.0, environment="old-environment"))
    report = summarize(_write(tmp_path, episodes))
    stats = report["group_stats"]
    assert report["format"] == "finflow-rl-report-v2"
    assert stats["groups"] == 4
    assert stats["zero_std_groups"] == 2
    assert stats["all_zero_groups"] == 1
    assert stats["all_one_groups"] == 1
    assert stats["pass_at_k"] == 0.75
    assert stats["all_zero_rate"] == 0.25
    assert report["version_mismatch"] == 1
    assert report["attempts"]["a1"]["episodes"] == 16
    assert report["groups"]["train/validation"]["episodes"] == 4
    assert report["families"]["train/train/revenue_growth"]["valid"] == 12
    assert report["families"]["train/train/revenue_growth"]["extra_evidence"] == 0
    assert report["difficulties"]["train/validation/medium"]["valid"] == 4
