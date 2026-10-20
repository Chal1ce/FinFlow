import json
import sys

import pytest

from agent.datasets.research import build_research_dataset
from agent.trainers.rl.runner import _run_logged, run_rl


def _dataset(tmp_path):
    build_research_dataset(tmp_path / "tasks", train_cases=4, validation_cases=1, test_cases=1)
    return tmp_path / "tasks"


def _adapter(tmp_path, base="Qwen/Qwen3.5-0.8B"):
    path = tmp_path / "adapter"
    path.mkdir(parents=True)
    (path / "adapter_config.json").write_text(
        json.dumps({"peft_type": "LORA", "base_model_name_or_path": base}), encoding="utf-8"
    )
    (path / "adapter_model.safetensors").write_bytes(b"weights")
    return path


def test_prepare_only_snapshots_train_validation_test(tmp_path):
    dataset = _dataset(tmp_path)
    record = run_rl(dataset, tmp_path / "run", prepare_only=True, max_steps=3)
    assert record["status"] == "prepared"
    assert record["rows"] == {"train": 4, "validation": 1, "test": 1}
    assert (tmp_path / "run" / "finflow-inputs" / "test.jsonl").is_file()
    assert not (tmp_path / "run" / "finflow-inputs.pending").exists()
    command = record["command"]
    assert command[command.index("--max_steps") + 1] == "3"
    assert command[command.index("--generation_batch_size") + 1] == "16"
    assert command[command.index("--gradient_accumulation_steps") + 1] == "16"
    assert command[command.index("--dynamic_sample") + 1] == "true"
    assert command.count("--eval_steps") == 1
    ranks = {"easy": 0, "medium": 1, "hard": 2, "unknown": 3}
    difficulties = [
        json.loads(line)["difficulty"]
        for line in (tmp_path / "run" / "finflow-inputs" / "train.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [ranks[item] for item in difficulties] == sorted(ranks[item] for item in difficulties)


def test_prepared_output_can_be_started_again(tmp_path):
    dataset = _dataset(tmp_path)
    run_rl(dataset, tmp_path / "run", prepare_only=True, max_steps=3)
    record = run_rl(dataset, tmp_path / "run", prepare_only=True, max_steps=3)
    assert record["status"] == "prepared"
    assert len(list((tmp_path / "run" / "attempts").glob("*.json"))) == 2


def test_adapter_base_and_command(tmp_path):
    dataset = _dataset(tmp_path)
    adapter = _adapter(tmp_path)
    record = run_rl(dataset, tmp_path / "run", prepare_only=True, sft_adapter=adapter)
    command = record["command"]
    assert command[command.index("--adapters") + 1] == str(adapter.resolve())
    assert command[command.index("--ref_adapters") + 1] == str(adapter.resolve())
    assert record["reference_adapter_metadata"]["base_model_name_or_path"] == "Qwen/Qwen3.5-0.8B"

    # A prepared run must keep loading the recorded SFT adapter even when the
    # caller does not repeat --sft-adapter.
    replay = run_rl(dataset, tmp_path / "run", prepare_only=True)
    assert replay["command"][replay["command"].index("--adapters") + 1] == str(adapter.resolve())

    wrong = _adapter(tmp_path / "wrong", base="other/model")
    with pytest.raises(ValueError, match="base model"):
        run_rl(dataset, tmp_path / "run-wrong", prepare_only=True, sft_adapter=wrong)


def test_logged_command_writes_and_streams(tmp_path, capsys):
    log = tmp_path / "trainer.log"
    code = f"print('hello {tmp_path.name}')"
    returncode = _run_logged([sys.executable, "-c", code], None, log)
    assert returncode == 0
    assert "hello" in log.read_text(encoding="utf-8")
    assert "hello" in capsys.readouterr().out
