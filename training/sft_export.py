"""Portable, separate text/vision exports and an optional PIL loading adapter."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path

from core.flywheel_files import safe_path, sha256
from training.flywheel_corpus import jsonl, read_jsonl, verify


def sample_hash(sample):
    from workflow.flywheel_config import digest

    if sample.get("images"):
        return digest({"messages": sample["messages"], "image_sha256s": sample["image_sha256s"]})
    return digest(sample["messages"])


def inspect_image(path, expected_hash, policy):
    from PIL import Image

    path = Path(path)
    if path.stat().st_size > policy.get("max_image_bytes", 10485760):
        raise ValueError("image_byte_limit")
    if sha256(path) != expected_hash:
        raise ValueError("image_checksum_mismatch")
    with Image.open(path) as image:
        width, height = image.size
        if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
            raise ValueError("expected_single_frame_png")
        if width <= 0 or height <= 0 or width * height > policy.get("max_image_pixels", 40000000):
            raise ValueError("image_pixel_limit")
        image.verify()
    return {"width": width, "height": height, "bytes": path.stat().st_size, "mime": "image/png"}


def export_files(root, staging, samples, evidence):
    sources = {e["evidence_uid"]: e for e in evidence}
    images = {}
    for sample in samples:
        if sample.get("modality") != "vision":
            continue
        source = sources[sample["evidence_uid"]]
        expected = source["image_sha256"]
        if sample["images"] != [f"images/{expected}.png"] or sample["image_sha256s"] != [expected]:
            raise ValueError("sample_image_binding_mismatch")
        original = safe_path(root, source["image_path"])
        if sha256(original) != expected:
            raise ValueError("image_changed_before_export")
        target = safe_path(staging, sample["images"][0])
        if expected not in images:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
            if sha256(target) != expected:
                raise ValueError("exported_image_checksum_mismatch")
            images[expected] = {
                "path": target.relative_to(staging).as_posix(),
                "sha256": expected,
                "source_artifact_uids": [],
                **source["image_info"],
            }
        if source["image_artifact_uid"] not in images[expected]["source_artifact_uids"]:
            images[expected]["source_artifact_uids"].append(source["image_artifact_uid"])
    jsonl(staging / "images.jsonl", sorted(images.values(), key=lambda x: x["sha256"]))
    for split in ("train", "validation"):
        for vision in (False, True):
            rows = []
            for sample in samples:
                if sample["split"] != split or (sample.get("modality") == "vision") != vision:
                    continue
                row = {"sample_id": sample["sample_id"], "messages": sample["messages"]}
                if vision:
                    row["images"] = sample["images"]
                rows.append(row)
            jsonl(staging / (split + (".vision" if vision else "") + ".jsonl"), rows)
    return {"images": len(images), "modalities": dict(Counter(s.get("modality", "text") for s in samples))}


def validate_messages(row, vision):
    messages = row.get("messages")
    if not isinstance(messages, list) or [m.get("role") for m in messages] != ["system", "user", "assistant"]:
        raise ValueError("invalid_export_messages")
    placeholders = 0
    for message in messages:
        content = message.get("content")
        if not vision:
            if not isinstance(content, str) or not content.strip():
                raise ValueError("invalid_text_export")
            continue
        if not isinstance(content, list) or not content:
            raise ValueError("invalid_vision_content")
        for block in content:
            if not isinstance(block, dict):
                raise ValueError("invalid_vision_block")
            if block.get("type") == "image" and message["role"] == "user" and set(block) == {"type"}:
                placeholders += 1
            elif block.get("type") != "text" or not isinstance(block.get("text"), str) or not block["text"].strip():
                raise ValueError("invalid_vision_block")
    if vision and (placeholders != 1 or not isinstance(row.get("images"), list) or len(row["images"]) != placeholders):
        raise ValueError("image_placeholder_mismatch")


def verify_dataset(path):
    """Validate checksums, image bindings and split exports without model credentials."""
    path = Path(path).resolve()
    manifest = verify(path)
    if manifest.get("schema_version") == "financial-sft-dataset-v1":
        return manifest
    if manifest.get("schema_version") != "financial-sft-dataset-v2":
        raise ValueError("not_a_supported_sft_dataset")
    samples = read_jsonl(path / "samples.jsonl")
    canonical = {s["sample_id"]: s for s in samples}
    if len(canonical) != len(samples) or len(samples) != manifest["samples"]:
        raise ValueError("sample_inventory_mismatch")
    content_splits = {}
    for sample in samples:
        if sample["content_hash"] != sample_hash(sample):
            raise ValueError("sample_content_hash_mismatch")
        previous = content_splits.setdefault(sample["content_hash"], sample["split"])
        if previous != sample["split"]:
            raise ValueError("duplicate_sample_crosses_splits")
    media = {}
    for image in read_jsonl(path / "images.jsonl"):
        if image["path"] != f"images/{image['sha256']}.png" or image["path"] in media:
            raise ValueError("invalid_media_inventory")
        actual = inspect_image(safe_path(path, image["path"]), image["sha256"], manifest["recipe"])
        if any(image.get(key) != value for key, value in actual.items()):
            raise ValueError("media_metadata_mismatch")
        media[image["path"]] = image
    visited, referenced, image_splits = set(), set(), {}
    for split in ("train", "validation"):
        for vision in (False, True):
            for row in read_jsonl(path / (split + (".vision" if vision else "") + ".jsonl")):
                uid = row.get("sample_id")
                if uid not in canonical or uid in visited:
                    raise ValueError("split_sample_inventory_mismatch")
                sample = canonical[uid]
                if sample["split"] != split or sample["messages"] != row["messages"] or (sample.get("modality") == "vision") != vision:
                    raise ValueError("split_sample_content_mismatch")
                validate_messages(row, vision)
                if vision:
                    if row["images"] != sample["images"] or sample["image_sha256s"] != [media[p]["sha256"] for p in row["images"]]:
                        raise ValueError("export_image_binding_mismatch")
                    for name in row["images"]:
                        previous = image_splits.setdefault(name, split)
                        if previous != split:
                            raise ValueError("same_image_crosses_splits")
                    referenced.update(row["images"])
                elif "images" in row:
                    raise ValueError("unexpected_text_images")
                visited.add(uid)
    if visited != set(canonical) or referenced != set(media) or len(media) != manifest["images"]:
        raise ValueError("incomplete_export_inventory")
    if manifest["modalities"] != dict(Counter(s.get("modality", "text") for s in samples)):
        raise ValueError("modality_counts_mismatch")
    return manifest


def load_vision_records(dataset_path, split="train"):
    """Yield trainer records with PIL images; no training framework dependency."""
    from PIL import Image

    if split not in {"train", "validation"}:
        raise ValueError("split must be train or validation")
    path = Path(dataset_path).resolve()
    manifest = verify_dataset(path)
    if manifest["schema_version"] != "financial-sft-dataset-v2":
        raise ValueError("vision loading requires an SFT v2 dataset")
    with (path / f"{split}.vision.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            images = []
            for filename in row["images"]:
                with Image.open(safe_path(path, filename)) as image:
                    images.append(image.copy())
            yield {"messages": row["messages"], "images": images}
