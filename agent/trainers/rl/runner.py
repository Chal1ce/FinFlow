"""Snapshot executable research cases and launch one-GPU ms-swift GRPO."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from agent.environment.cases import AGENT_ROOT, ENVIRONMENT_VERSION, REWARD_VERSION, ResearchCase, digest, scoped_path, write_json

DEFAULT_CONFIG = Path(__file__).with_name("qwen3.5-0.8b.json")


def _snapshot(dataset, inputs):
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != "finflow-research-dataset-v1":
        raise ValueError("RL dataset must contain executable case manifests; SFT answers are not RL prompts")
    counts, budgets, groups = {}, [], {}
    inputs.mkdir(parents=True, exist_ok=True)
    for split in ("train", "validation"):
        rows = []
        for binding in manifest["splits"].get(split, []):
            case_path = scoped_path(dataset, binding["path"])
            if digest(case_path) != binding["sha256"]:
                raise ValueError("dataset case manifest hash mismatch")
            case = ResearchCase.load(dataset, binding["path"])
            if case.manifest["split"] != split:
                raise ValueError("case split differs from dataset manifest")
            if case.manifest["versions"]["environment"] != ENVIRONMENT_VERSION or case.manifest["versions"]["reward"] != REWARD_VERSION:
                raise ValueError("case environment/reward version is not supported by this trainer")
            for key in ("source_group", "base_snapshot_group", "variant_family"):
                group = (key, case.manifest["grouping"][key])
                if group in groups and groups[group] != split:
                    raise ValueError("source/snapshot/variant group occurs in both training and validation")
                groups[group] = split
            for source in [binding, *case.manifest["files"].values()]:
                target = scoped_path(inputs, source["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(scoped_path(dataset, source["path"]), target)
            # Private task, fixture and oracle paths stay in env_config, outside model messages.
            rows.append({"messages": [{"role": "user", "content": case.task["instruction"]}], "env_config": {"case_path": binding["path"]}})
            budgets.append(case.task["budgets"])
        with (inputs / f"{split}.jsonl").open("w", encoding="utf-8") as output:
            for row in rows:
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
        counts[split] = len(rows)
    if not counts["train"]:
        raise ValueError("RL dataset has no training cases")
    shutil.copyfile(dataset / "manifest.json", inputs / "manifest.json")
    if any(budget != budgets[0] for budget in budgets):
        raise ValueError("this single-GPU recipe needs uniform task budgets; split tasks by budget first")
    return counts, budgets[0]


def _packages():
    result = {}
    for name in ("ms-swift", "transformers", "torch", "vllm", "peft", "qwen-vl-utils"):
        try:
            package = distribution(name)
            direct = json.loads(package.read_text("direct_url.json") or "{}")
            result[name] = {"version": package.version, "revision": direct.get("vcs_info", {}).get("commit_id")}
        except PackageNotFoundError:
            result[name] = None
    return result


def _adapter_hashes(path):
    if not path:
        return {}
    files = sorted(Path(path).glob("adapter_*"))
    if not any(item.suffix in {".safetensors", ".bin"} for item in files):
        raise ValueError("SFT adapter weights are missing")
    return {item.name: digest(item) for item in files if item.is_file()}


def run_rl(
    dataset, output, *, config=DEFAULT_CONFIG, model=None, max_steps=None, sft_adapter=None, resume_from_checkpoint=None, prepare_only=False
):
    dataset, output = Path(dataset).resolve(), Path(output).resolve()
    if not dataset.is_dir():
        raise ValueError("RL dataset directory does not exist; first use rl-build")
    if output == dataset or dataset in output.parents or output in dataset.parents:
        raise ValueError("training output and source RL dataset must not contain each other")
    if max_steps is not None and (type(max_steps) is not int or max_steps < 1):
        raise ValueError("max-steps must be a positive integer")
    recipe = json.loads(Path(config).read_text(encoding="utf-8"))
    parameters = dict(recipe["arguments"])
    if max_steps is not None:
        parameters["max_steps"] = max_steps
    model = model or recipe["model"]
    packages = _packages()
    swift = shutil.which("swift")
    if not prepare_only and (not swift or any(packages[key] is None for key in ("ms-swift", "vllm", "torch", "transformers", "peft"))):
        raise ValueError("install agent/trainers/rl/requirements.txt in the GPU environment first")
    if not parameters.get("use_vllm") or parameters.get("vllm_mode") != "colocate":
        raise ValueError("this first RL adapter requires single-GPU colocate vLLM for native rollout capture")
    if parameters.get("loss_scale") != "default":
        raise ValueError("multi-turn policy loss must use all assistant responses (loss_scale=default)")
    for checkpoint in (resume_from_checkpoint, sft_adapter):
        if checkpoint and not Path(checkpoint).resolve().is_dir():
            raise ValueError("checkpoint/adapter directory does not exist")
    if resume_from_checkpoint and sft_adapter:
        raise ValueError("choose either a fresh SFT adapter initialization or an RL checkpoint resume")
    dataset_hash = digest(dataset / "manifest.json")
    run_path = output / "finflow-run.json"
    reference_adapter = str(Path(sft_adapter).resolve()) if sft_adapter else None
    previous = None
    if resume_from_checkpoint and not run_path.is_file():
        raise ValueError("resume in the original RL output directory to retain its reference policy and run bindings")
    if output.exists() and any(output.iterdir()):
        if not resume_from_checkpoint or not run_path.is_file():
            raise ValueError("training output is not empty; use a new output or resume its RL checkpoint")
        previous = json.loads(run_path.read_text(encoding="utf-8"))
        if previous["dataset_sha256"] != dataset_hash or previous["model"] != model or previous["recipe"] != recipe:
            raise ValueError("resume must keep the original dataset, model and recipe")
        reference_adapter = previous.get("reference_adapter")
    reference_hashes = _adapter_hashes(reference_adapter)
    if previous and previous.get("reference_adapter_sha256", {}) != reference_hashes:
        raise ValueError("SFT reference adapter changed since the original RL run")
    output.mkdir(parents=True, exist_ok=True)
    inputs = output / "finflow-inputs"
    counts, budgets = _snapshot(dataset, inputs)
    if (
        parameters["max_length"] > budgets["max_context_tokens"]
        or parameters["max_completion_length"] > budgets["max_output_tokens_per_turn"]
    ):
        raise ValueError("trainer lengths exceed task budgets; use a matching recipe")
    command = [
        swift or "swift",
        "rlhf",
        "--model",
        model,
        "--dataset",
        str(inputs / "train.jsonl"),
        "--output_dir",
        str(output / "checkpoints"),
        "--external_plugins",
        str(Path(__file__).with_name("swift_plugin.py")),
        "--callbacks",
        "finflow_rl_context",
        "--multi_turn_scheduler",
        "finflow_research",
        "--use_gym_env",
        "true",
        "--max_turns",
        str(budgets["max_actions"]),
    ]
    if counts["validation"]:
        command += ["--val_dataset", str(inputs / "validation.jsonl"), "--eval_strategy", "steps", "--eval_steps", "10"]
    else:
        command += ["--eval_strategy", "no"]
    if sft_adapter:
        command += ["--adapters", str(Path(sft_adapter).resolve())]
    if reference_adapter:
        command += ["--ref_adapters", reference_adapter]
    if resume_from_checkpoint:
        command += ["--resume_from_checkpoint", str(Path(resume_from_checkpoint).resolve())]
    for key, value in parameters.items():
        command.append("--" + key)
        command.extend(
            [str(item).lower() if isinstance(item, bool) else str(item) for item in (value if isinstance(value, list) else [value])]
        )
    environment = os.environ.copy()
    environment.setdefault("CUDA_VISIBLE_DEVICES", "0")
    environment["NPROC_PER_NODE"] = "1"
    environment["FINFLOW_RL_DATASET"] = str(inputs)
    environment["FINFLOW_RL_EPISODES"] = str(output / "episodes")
    # Each launch gets an immutable record so resumed episodes bind to the correct run attempt.
    attempt = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    attempt_path = output / "attempts" / (attempt + ".json")
    environment["FINFLOW_RL_RUN_MANIFEST"] = str(attempt_path)
    environment["PYTHONPATH"] = str(AGENT_ROOT.parent) + os.pathsep + environment.get("PYTHONPATH", "")
    record = {
        "format": "finflow-rl-run-v1",
        "status": "prepared" if prepare_only else "running",
        "attempt": attempt,
        "model": model,
        "reference_adapter": reference_adapter,
        "reference_adapter_sha256": reference_hashes,
        "dataset": str(dataset),
        "dataset_sha256": dataset_hash,
        "rows": counts,
        "recipe": recipe,
        "resolved_arguments": parameters,
        "command": command,
        "packages": packages,
        "source_sha256": {str(path.relative_to(AGENT_ROOT)): digest(path) for path in sorted(AGENT_ROOT.rglob("*.py"))},
        "environment": ENVIRONMENT_VERSION,
        "reward": REWARD_VERSION,
        "capture": "native IDs/logprobs and assistant masks from ms-swift/vLLM; no re-tokenized rollout claims",
    }
    write_json(attempt_path, record)
    write_json(run_path, record)
    if prepare_only:
        return record
    try:
        process = subprocess.run(command, env=environment, check=False)
    except BaseException:
        record["status"] = "interrupted"
        write_json(run_path, record)
        raise
    record.update(status="completed" if process.returncode == 0 else "failed", returncode=process.returncode)
    write_json(run_path, record)
    return record
