"""Freeze SFT work groups before generation and respect existing CPT assignments."""

from __future__ import annotations

import json

from core.ids import stable_uid
from training.flywheel_corpus import read_jsonl, verify


def assign_splits(store, evidence, fraction, root):
    parent, hashes = {}, {}

    def find(work):
        parent.setdefault(work, work)
        while parent[work] != work:
            parent[work] = parent[parent[work]]
            work = parent[work]
        return work

    for item in evidence:
        work, key = item["work_uid"], item["content_hash"]
        find(work)
        if key in hashes:
            a, b = find(work), find(hashes[key])
            parent[max(a, b)] = min(a, b)
        hashes[key] = work
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
    for item in evidence:
        group = find(item["work_uid"])
        for kind, key in (("sft-work-split", item["work_uid"]), ("sft-content-split", item["content_hash"])):
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
            store.put("sft-work-split", item["work_uid"], {"split": split, "work_uid": item["work_uid"]})
            store.put("sft-content-split", item["content_hash"], {"split": split, "work_uid": item["work_uid"]})
    return evidence
