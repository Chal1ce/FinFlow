"""Connect existing FinFlow SFT exports to a single-GPU ms-swift trainer."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from core.flywheel_files import safe_path, write_json

DEFAULT_CONFIG = Path(__file__).with_name("qwen3.5-0.8b.json")


def _swift_record(row, dataset):
    messages = []
    placeholders = 0
    for message in row["messages"]:
        content = message["content"]
        if isinstance(content, list):
            parts = []
            for block in content:
                if block["type"] == "image":
                    parts.append("<image>")
                    placeholders += 1
                elif block["type"] == "text":
                    parts.append(block["text"])
                else:
                    raise ValueError("unsupported content block in SFT export")
            content = "".join(parts)
        if not isinstance(content, str):
            raise ValueError("SFT message content must be text or text/image blocks")
        messages.append({**message, "content": content})
    result = {"messages": messages}
    if row.get("images"):
        images = [safe_path(dataset, name) for name in row["images"]]
        if placeholders != len(images) or any(not image.is_file() for image in images):
            raise ValueError("SFT image placeholder or file is missing")
        result["images"] = [str(image) for image in images]
    elif placeholders:
        raise ValueError("SFT image block has no image file")
    return result


def _prepare(dataset, destination, modality, max_samples):
    """Serialize trainer inputs; upstream governance decisions are reused."""
    counts = {}
    for split in ("train", "validation"):
        count = 0
        names = [f"{split}.jsonl", f"{split}.vision.jsonl"]
        if modality == "text":
            names = names[:1]
        elif modality == "vision":
            names = names[1:]
        with (destination / f"{split}.jsonl").open("w", encoding="utf-8") as output:
            for name in names:
                path = dataset / name
                if not path.is_file():
                    continue
                with path.open(encoding="utf-8") as source:
                    for line in source:
                        if not line.strip():
                            continue
                        if max_samples is not None and count >= max_samples:
                            break
                        output.write(json.dumps(_swift_record(json.loads(line), dataset), ensure_ascii=False) + "\n")
                        count += 1
        counts[split] = count
    if not counts["train"]:
        raise ValueError("selected SFT export has no training rows; use an existing SFT dataset, not OCR or assets.jsonl")
    return counts


def run_sft(
    dataset,
    output,
    *,
    config=DEFAULT_CONFIG,
    model=None,
    modality="all",
    max_steps=None,
    max_samples=None,
    resume_from_checkpoint=None,
    prepare_only=False,
):
    dataset, output = Path(dataset).resolve(), Path(output).resolve()
    if not dataset.is_dir():
        raise ValueError("SFT dataset directory does not exist")
    if output == dataset or dataset in output.parents:
        raise ValueError("training output must be outside the source SFT dataset")
    if output.exists() and any(output.iterdir()) and not resume_from_checkpoint:
        raise ValueError("training output is not empty; choose a new output or pass --resume-from-checkpoint")
    if modality not in {"all", "text", "vision"}:
        raise ValueError("modality must be all, text or vision")
    if any(value is not None and (type(value) is not int or value <= 0) for value in (max_steps, max_samples)):
        raise ValueError("max-steps and max-samples must be positive integers")
    recipe = json.loads(Path(config).read_text(encoding="utf-8"))
    parameters = dict(recipe["arguments"])
    if max_steps is not None:
        parameters["max_steps"] = max_steps
    model = model or recipe["model"]
    swift = shutil.which("swift")
    if not prepare_only:
        try:
            version("ms-swift")
        except PackageNotFoundError as exc:
            raise ValueError("install agent/trainers/sft/requirements.txt in the GPU environment") from exc
        if not swift:
            raise ValueError("ms-swift executable missing from the active GPU environment")
    output.mkdir(parents=True, exist_ok=True)
    inputs = output / "finflow-inputs"
    inputs.mkdir(exist_ok=True)
    counts = _prepare(dataset, inputs, modality, max_samples)
    command = [swift or "swift", "sft", "--model", model, "--dataset", str(inputs / "train.jsonl")]
    command.extend(["--output_dir", str(output / "checkpoints")])
    if counts["validation"]:
        command.extend(["--val_dataset", str(inputs / "validation.jsonl"), "--eval_strategy", "steps", "--eval_steps", "50"])
    else:
        command.extend(["--eval_strategy", "no"])
    if resume_from_checkpoint:
        checkpoint = Path(resume_from_checkpoint).resolve()
        if not checkpoint.is_dir():
            raise ValueError("resume checkpoint directory does not exist")
        command.extend(["--resume_from_checkpoint", str(checkpoint)])
    for key, value in parameters.items():
        command.extend(["--" + key, str(value).lower() if isinstance(value, bool) else str(value)])
    environment = os.environ.copy()
    environment.setdefault("CUDA_VISIBLE_DEVICES", "0")
    environment["NPROC_PER_NODE"] = "1"
    environment["IMAGE_MAX_TOKEN_NUM"] = str(recipe["image_max_token_num"])
    packages = {}
    for package in ("ms-swift", "transformers", "torch", "peft", "qwen-vl-utils"):
        try:
            packages[package] = version(package)
        except PackageNotFoundError:
            packages[package] = None
    record = {
        "status": "prepared" if prepare_only else "running",
        "model": model,
        "dataset": str(dataset),
        "modality": modality,
        "rows": counts,
        "command": command,
        "packages": packages,
        "image_max_token_num": recipe["image_max_token_num"],
    }
    write_json(output / "finflow-run.json", record)
    if prepare_only:
        return record
    try:
        process = subprocess.run(command, env=environment, check=False)
    except BaseException:
        record["status"] = "interrupted"
        write_json(output / "finflow-run.json", record)
        raise
    record.update(status="completed" if process.returncode == 0 else "failed", returncode=process.returncode)
    write_json(output / "finflow-run.json", record)
    return record
