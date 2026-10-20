"""Snapshot executable research cases and launch one-GPU ms-swift GRPO."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from agent.datasets.files import load_cases
from agent.environment.cases import AGENT_ROOT, ENVIRONMENT_VERSION, REWARD_VERSION, digest, scoped_path, write_json

DEFAULT_CONFIG = Path(__file__).with_name("qwen3.5-0.8b.json")
PACKAGE_NAMES = ("ms-swift", "transformers", "torch", "vllm", "peft", "qwen-vl-utils")
REQUIRED_PACKAGES = ("ms-swift", "vllm", "torch", "transformers", "peft")


def _snapshot(dataset, inputs):
    counts, budgets = {}, []
    split_rows = {"train": [], "validation": [], "test": []}
    inputs.mkdir(parents=True, exist_ok=True)
    for split, binding, case in load_cases(dataset):
        for source in [binding, *case.manifest["files"].values()]:
            target = scoped_path(inputs, source["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(scoped_path(dataset, source["path"]), target)
        # Private task, fixture, provenance and oracle stay outside model messages.
        split_rows[split].append(
            {
                "messages": [{"role": "user", "content": case.task["instruction"]}],
                "env_config": {"case_path": binding["path"]},
                "difficulty": case.manifest["grouping"].get("difficulty", "unknown"),
            }
        )
        budgets.append(case.task["budgets"])
    order = {"easy": 0, "medium": 1, "hard": 2, "unknown": 3}
    split_rows["train"].sort(key=lambda row: order.get(row["difficulty"], 3))
    for split, rows in split_rows.items():
        with (inputs / f"{split}.jsonl").open("w", encoding="utf-8") as output:
            for row in rows:
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
        counts[split] = len(rows)
    if not counts["train"]:
        raise ValueError("RL dataset has no training cases")
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("excluded_sha256"):
        if digest(dataset / "excluded.jsonl") != manifest["excluded_sha256"]:
            raise ValueError("dataset exclusion report hash mismatch")
        shutil.copyfile(dataset / "excluded.jsonl", inputs / "excluded.jsonl")
    shutil.copyfile(dataset / "manifest.json", inputs / "manifest.json")
    if any(budget != budgets[0] for budget in budgets):
        raise ValueError("this single-GPU recipe needs uniform task budgets; split tasks by budget first")
    return counts, budgets[0]


def _snapshot_atomic(dataset, inputs):
    """Build the immutable input snapshot before exposing it in the run directory."""
    staging = inputs.parent / (inputs.name + ".pending")
    if staging.exists():
        shutil.rmtree(staging)
    try:
        counts, budgets = _snapshot(dataset, staging)
        if inputs.exists():
            shutil.rmtree(inputs)
        staging.rename(inputs)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return counts, budgets


def _packages():
    result = {}
    for name in PACKAGE_NAMES:
        try:
            package = distribution(name)
            direct = json.loads(package.read_text("direct_url.json") or "{}")
            result[name] = {"version": package.version, "revision": direct.get("vcs_info", {}).get("commit_id")}
        except PackageNotFoundError:
            result[name] = None
    return result


def _model_key(value):
    if not value:
        return ""
    path = Path(value)
    if path.exists():
        return str(path.resolve())
    return str(value).strip().rstrip("/")


def _adapter_metadata(path):
    if not path:
        return {}
    config_path = Path(path) / "adapter_config.json"
    if not config_path.is_file():
        raise ValueError("SFT adapter config is missing")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("peft_type") != "LORA":
        raise ValueError("SFT adapter is not a LoRA adapter")
    base = config.get("base_model_name_or_path")
    return {"peft_type": "LORA", "base_model_name_or_path": base}


def _check_adapter_base(metadata, model):
    base = metadata.get("base_model_name_or_path")
    if base and _model_key(base) != _model_key(model):
        raise ValueError(f"SFT adapter base model {base!r} does not match --model {model!r}")


def _adapter_hashes(path):
    if not path:
        return {}
    files = sorted(Path(path).glob("adapter_*"))
    if not any(item.suffix in {".safetensors", ".bin"} for item in files):
        raise ValueError("SFT adapter weights are missing")
    return {item.name: digest(item) for item in files if item.is_file()}


def _run_logged(command, environment, log_path):
    """Stream the trainer output while persisting it under the run directory."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        try:
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line)
                log.flush()
                sys.stdout.write(line)
                sys.stdout.flush()
            return process.wait()
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise


