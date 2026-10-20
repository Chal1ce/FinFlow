"""Prepare locally, then run Agent SFT -> GRPO without a supplied checkpoint."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from agent.datasets.governed import build_governed_dataset
from agent.datasets.research import build_research_dataset
from agent.datasets.research_sft import export_research_sft
from agent.environment.cases import AGENT_ROOT, digest, scoped_path, write_json
from agent.trainers.rl.runner import _snapshot, run_rl
from agent.trainers.sft.runner import run_sft


def _checkpoint(run, *, expected_step=None):
    """Only select this run's numerical checkpoints with matching trainer state."""
    root = (run / "checkpoints").resolve()
    candidates = []
    for path in root.glob("checkpoint-*"):
        match = re.fullmatch(r"checkpoint-([0-9]+)", path.name)
        if not match or not path.is_dir() or path.resolve().parent != root:
            continue
        state, config = path / "trainer_state.json", path / "adapter_config.json"
        weights = [path / "adapter_model.safetensors", path / "adapter_model.bin"]
        if not state.is_file() or not config.is_file() or not any(weight.is_file() for weight in weights):
            continue
        try:
            step = json.loads(state.read_text(encoding="utf-8"))["global_step"]
            adapter = json.loads(config.read_text(encoding="utf-8"))
        except (ValueError, KeyError):
            continue
        if type(step) is not int or step != int(match[1]) or adapter.get("peft_type") != "LORA":
            continue
        if expected_step is None or step == expected_step:
            candidates.append((step, path.resolve()))
    return max(candidates, default=(None, None), key=lambda item: item[0])[1]


def _input_bindings(root):
    return {
        path.relative_to(root).as_posix(): digest(path)
        for name in ("tasks", "references", "recipes")
        for path in sorted((root / name).rglob("*"))
        if path.is_file()
    }


def _prepare(output, *, dataset, sft_dataset, model, sft_steps, rl_steps, train_cases, validation_cases, seed, direct_rl):
    if dataset and sft_dataset:
        raise ValueError("choose an RL task dataset or a governed SFT dataset")
    if (dataset or sft_dataset) and any(value is not None for value in (train_cases, validation_cases, seed)):
        raise ValueError("case counts and seed apply only to the synthetic fallback")
    for source in (dataset, sft_dataset):
        if source:
            source = Path(source).resolve()
            if source == output or source in output.parents or output in source.parents:
                raise ValueError("experiment output must be outside its input dataset")
    sft_recipe = json.loads((AGENT_ROOT / "trainers/rl/agent-sft-qwen3.5-0.8b.json").read_text(encoding="utf-8"))
    rl_recipe = json.loads((AGENT_ROOT / "trainers/rl/qwen3.5-0.8b.json").read_text(encoding="utf-8"))
    model = model or rl_recipe["model"]
    sft_steps = sft_steps if sft_steps is not None else sft_recipe["arguments"]["max_steps"]
    rl_steps = rl_steps if rl_steps is not None else rl_recipe["arguments"]["max_steps"]
    if any(type(steps) is not int or steps < 1 for steps in (sft_steps, rl_steps)):
        raise ValueError("SFT/RL steps must be positive integers")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".research-start-", dir=output.parent) as temporary:
        staging = Path(temporary) / "experiment"
        (staging / "recipes").mkdir(parents=True)
        write_json(staging / "recipes/sft.json", {**sft_recipe, "model": model})
        write_json(staging / "recipes/rl.json", {**rl_recipe, "model": model})
        if dataset:
            _snapshot(Path(dataset).resolve(), staging / "tasks")
            source_kind = "frozen_research_tasks"
        elif sft_dataset:
            build_governed_dataset(sft_dataset, staging / "tasks")
            source_kind = "accepted_financial_tables"
        else:
            build_research_dataset(
                staging / "tasks",
                train_cases=128 if train_cases is None else train_cases,
                validation_cases=32 if validation_cases is None else validation_cases,
                seed=42 if seed is None else seed,
            )
            source_kind = "synthetic_integration_tasks"
        if not direct_rl:
            export_research_sft(staging / "tasks", staging / "references", variants=2)
        record = {
            "format": "finflow-research-pipeline-v1",
            "status": "prepared",
            "model": model,
            "source_kind": source_kind,
            "sft_steps": sft_steps,
            "rl_steps": rl_steps,
            "direct_rl": direct_rl,
            "inputs": _input_bindings(staging),
            "stages": {
                "sft": {"status": "skipped" if direct_rl else "pending", "attempts": []},
                "rl": {"status": "pending", "attempts": []},
            },
            "train_command": ["python", "-m", "agent.cli", "rl-start", "--output", str(output), "--train"],
            "preparation": "local task construction and optional deterministic reference tools; no model download or GPU training",
        }
        write_json(staging / "pipeline.json", record)
        staging.rename(output)
    return record


