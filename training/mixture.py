"""Deterministic, no-replacement dataset selection with explicit shortfalls and portable exports."""

from __future__ import annotations

import math
import hashlib
import os
import random
import shutil
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from core.flywheel_files import safe_path, sha256, write_json
from training.flywheel_corpus import checksums, jsonl, read_jsonl, verify
from training.mixture_data import load_pool
from training.sft_export import sample_hash, validate_messages
from workflow.flywheel_config import digest


def normalize_weights(weights, names):
    if not isinstance(weights, dict) or set(weights) != set(names):
        raise ValueError("weights must name every available training group exactly once (zero is allowed)")
    if any(type(v) not in (float, int) or not math.isfinite(v) or v < 0 for v in weights.values()):
        raise ValueError("weights must be finite and nonnegative")
    total = sum(weights.values())
    if not math.isfinite(total) or total <= 0:
        raise ValueError("at least one weight must be positive")
    return {name: weights[name] / total for name in sorted(names)}


def plan(config):
    if config.get("schema_version") != "finflow-mixture-v1":
        raise ValueError("expected finflow-mixture-v1")
    records, manifest = load_pool(config["inputs"], config.get("group_fields", ["method"]))
    dedup_decisions = None
    if config.get("near_dedup_report"):
        from training.near_dedup import apply_report

        if config.get("method") in {"doremi", "regmix"}:
            raise ValueError("learned weights describe the original pool; near-dedup requires a new experiment integration")
        report_path = Path(config["near_dedup_report"])
        dedup_decisions, dedup_manifest = apply_report(records, manifest, report_path)
        manifest["near_dedup"] = {
            "inventory_sha256": sha256(report_path / "checksums.sha256"),
            "counts": dedup_manifest["counts"],
            "policy": dedup_manifest["policy"],
        }
    unit = config.get("unit", "tokens" if manifest["kind"] == "cpt" else "samples")
    if unit not in {"samples", "tokens"} or (manifest["kind"] == "sft" and unit != "samples"):
        raise ValueError("SFT mixtures use samples; CPT mixtures support samples or tokens")
    budget, seed = config.get("budget"), config.get("seed", 42)
    cap = config.get("max_per_family")
    if type(budget) is not int or budget <= 0 or type(seed) is not int:
        raise ValueError("budget must be a positive integer and seed an integer")
    if cap is not None and (type(cap) is not int or cap <= 0):
        raise ValueError("max_per_family must be a positive sample count")
    for key in ("redistribute", "allow_shortfall"):
        if type(config.get(key, False)) is not bool:
            raise ValueError(key + " must be boolean")
    buckets = defaultdict(list)
    for row in records:
        if row["split"] == "train":
            bucket = buckets[row["group"]]
            if dedup_decisions is None or dedup_decisions[row["id"]]["action"] == "keep":
                bucket.append(row)
    if not buckets:
        raise ValueError("no training records")
    if unit == "tokens" and len({digest(r["tokenizer"]) for r in records}) != 1:
        raise ValueError("token mixtures require identical tokenizer recipes")

    def size(r):
        return r["token_count"] if unit == "tokens" else 1

    available = {k: sum(size(r) for r in v) for k, v in buckets.items()}
    if not sum(available.values()):
        raise ValueError("no training records remain after near-dedup")
    method = config.get("method", "temperature")
    learned = None
    if method == "temperature":
        alpha = config.get("alpha", 0.5)
        if type(alpha) not in (float, int) or not math.isfinite(alpha) or not 0 <= alpha <= 2:
            raise ValueError("alpha must be finite in [0,2]")
        weights = normalize_weights({k: v**alpha for k, v in available.items()}, available)
    elif method == "fixed":
        weights = normalize_weights(config.get("weights"), available)
    elif method in {"doremi", "regmix"}:
        import json

        weight_path = Path(config["weights_file"])
        learned = json.loads(weight_path.read_text(encoding="utf-8"))
        if (
            learned.get("schema_version") != "finflow-learned-weights-v1"
            or learned.get("method") != method
            or learned.get("pool_uid") != manifest["pool_uid"]
            or learned.get("group_fields") != manifest["group_fields"]
        ):
            raise ValueError("learned weights do not match this frozen pool, grouping or method")
        if manifest["kind"] != "cpt" or unit != "tokens":
            raise ValueError("learned CPT weights must be applied using token quotas")
        weights = normalize_weights(learned["weights"], available)
        manifest["weights_sha256"] = sha256(weight_path)
    else:
        raise ValueError("method must be fixed, temperature, doremi or regmix")
    quotas = {k: math.floor(budget * w) for k, w in weights.items()}
    remainder = budget - sum(quotas.values())
    for key in sorted(weights, key=lambda k: (-(budget * weights[k] - quotas[k]), k))[:remainder]:
        quotas[key] += 1
    selected, totals, families = [], Counter(), Counter()
    order = sorted(buckets)
    random.Random(seed).shuffle(order)
    for key in order:
        random.Random(digest([seed, key])).shuffle(buckets[key])
    # Optional diversity selection consumes explicit, content-bound scores; never turns invalid facts into valid data.
    selection = config.get("selection")
    selector = None
    if selection:
        from training.mixture_selection import Selector

        selector = Selector(selection, records)
        for key in order:
            buckets[key].sort(key=selector.rank)
        manifest["selection"] = selector.identity
    leftovers = []

    def take(row):
        if cap is not None and families[row["family"]] >= cap:
            return False
        if selector and not selector.accept(row):
            return False
        selected.append(row)
        totals[row["group"]] += size(row)
        families[row["family"]] += 1
        return True

    for key in order:
        for row in buckets[key]:
            if totals[key] + size(row) <= quotas[key] and take(row):
                continue
            leftovers.append(row)
    if config.get("redistribute", False):
        total = sum(totals.values())
        for row in sorted(leftovers, key=lambda r: digest([seed, r["id"]])):
            if weights[row["group"]] > 0 and total + size(row) <= budget and take(row):
                total += size(row)
    actual = sum(totals.values())
    manifest.update(
        {
            "schema_version": "finflow-mixture-dataset-v1",
            "recipe": config,
            "method": method,
            "unit": unit,
            "budget": budget,
            "actual": actual,
            "shortfall": budget - actual,
            "status": "success" if actual == budget else "partial",
            "seed": seed,
            "groups": {
                k: {
                    "available": available[k],
                    "weight": weights[k],
                    "quota": quotas[k],
                    "selected": totals[k],
                    "actual_fraction": totals[k] / actual if actual else 0,
                }
                for k in sorted(weights)
            },
            "train_samples": len(selected),
            "validation_samples": sum(r["split"] == "validation" for r in records),
            "replacement": False,
            "validation_policy": "all-original-validation-records-unmixed",
            "shortfall_reasons": "availability, intact-sample granularity, family cap, near-dedup or selection filters",
            "tokenizer": records[0]["tokenizer"] if manifest["kind"] == "cpt" else None,
            "learned_experiment": learned.get("experiment_uid") if learned else None,
        }
    )
    return selected + [r for r in records if r["split"] == "validation"], manifest


