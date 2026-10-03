"""Freeze document-local causal-LM blocks for controlled data-mixture experiments."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections import Counter
from importlib.metadata import version
from pathlib import Path

from core.flywheel_files import sha256, write_json
from training.flywheel_corpus import checksums, jsonl
from training.mixture_data import load_pool
from workflow.flywheel_config import digest


def prepare(config, output):
    from tokenizers import Tokenizer

    if config.get("schema_version") != "finflow-mixture-experiment-v1":
        raise ValueError("expected finflow-mixture-experiment-v1")
    sequence = config.get("sequence_length", 256)
    limit = config.get("max_prepared_tokens", 10000000)
    if type(sequence) is not int or not 8 <= sequence <= 8192 or type(limit) is not int or limit < 1:
        raise ValueError("invalid sequence_length or max_prepared_tokens")
    records, manifest = load_pool(config["inputs"], config.get("group_fields", ["source_name"]))
    if manifest["kind"] != "cpt":
        raise ValueError("DoReMi/RegMix experiments require CPT packages, not SFT messages")
    tokenizer_path = Path(config["tokenizer"])
    tokenizer_hash = sha256(tokenizer_path)
    if any(r["tokenizer"].get("sha256") != tokenizer_hash for r in records):
        raise ValueError("use the same tokenizer.json as the CPT input packages")
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.no_padding()
    tokenizer.no_truncation()
    eos = config.get("eos_token_id")
    vocab = tokenizer.get_vocab_size(with_added_tokens=True)
    if type(eos) is not int or not 0 <= eos < vocab:
        raise ValueError("configure a valid eos_token_id for this tokenizer")
    if max(tokenizer.get_vocab().values()) >= vocab:
        raise ValueError("tokenizer has non-contiguous IDs")
    names = sorted({r["group"] for r in records if r["split"] == "train"})
    if len(names) < 2:
        raise ValueError("mixture learning requires at least two training domains")
    if {r["group"] for r in records if r["split"] == "validation"} != set(names):
        raise ValueError("each training domain requires an existing held-out validation split; regenerate a larger source snapshot")
    destination = Path(output).resolve()
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".domains-", dir=destination.parent))
    counts, blocks, dropped, total = Counter(), Counter(), Counter(), 0
    try:
        for split in ("train", "validation"):
            rows = []
            for record in records:
                if record["split"] != split:
                    continue
                ids = tokenizer.encode(record["payload"]["text"], add_special_tokens=False).ids + [eos]
                # Adjacent windows share one input token; each target is trained exactly once per pass.
                for start in range(0, len(ids) - 1, sequence):
                    values = ids[start : start + sequence + 1]
                    n = len(values) - 1
                    if split == "train" and n < sequence:
                        dropped[record["group"]] += n
                        continue
                    total += n
                    if total > limit:
                        raise ValueError("prepared data exceeds max_prepared_tokens; select a smaller frozen snapshot or raise the limit")
                    group = record["group"]
                    rows.append(
                        {
                            "id": digest([record["id"], start]),
                            "sample_id": record["id"],
                            "group": group,
                            "family": record["family"],
                            "input_ids": values,
                            "token_offset": start,
                            "split": split,
                        }
                    )
                    counts[split + ":" + group] += n
                    blocks[split + ":" + group] += 1
            jsonl(staging / f"{split}.jsonl", rows)
        if any(blocks[split + ":" + name] == 0 for split in ("train", "validation") for name in names):
            raise ValueError("empty tokenized domain")
        shutil.copyfile(tokenizer_path, staging / "tokenizer.json")
        manifest.update(
            {
                "schema_version": "finflow-domain-data-v1",
                "engine": "finflow-domain-prepare-v1",
                "tokenizers_version": version("tokenizers"),
                "domains": names,
                "sequence_length": sequence,
                "tokenizer_sha256": tokenizer_hash,
                "eos_token_id": eos,
                "vocab_size": vocab,
                "tokens": dict(counts),
                "blocks": dict(blocks),
                "total_target_tokens": total,
                "discarded_train_tail_tokens": dict(dropped),
                "packing": "document-local; fixed-length training blocks (short tails dropped); validation tails retained and masked",
            }
        )
        write_json(staging / "manifest.json", manifest)
        checksums(staging)
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"status": "success", "path": str(destination), "manifest": manifest}