def _validate_lengths(parameters, budgets):
    if parameters.get("max_length", 0) > budgets["max_context_tokens"]:
        raise ValueError("trainer max_length exceeds the task context budget")
    if parameters.get("max_completion_length", 0) > budgets["max_output_tokens_per_turn"]:
        raise ValueError("trainer max_completion_length exceeds the per-turn output budget")


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
    if not prepare_only and (not swift or any(packages[key] is None for key in REQUIRED_PACKAGES)):
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

    expected_swift = recipe.get("swift_revision")
    installed_swift = (packages.get("ms-swift") or {}).get("revision")
    if not prepare_only and expected_swift and installed_swift and installed_swift != expected_swift:
        raise ValueError(f"ms-swift revision mismatch: expected {expected_swift}, installed {installed_swift}")

    dataset_hash = digest(dataset / "manifest.json")
    run_path = output / "finflow-run.json"
    stale = output / "finflow-inputs.pending"
    if stale.exists():
        shutil.rmtree(stale)
    reference_adapter = str(Path(sft_adapter).resolve()) if sft_adapter else None
    previous = None
    if resume_from_checkpoint and not run_path.is_file():
        raise ValueError("resume in the original RL output directory to retain its reference policy and run bindings")
    if output.exists() and any(output.iterdir()):
        if not run_path.is_file():
            raise ValueError("training output is not empty; use a new output or resume its RL checkpoint")
        previous = json.loads(run_path.read_text(encoding="utf-8"))
        if previous["dataset_sha256"] != dataset_hash or previous["model"] != model or previous["recipe"] != recipe:
            raise ValueError("resume must keep the original dataset, model and recipe")
        reference_adapter = previous.get("reference_adapter")
        if resume_from_checkpoint:
            if previous.get("status") == "prepared":
                raise ValueError("the prepared run has no checkpoint to resume; start it without --resume-from-checkpoint")
        elif previous.get("status") != "prepared":
            raise ValueError("training output is not empty; use a new output or resume its RL checkpoint")
        elif sft_adapter and str(Path(sft_adapter).resolve()) != reference_adapter:
            raise ValueError("prepared run used a different SFT adapter; use its original adapter or a new output")
    reference_metadata = _adapter_metadata(reference_adapter)
    _check_adapter_base(reference_metadata, model)
    reference_hashes = _adapter_hashes(reference_adapter)
    if previous and previous.get("reference_adapter_sha256", {}) != reference_hashes:
        raise ValueError("SFT reference adapter changed since the original RL run")

    output.mkdir(parents=True, exist_ok=True)
    inputs = output / "finflow-inputs"
    counts, budgets = _snapshot_atomic(dataset, inputs)
    _validate_lengths(parameters, budgets)

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
        command += ["--val_dataset", str(inputs / "validation.jsonl"), "--eval_strategy", "steps"]
        if "eval_steps" not in parameters:
            command += ["--eval_steps", "50"]
    else:
        command += ["--eval_strategy", "no"]
    # A resumed RL checkpoint already contains the policy adapter; a fresh or prepared
    # run must load the SFT adapter explicitly, otherwise it would silently start from base.
    if reference_adapter and not resume_from_checkpoint:
        command += ["--adapters", reference_adapter]
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
    started_at = datetime.now(timezone.utc).isoformat()
    record = {
        "format": "finflow-rl-run-v1",
        "status": "prepared" if prepare_only else "running",
        "attempt": attempt,
        "started_at": started_at,
        "ended_at": None,
        "model": model,
        "reference_adapter": reference_adapter,
        "reference_adapter_metadata": reference_metadata,
        "reference_adapter_sha256": reference_hashes,
        "dataset": str(dataset),
        "dataset_sha256": dataset_hash,
        "rows": counts,
        "recipe": recipe,
        "resolved_arguments": parameters,
        "command": command,
        "packages": packages,
        "swift_revision_expected": expected_swift,
        "swift_revision_installed": installed_swift,
        "source_sha256": {str(path.relative_to(AGENT_ROOT)): digest(path) for path in sorted(AGENT_ROOT.rglob("*.py"))},
        "environment": ENVIRONMENT_VERSION,
        "reward": REWARD_VERSION,
        "capture": "native IDs/logprobs and assistant masks from ms-swift/vLLM; no re-tokenized rollout claims",
    }
    write_json(attempt_path, record)
    write_json(run_path, record)
    if prepare_only:
        return record
    log_path = output / "trainer.log"
    try:
        returncode = _run_logged(command, environment, log_path)
    except BaseException:
        record["status"] = "interrupted"
        record["ended_at"] = datetime.now(timezone.utc).isoformat()
        record["trainer_log"] = str(log_path)
        write_json(run_path, record)
        raise
    record.update(
        status="completed" if returncode == 0 else "failed",
        returncode=returncode,
        ended_at=datetime.now(timezone.utc).isoformat(),
        trainer_log=str(log_path),
    )
    write_json(run_path, record)
    return record
