import json

import pytest

from agent.datasets.files import load_cases
from agent.datasets.research import build_research_dataset
from agent.datasets.research_sft import reference_actions
from agent.environment.research import ResearchEpisode


def _case(tmp_path):
    build_research_dataset(tmp_path / "tasks", train_cases=3, validation_cases=1)
    for _, _, case in load_cases(tmp_path / "tasks"):
        if case.manifest["grouping"]["template_family"] == "revenue_growth":
            return case
    raise AssertionError("case not found")


def _prepared(case, *, strict_evidence=False):
    episode = ResearchEpisode(case, strict_evidence=strict_evidence)
    actions = reference_actions(case)
    for action in actions[:-1]:
        episode.step(action)
    check = next(item for item in case.oracle["checks"] if item["kind"] == "research_result")
    calculation = episode.actions[-1]["observation"]["data"]["calculation_id"]
    return episode, check, calculation, actions[-1]


def _submit(episode, check, value, refs, *, periods=None):
    result = {
        "metric_id": check["metric_id"],
        "value": value,
        "unit": check["unit"],
        "entity_id": check["entity_id"],
        "scope": check["scope"],
        "periods": periods if periods is not None else check["periods"],
    }
    episode.step(
        json.dumps(
            {
                "name": "submit_answer",
                "arguments": {
                    "answer_status": "answered",
                    "results": [result],
                    "evidence_refs": refs,
                    "explanation": "x",
                },
            },
            ensure_ascii=False,
        )
    )


def test_correct_answer_passes(tmp_path):
    case = _case(tmp_path)
    episode, check, calculation, _ = _prepared(case)
    _submit(episode, check, check["expected_value"], [calculation])
    assert episode.verdict["reward"] == 1.0
    assert episode.verdict["strict_support"] is True


def _read_distractor(episode, case):
    document = case.fixture["documents"][0]
    for table in document["tables"]:
        observation = episode.step(json.dumps({"name": "read_table", "arguments": {"table_id": table["table_id"]}}, ensure_ascii=False))
        if observation["ok"]:
            for cell in observation["data"]["cells"]:
                if cell["cell_id"] not in {
                    ref
                    for check in case.oracle["checks"]
                    if check["kind"] == "research_result"
                    for ref in check["evidence_alternatives"][0]
                }:
                    return cell["cell_id"]
    return None


def test_extra_evidence_passes_training_but_not_strict(tmp_path):
    case = _case(tmp_path)
    episode, check, calculation, _ = _prepared(case)
    extra = _read_distractor(episode, case)
    assert extra is not None
    _submit(episode, check, check["expected_value"], [*check["evidence_alternatives"][0], extra])
    assert episode.verdict["reward"] == 1.0
    assert episode.verdict["reason"] == "all_checks_passed_extra_evidence"
    assert episode.verdict["strict_support"] is False

    strict_episode, strict_check, strict_calc, _ = _prepared(case, strict_evidence=True)
    strict_extra = _read_distractor(strict_episode, case)
    assert strict_extra is not None
    _submit(strict_episode, strict_check, strict_check["expected_value"], [*strict_check["evidence_alternatives"][0], strict_extra])
    assert strict_episode.verdict["reward"] == 0.0
    assert strict_episode.verdict["reason"] == "evidence_extra"


def test_numeric_value_and_format_are_distinct(tmp_path):
    case = _case(tmp_path)
    episode, check, calculation, _ = _prepared(case)
    _submit(episode, check, "999.00", [calculation])
    assert episode.verdict["reason"] == "numeric_value"

    episode, check, calculation, _ = _prepared(case)
    expected = check["expected_value"]
    one_decimal = expected.split(".")[0] if "." not in expected else expected[:-1]
    _submit(episode, check, one_decimal, [calculation])
    assert episode.verdict["reason"] == "numeric_format"


def test_periods_are_order_insensitive_but_not_duplicated(tmp_path):
    case = _case(tmp_path)
    episode, check, calculation, _ = _prepared(case)
    _submit(episode, check, check["expected_value"], [calculation], periods=list(reversed(check["periods"])))
    assert episode.verdict["reward"] == 1.0

    episode, check, calculation, _ = _prepared(case)
    _submit(episode, check, check["expected_value"], [calculation], periods=[check["periods"][0], check["periods"][0]])
    assert episode.verdict["reason"] == "result_periods"


def test_context_and_evidence_reasons(tmp_path):
    case = _case(tmp_path)
    episode, check, calculation, _ = _prepared(case)
    result_periods = check["periods"]
    episode.step(
        json.dumps(
            {
                "name": "submit_answer",
                "arguments": {
                    "answer_status": "answered",
                    "results": [
                        {
                            "metric_id": check["metric_id"],
                            "value": check["expected_value"],
                            "unit": "wrong",
                            "entity_id": check["entity_id"],
                            "scope": check["scope"],
                            "periods": result_periods,
                        }
                    ],
                    "evidence_refs": [calculation],
                    "explanation": "x",
                },
            },
            ensure_ascii=False,
        )
    )
    assert episode.verdict["reason"] == "result_context"

    episode, check, calculation, _ = _prepared(case)
    _submit(episode, check, check["expected_value"], ["cell-not-observed"])
    assert episode.verdict["reason"] == "unobserved_evidence"

    # A cell that is observed but does not support the result.
    episode, check, calculation, _ = _prepared(case)
    document = case.fixture["documents"][0]
    for table in document["tables"]:
        observation = episode.step(json.dumps({"name": "read_table", "arguments": {"table_id": table["table_id"]}}, ensure_ascii=False))
        cells = [cell["cell_id"] for cell in observation["data"]["cells"] if cell["cell_id"] not in check["evidence_alternatives"][0]]
        if observation["ok"] and cells:
            unrelated = cells[0]
            break
    else:
        pytest.skip("no unrelated observed cell")
    _submit(episode, check, check["expected_value"], [unrelated])
    assert episode.verdict["reason"] == "evidence_missing"


def test_forbidden_action_reward(tmp_path):
    case = _case(tmp_path)
    case.task["allowed_tools"] = [name for name in case.task["allowed_tools"] if name != "calculate"]
    episode = ResearchEpisode(case)
    episode.step('{"name":"calculate","arguments":{"operation":"sum","operands":["a","b"]}}')
    assert episode.verdict == {
        "reward": -1.0,
        "train_eligible": True,
        "reason": "forbidden_action",
        "strict_support": False,
    }
