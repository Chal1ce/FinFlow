import json


from agent.datasets.files import load_cases
from agent.datasets.research import build_research_dataset
from agent.datasets.research_sft import export_episode_sft, export_research_sft, reference_actions
from agent.environment.research import ResearchEpisode
from agent.evaluation.policies import ReferencePolicy, ReplayPolicy
from agent.evaluation.runner import evaluate, load_replay, run_episode


def _dataset(tmp_path):
    build_research_dataset(tmp_path / "tasks", train_cases=3, validation_cases=1, test_cases=2)
    return tmp_path / "tasks"


def test_reference_eval_and_frozen_test_split(tmp_path):
    dataset = _dataset(tmp_path)
    result = evaluate(
        dataset,
        tmp_path / "eval",
        policy_factory=ReferencePolicy,
        policy_name="reference",
        split="test",
        samples=2,
    )
    assert result["summary"]["cases"] == 2
    assert result["summary"]["pass_at_1"] == 1.0
    assert result["summary"]["pass_at_k"] == 1.0
    assert (tmp_path / "eval" / "eval.json").is_file()
    assert len(result["cases"]) == 2


def test_replay_policy_eval(tmp_path):
    dataset = _dataset(tmp_path)
    rows = []
    for split, _, case in load_cases(dataset):
        if split == "test":
            rows.append({"case_id": case.manifest["case_id"], "actions": reference_actions(case)})
    replay = tmp_path / "replay.jsonl"
    replay.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    actions = load_replay(replay)
    result = evaluate(
        dataset,
        tmp_path / "eval",
        policy_factory=lambda case: ReplayPolicy(case, actions[case.manifest["case_id"]]),
        policy_name="replay",
        split="test",
    )
    assert result["summary"]["pass_at_1"] == 1.0
    assert result["summary"]["reasons"] == {"all_checks_passed": 2}


def test_reference_episode_and_export_excludes_test(tmp_path):
    dataset = _dataset(tmp_path)
    case = next(case for split, _, case in load_cases(dataset) if split == "train")
    episode, messages = run_episode(ReferencePolicy(case), case)
    assert episode.reward == 1.0
    assert messages[-1]["role"] == "assistant"

    result = export_research_sft(dataset, tmp_path / "sft", variants=3)
    assert result["rows"]["train"] > 0
    assert result["rows"]["validation"] > 0
    assert "test" not in result["rows"]
    assert result["variants"] == 3


def test_episode_rejection_export(tmp_path):
    dataset = _dataset(tmp_path)
    case = next(case for split, _, case in load_cases(dataset) if split == "train")
    logs = tmp_path / "episodes"
    episode = ResearchEpisode(case, logs=logs, policy={"phase": "train", "kind": "on_policy_rollout"})
    for action in reference_actions(case):
        episode.step(action)
    assert episode.reward == 1.0
    result = export_episode_sft(dataset, logs, tmp_path / "policy-sft")
    assert result["rows"] == {"train": 1}
    row = json.loads((tmp_path / "policy-sft" / "train.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["finflow"]["on_policy"] is True
    assert row["finflow"]["kind"] == "policy_success_sft"
