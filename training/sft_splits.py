"""Freeze SFT work groups before generation and respect existing CPT assignments."""

from __future__ import annotations

import json

from core.ids import stable_uid
from training.flywheel_corpus import read_jsonl, verify


def split_keys(item):
    keys = [("sft-work-split", item["work_uid"])]
    if item["text"].strip():
        keys.append(("sft-content-split", item["content_hash"]))
    if item.get("image_sha256"):
        keys.append(("sft-image-split", item["image_sha256"]))
    return keys


def assign_splits(store, evidence, fraction, root):
    parent, hashes = {}, {}

    def find(work):
        parent.setdefault(work, work)
        while parent[work] != work:
            parent[work] = parent[parent[work]]
            work = parent[work]
        return work

    for item in evidence:
        work = item["work_uid"]
        find(work)
        for key in split_keys(item)[1:]:
            if key in hashes:
                a, b = find(work), find(hashes[key])
                parent[max(a, b)] = min(a, b)
            hashes[key] = work
    # A copied image can connect selected evidence to another historical CPT work.
    for row in store.connection.execute("SELECT work_uid,sha256 FROM visual_asset WHERE status='success'"):
        key = ("sft-image-split", row["sha256"])
        if key in hashes:
            a, b = find(row["work_uid"]), find(hashes[key])
            parent[max(a, b)] = min(a, b)
    constraints = {}
    for row in store.connection.execute("SELECT metadata_json FROM training_origin"):
        origin = json.loads(row[0])
        if origin["work_uid"] in parent:
            constraints.setdefault(find(origin["work_uid"]), set()).add(origin["split"])
    # Include immutable CPT exports even if their SQLite indexing was interrupted.
    for kind in ("pretrain", "origin_deltas"):
        for directory in sorted((root / "training" / kind).glob("*")):
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            manifest = verify(directory)
            if manifest.get("schema_version") not in {"continued-pretraining-delta-v2", "training-lineage-delta-v1"}:
                continue
            for origin in read_jsonl(directory / "provenance.jsonl"):
                if origin["work_uid"] in parent:
                    constraints.setdefault(find(origin["work_uid"]), set()).add(origin["split"])
    for work in parent:
        previous = store.get("sft-work-split", work)
        if previous:
            constraints.setdefault(find(work), set()).add(previous["split"])
    for item in evidence:
        group = find(item["work_uid"])
        for kind, key in split_keys(item):
            previous = store.get(kind, key)
            if previous:
                constraints.setdefault(group, set()).add(previous["split"])
    groups = {}
    for work in parent:
        groups.setdefault(find(work), []).append(work)
    assignments = {}
    for group, works in groups.items():
        choices = constraints.get(group, set())
        assignments[group] = (
            None
            if len(choices) > 1
            else next(iter(choices))
            if choices
            else "validation"
            if int(stable_uid(min(works), length=8), 16) / 0xFFFFFFFF < fraction
            else "train"
        )
    for item in evidence:
        split = assignments[find(item["work_uid"])]
        item["split"] = split
        if split is not None:
            # Caller holds the shared pipeline lock. Persist before any model call.
            for kind, key in split_keys(item):
                store.put(kind, key, {"split": split, "work_uid": item["work_uid"]})
    for work in parent:
        split = assignments[find(work)]
        if split is not None:
            store.put("sft-work-split", work, {"split": split, "work_uid": work})
    return evidence
