import asyncio
import importlib
import json
import sys
import types



class _TrainerCallback:
    def __init__(self, args, trainer):
        self.args = args
        self.trainer = trainer
        self.calls = []


class _MultiTurnScheduler:
    def __init__(self, *args, **kwargs):
        self.max_turns = kwargs.get("max_turns")
        self._template = kwargs.get("template")
        self._tokenizer = kwargs.get("tokenizer")

    def get_response_token_data(self, infer_request, response_choice, **kwargs):
        return list(response_choice.token_ids), [1] * len(response_choice.token_ids)


def _install_stubs(monkeypatch):
    swift = types.ModuleType("swift")
    swift.__path__ = []
    callbacks = types.ModuleType("swift.callbacks")
    callbacks.TrainerCallback = _TrainerCallback
    callbacks.callbacks_map = {}
    rollout = types.ModuleType("swift.rollout")
    rollout.__path__ = []
    multi = types.ModuleType("swift.rollout.multi_turn")
    multi.MultiTurnScheduler = _MultiTurnScheduler
    multi.multi_turns = {}
    monkeypatch.setitem(sys.modules, "swift", swift)
    monkeypatch.setitem(sys.modules, "swift.callbacks", callbacks)
    monkeypatch.setitem(sys.modules, "swift.rollout", rollout)
    monkeypatch.setitem(sys.modules, "swift.rollout.multi_turn", multi)
    return importlib.import_module("agent.trainers.rl.swift_plugin")


def _dataset(tmp_path):
    from agent.datasets.research import build_research_dataset

    build_research_dataset(tmp_path / "tasks", train_cases=1, validation_cases=0)
    return tmp_path / "tasks"


def _request(dataset):
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    return types.SimpleNamespace(
        uuid="request-1",
        data_dict={"env_config": {"case_path": manifest["splits"]["train"][0]["path"]}},
        messages=[{"role": "user", "content": "placeholder"}],
    )


def _choice(text, tokens=3):
    return types.SimpleNamespace(
        token_ids=[1] * tokens,
        logprobs={"content": [{"logprob": -0.1}] * tokens},
        finish_reason="stop",
        message=types.SimpleNamespace(content=text),
    )


def _trainer():
    class State:
        global_step = 7

    class Model:
        training = True
        config = types.SimpleNamespace(_commit_hash="rev")

    class Processing:
        tokenizer = types.SimpleNamespace(name_or_path="tok")

    class Trainer:
        state = State()
        model = Model()
        processing_class = Processing()
        args = types.SimpleNamespace(temperature=0.9, top_p=0.95)

    return Trainer()


def _scheduler(plugin):
    template = types.SimpleNamespace(tokenizer=types.SimpleNamespace(name_or_path="tok"))
    return plugin.FinancialResearchScheduler(max_turns=8, tokenizer=template.tokenizer, template=template)


def _env(tmp_path, monkeypatch):
    dataset = _dataset(tmp_path)
    monkeypatch.setenv("FINFLOW_RL_DATASET", str(dataset))
    monkeypatch.setenv("FINFLOW_RL_EPISODES", str(tmp_path / "episodes"))
    monkeypatch.setenv("FINFLOW_RL_RUN_MANIFEST", str(tmp_path / "attempts" / "a.json"))
    return dataset


def test_reference_rollout_terminates_with_reward(tmp_path, monkeypatch):
    plugin = _install_stubs(monkeypatch)
    dataset = _env(tmp_path, monkeypatch)
    plugin.RunContextCallback(None, _trainer())
    request = _request(dataset)
    scheduler = _scheduler(plugin)
    asyncio.run(scheduler.on_trajectory_start([request]))
    assert request.messages[0]["role"] == "system"
    assert request.messages[1]["role"] == "user"

    from agent.datasets.files import load_cases
    from agent.datasets.research_sft import reference_actions

    case = next(case for _, _, case in load_cases(dataset))
    actions = reference_actions(case)
    result = None
    for turn, action in enumerate(actions, start=1):
        request.messages.append({"role": "assistant", "content": action})
        result = asyncio.run(scheduler.on_turn_end(request, _choice(action), turn))
        if result["done"]:
            break
        scheduler.step(request, _choice(action), turn)
    assert result["done"] is True
    assert result["rollout_infos"]["total_reward"] == 1.0
    assert result["rollout_infos"]["terminal_status"] == "submitted"
    episode_path = next((tmp_path / "episodes").glob("*/episode.json"))
    episode = json.loads(episode_path.read_text(encoding="utf-8"))
    assert episode["policy"]["request_id"] == "request-1"
    assert episode["policy"]["attempt"] == "a"
    assert episode["policy"]["optimizer_step"] == 7
    assert episode["verdict"]["reward"] == 1.0
    tokens = list((episode_path.parent / "tokens.jsonl").read_text(encoding="utf-8").splitlines())
    assert len(tokens) == len(episode["actions"])


def test_unknown_tool_keeps_episode_running(tmp_path, monkeypatch):
    plugin = _install_stubs(monkeypatch)
    dataset = _env(tmp_path, monkeypatch)
    plugin.RunContextCallback(None, _trainer())
    request = _request(dataset)
    scheduler = _scheduler(plugin)
    asyncio.run(scheduler.on_trajectory_start([request]))
    request.messages.append({"role": "assistant", "content": '{"name":"search_document","arguments":{"query":"x"}}'})
    result = asyncio.run(scheduler.on_turn_end(request, _choice("unknown"), 1))
    assert result["done"] is False
    assert result["rollout_infos"]["total_reward"] == 0.0
    assert request.uuid in scheduler.pending
    scheduler.step(request, _choice("unknown"), 1)
    assert request.messages[-1]["role"] == "user"
    assert json.loads(request.messages[-1]["content"])["code"] == "unknown_tool"