def build(config, output):
    rows, manifest = plan(config)
    if manifest["shortfall"] and not config.get("allow_shortfall", False):
        raise ValueError("requested budget cannot be filled; inspect plan or explicitly set allow_shortfall")
    destination = Path(output).resolve()
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".mixture-", dir=destination.parent))
    try:
        exported, media = [], {}
        for row in rows:
            payload = dict(row["payload"])
            # Dataset-local IDs are unique even when upstream packages reused sample IDs.
            payload["sample_id"] = row["id"]
            for name, expected in zip(payload.get("images", []), payload.get("image_sha256s", [])):
                source = safe_path(row["package_path"], name)
                if sha256(source) != expected:
                    raise ValueError("image changed while building mixture")
                dest = safe_path(staging, name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if name not in media:
                    shutil.copyfile(source, dest)
                    media[name] = expected
            exported.append({k: v for k, v in row.items() if k != "package_path"} | {"payload": payload})
        jsonl(staging / "records.jsonl", exported)
        for split in ("train", "validation"):
            if manifest["kind"] == "cpt":
                jsonl(staging / f"{split}.jsonl", [r["payload"] for r in exported if r["split"] == split])
            else:
                for vision in (False, True):
                    values = []
                    for r in exported:
                        p = r["payload"]
                        if r["split"] != split or bool(p.get("images")) != vision:
                            continue
                        value = {"sample_id": p["sample_id"], "messages": p["messages"]}
                        if vision:
                            value["images"] = p["images"]
                        values.append(value)
                    jsonl(staging / (split + (".vision" if vision else "") + ".jsonl"), values)
        write_json(staging / "images.json", media)
        if manifest.get("near_dedup"):
            shutil.copytree(config["near_dedup_report"], staging / "near-dedup")
            verify(staging / "near-dedup")
            if sha256(staging / "near-dedup" / "checksums.sha256") != manifest["near_dedup"]["inventory_sha256"]:
                raise ValueError("near-dedup report changed while building mixture")
        if config.get("method") in {"doremi", "regmix"}:
            weight_path = Path(config["weights_file"])
            shutil.copyfile(weight_path, staging / "learned-weights.json")
            if sha256(staging / "learned-weights.json") != manifest["weights_sha256"]:
                raise ValueError("learned weights changed while building mixture")
        write_json(staging / "manifest.json", manifest)
        checksums(staging)
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"path": str(destination), **manifest}