def _stage(output, record, name, launch):
    stage = record["stages"][name]
    if stage["status"] == "completed":
        return True
    previous = stage["attempts"][-1] if stage["attempts"] else None
    run = scoped_path(output, previous["path"]) if previous else None
    resume = _checkpoint(run) if run else None
    if resume and (run / "finflow-run.json").is_file():
        attempt = previous
    else:
        # Keep a failed attempt's logs even when no checkpoint was written yet.
        relative = f"runs/{name}/attempt-{len(stage['attempts']) + 1:03d}"
        run, resume = output / relative, None
        attempt = {"path": relative, "status": "pending"}
        stage["attempts"].append(attempt)
    stage["status"] = attempt["status"] = "running"
    record["status"] = "running"
    write_json(output / "pipeline.json", record)
    try:
        result = launch(run, resume)
    except BaseException as exc:
        status = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
        stage["status"] = attempt["status"] = record["status"] = status
        attempt["error_type"] = type(exc).__name__
        write_json(output / "pipeline.json", record)
        raise
    stage["status"] = attempt["status"] = result["status"]
    record["status"] = "running" if result["status"] == "completed" else "failed"
    write_json(output / "pipeline.json", record)
    return result["status"] == "completed"


def start_research(
    output,
    *,
    dataset=None,
    sft_dataset=None,
    model=None,
    sft_steps=None,
    rl_steps=None,
    train_cases=None,
    validation_cases=None,
    seed=None,
    direct_rl=False,
    train=False,
):
    output = Path(output).resolve()
    if not output.exists():
        record = _prepare(
            output,
            dataset=dataset,
            sft_dataset=sft_dataset,
            model=model,
            sft_steps=sft_steps,
            rl_steps=rl_steps,
            train_cases=train_cases,
            validation_cases=validation_cases,
            seed=seed,
            direct_rl=direct_rl,
        )
    else:
        path = output / "pipeline.json"
        if not path.is_file():
            raise ValueError("output exists without a prepared pipeline; choose a new output")
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("format") != "finflow-research-pipeline-v1":
            raise ValueError("unsupported pipeline bundle")
        if any(value is not None for value in (dataset, sft_dataset, model, sft_steps, rl_steps, train_cases, validation_cases, seed)):
            raise ValueError("reuse a prepared bundle with --output and --train only; use a new output to change its inputs")
        if direct_rl and not record["direct_rl"]:
            raise ValueError("a prepared SFT -> RL bundle cannot switch to direct RL")
    record["train_command"] = ["python", "-m", "agent.cli", "rl-start", "--output", str(output), "--train"]
    if _input_bindings(output) != record["inputs"]:
        raise ValueError("prepared pipeline inputs changed; prepare a new bundle for edited inputs")
    if not train or record["status"] == "completed":
        return {**record, "output": str(output)}
    adapter = None
    if not record["direct_rl"]:
        if not _stage(
            output,
            record,
            "sft",
            lambda run, resume: run_sft(
                output / "references",
                run,
                config=output / "recipes/sft.json",
                model=record["model"],
                max_steps=record["sft_steps"],
                modality="text",
                resume_from_checkpoint=resume,
            ),
        ):
            return {**record, "output": str(output)}
        run = scoped_path(output, record["stages"]["sft"]["attempts"][-1]["path"])
        adapter = _checkpoint(run, expected_step=record["sft_steps"])
        if not adapter:
            record["status"] = "failed"
            record["stages"]["sft"]["status"] = "failed"
            attempt = record["stages"]["sft"]["attempts"][-1]
            attempt.update(status="failed", error_type="MissingFinalSFTCheckpoint")
            write_json(output / "pipeline.json", record)
            raise ValueError("SFT process completed without its final LoRA checkpoint; inspect its finflow-run.json and trainer output")
        record["sft_adapter"] = adapter.relative_to(output).as_posix()
    if not _stage(
        output,
        record,
        "rl",
        lambda run, resume: run_rl(
            output / "tasks",
            run,
            config=output / "recipes/rl.json",
            model=record["model"],
            max_steps=record["rl_steps"],
            sft_adapter=None if resume else adapter,
            resume_from_checkpoint=resume,
        ),
    ):
        return {**record, "output": str(output)}
    # A zero exit code is not proof that the trainer wrote a usable policy checkpoint.
    stage = record["stages"]["rl"]
    attempt = stage["attempts"][-1]
    run = scoped_path(output, attempt["path"])
    final_adapter = _checkpoint(run, expected_step=record["rl_steps"])
    if not final_adapter:
        stage["status"] = attempt["status"] = record["status"] = "failed"
        attempt["error_type"] = "MissingFinalRLCheckpoint"
        write_json(output / "pipeline.json", record)
        raise ValueError("RL process completed without its final LoRA checkpoint; inspect trainer.log and finflow-run.json")
    record["rl_adapter"] = final_adapter.relative_to(output).as_posix()
    record["status"] = "completed"
    write_json(output / "pipeline.json", record)
    return {**record, "output": str(output)}
