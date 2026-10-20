import json

import pytest

from agent.datasets.files import load_cases
from agent.datasets.research import build_research_dataset
from agent.datasets.research_sft import reference_actions
from agent.environment.research import ResearchEpisode, parse_action, parse_action_with_repair


def _dataset(tmp_path):
    build_research_dataset(tmp_path / "tasks", train_cases=3, validation_cases=1, test_cases=1)
    return tmp_path / "tasks"


def _case(tmp_path, family="revenue_growth"):
    for _, _, case in load_cases(_dataset(tmp_path)):
        if case.manifest["grouping"]["template_family"] == family:
            return case
    raise AssertionError("family not found")


def _execute(case, actions, *, strict_evidence=False):
    episode = ResearchEpisode(case, strict_evidence=strict_evidence)
    for action in actions:
        episode.step(action)
    return episode


def test_dataset_has_test_split_and_difficulty(tmp_path):
    dataset = _dataset(tmp_path)
    rows = list(load_cases(dataset))
    assert {split for split, _, _ in rows} == {"train", "validation", "test"}
    assert all(case.manifest["grouping"]["difficulty"] in {"easy", "medium", "hard"} for _, _, case in rows)
    assert len(rows) == 5


def test_reference_actions_pass_and_are_deterministic(tmp_path):
    case = _case(tmp_path)
    first = reference_actions(case)
    second = reference_actions(case)
    assert first == second
    episode = _execute(case, first)
    assert episode.reward == 1.0
    assert episode.verdict["strict_support"] is True


def test_unknown_tool_is_recoverable(tmp_path):
    case = _case(tmp_path)
    episode = ResearchEpisode(case)
    observation = episode.step('{"name":"search_document","arguments":{"query":"x"}}')
    assert observation["code"] == "unknown_tool"
    assert episode.status == "running"
    assert len(episode.actions) == 1


def test_known_but_disallowed_tool_is_forbidden(tmp_path):
    case = _case(tmp_path)
    case.task["allowed_tools"] = [name for name in case.task["allowed_tools"] if name != "calculate"]
    episode = ResearchEpisode(case)
    observation = episode.step('{"name":"calculate","arguments":{"operation":"sum","operands":["a","b"]}}')
    assert observation["code"] == "forbidden_action"
    assert episode.status == "forbidden_action"
    assert episode.reward == -1.0


def test_parser_repairs_common_noise():
    action, repairs = parse_action_with_repair('```json {"tool":"search_documents","args":{"query":"x"}} ```')
    assert action == {"name": "search_documents", "arguments": {"query": "x"}}
    assert {"fence", "fence_end", "tool_alias", "args_alias"} <= set(repairs)


def test_parser_rejects_extra_fields_and_trailing_text():
    with pytest.raises(ValueError):
        parse_action('{"name":"search_documents","arguments":{"query":"x"},"extra":1}')
    with pytest.raises(ValueError):
        parse_action('{"name":"search_documents","arguments":{"query":"x"}} trailing')
    with pytest.raises(ValueError):
        parse_action("not json")


def test_read_table_period_filter(tmp_path):
    case = _case(tmp_path)
    episode = ResearchEpisode(case)
    document = case.fixture["documents"][0]
    episode.step(
        json.dumps(
            {
                "name": "search_documents",
                "arguments": {"query": document["report_title"], "entity_id": document["entity_id"], "period": document["report_period"]},
            },
            ensure_ascii=False,
        )
    )
    table = document["tables"][0]
    period = str(case.oracle["checks"][0]["periods"][-1])
    observation = episode.step(
        json.dumps(
            {"name": "read_table", "arguments": {"table_id": table["table_id"], "period": period}},
            ensure_ascii=False,
        )
    )
    assert observation["ok"] is True
    assert {cell["period"] for cell in observation["data"]["cells"]} == {str(case.oracle["checks"][0]["periods"][-1])}


def test_generation_limit_and_step_limit_are_terminal(tmp_path):
    case = _case(tmp_path)
    episode = ResearchEpisode(case)
    observation = episode.step("partial", sampled_tokens=600, finish_reason="length")
    assert observation["code"] == "generation_limit"
    assert episode.status == "generation_limit"
    assert episode.reward == 0.0

    episode = ResearchEpisode(case)
    for _ in range(case.task["budgets"]["max_actions"]):
        episode.step("{}")
    assert episode.status == "step_limit"
    assert episode.reward == 0.0


def test_observation_is_delta_and_compact(tmp_path):
    case = _case(tmp_path)
    actions = reference_actions(case)
    episode = ResearchEpisode(case)
    for action in actions:
        observation = episode.step(action)
        assert observation["observation_id"].startswith("obs-")
        assert len(json.dumps(observation)) < 6000
    assert episode.submission is not None


def test_dual_metric_reference_passes(tmp_path):
    build_research_dataset(tmp_path / "dual", train_cases=4, validation_cases=0)
    case = next(case for _, _, case in load_cases(tmp_path / "dual") if case.manifest["grouping"]["template_family"] == "dual_metric")
    checks = [check for check in case.oracle["checks"] if check["kind"] == "research_result"]
    assert {check["metric_id"] for check in checks} == {"revenue_growth", "operating_cash_flow_share"}
    episode = ResearchEpisode(case)
    for action in reference_actions(case):
        episode.step(action)
    assert episode.reward == 1.0
    assert episode.verdict["strict_support"] is True
    assert len(episode.submission["results"]) == 2


def test_cursor_is_deterministic_for_replay(tmp_path):
    from copy import deepcopy
    from dataclasses import replace

    case = _case(tmp_path)
    fixture = deepcopy(case.fixture)
    document = fixture["documents"][0]
    for group in range(2):
        for index, table in enumerate(list(document["tables"])):
            clone = deepcopy(table)
            clone["table_id"] = f"table-extra-{group}-{index}"
            for cell in clone["cells"]:
                cell["cell_id"] = f"cell-extra-{group}-{index}-{cell['cell_id'].rsplit('-', 1)[-1]}"
            document["tables"].append(clone)
    extended = replace(case, fixture=fixture)
    query = {
        "query": document["report_title"],
        "entity_id": document["entity_id"],
        "period": document["report_period"],
    }
    first = ResearchEpisode(extended)
    page = first.step(json.dumps({"name": "search_documents", "arguments": query}, ensure_ascii=False))
    assert page["next_cursor"]
    second = ResearchEpisode(extended)
    page2 = second.step(json.dumps({"name": "search_documents", "arguments": query}, ensure_ascii=False))
    assert page2["next_cursor"] == page["next_cursor"]
    next_page = second.step(
        json.dumps({"name": "search_documents", "arguments": {**query, "cursor": page["next_cursor"]}}, ensure_ascii=False)
    )
    assert next_page["ok"] is True