def verify_mixture(path):
    import json

    path = Path(path).resolve()
    manifest = verify(path)
    if manifest.get("schema_version") != "finflow-mixture-dataset-v1":
        raise ValueError("not a mixture package")
    rows = read_jsonl(Path(path) / "records.jsonl")
    if manifest.get("near_dedup"):
        verify(path / "near-dedup")
        if sha256(path / "near-dedup" / "checksums.sha256") != manifest["near_dedup"]["inventory_sha256"]:
            raise ValueError("near-dedup report fingerprint mismatch")
        decisions = {d["id"]: d for d in read_jsonl(path / "near-dedup" / "decisions.jsonl")}
        if any(r["id"] not in decisions or decisions[r["id"]]["action"] != "keep" for r in rows):
            raise ValueError("mixture contains an excluded near-duplicate")
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("duplicate selection IDs")
    if Counter(r["split"] for r in rows) != Counter({"train": manifest["train_samples"], "validation": manifest["validation_samples"]}):
        raise ValueError("mixture count mismatch")
    totals, families, images = Counter(), {}, {}
    for row in rows:
        payload = row["payload"]
        if payload.get("sample_id") != row["id"] or payload.get("split") != row["split"]:
            raise ValueError("mixture payload identity mismatch")
        actual_hash = hashlib.sha256(payload["text"].encode()).hexdigest() if manifest["kind"] == "cpt" else sample_hash(payload)
        if actual_hash != row["content_hash"] or digest([manifest["kind"], actual_hash]) != row["id"]:
            raise ValueError("mixture content identity mismatch")
        if families.setdefault(row["family"], row["split"]) != row["split"]:
            raise ValueError("mixture family crosses splits")
        if row["split"] == "train":
            totals[row["group"]] += row["token_count"] if manifest["unit"] == "tokens" else 1
        if manifest["kind"] == "sft":
            validate_messages(payload, bool(payload.get("images")))
            if len(payload.get("images", [])) != len(payload.get("image_sha256s", [])):
                raise ValueError("mixture image binding mismatch")
            for name, expected in zip(payload.get("images", []), payload.get("image_sha256s", [])):
                if name != f"images/{expected}.png" or sha256(safe_path(path, name)) != expected:
                    raise ValueError("mixture image checksum mismatch")
                if images.setdefault(name, (expected, row["split"])) != (expected, row["split"]):
                    raise ValueError("mixture image crosses splits")
    if sum(totals.values()) != manifest["actual"] or any(totals[k] != g["selected"] for k, g in manifest["groups"].items()):
        raise ValueError("mixture quotas mismatch")
    if json.loads((path / "images.json").read_text()) != {k: v[0] for k, v in images.items()}:
        raise ValueError("mixture image inventory mismatch")
    for split in ("train", "validation"):
        if manifest["kind"] == "cpt":
            expected = [r["payload"] for r in rows if r["split"] == split]
            if read_jsonl(path / f"{split}.jsonl") != expected:
                raise ValueError("CPT mixture export mismatch")
        else:
            for vision in (False, True):
                expected = []
                for row in rows:
                    p = row["payload"]
                    if row["split"] == split and bool(p.get("images")) == vision:
                        value = {"sample_id": p["sample_id"], "messages": p["messages"]}
                        if vision:
                            value["images"] = p["images"]
                        expected.append(value)
                if read_jsonl(path / (split + (".vision" if vision else "") + ".jsonl")) != expected:
                    raise ValueError("SFT mixture export mismatch")
    return manifest


def load_vision_records(path, split="train"):
    from PIL import Image

    if split not in {"train", "validation"}:
        raise ValueError("split must be train or validation")
    manifest = verify_mixture(path)
    if manifest["kind"] != "sft":
        raise ValueError("expected an SFT mixture")
    for row in read_jsonl(Path(path) / f"{split}.vision.jsonl"):
        media = []
        for filename in row["images"]:
            with Image.open(safe_path(path, filename)) as image:
                media.append(image.copy())
        yield {"messages": row["messages"], "images": media}
