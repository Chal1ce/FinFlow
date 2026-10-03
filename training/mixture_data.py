"""Read immutable CPT/SFT packages without flattening away provenance or split constraints."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path

from core.flywheel_files import sha256
from training.flywheel_corpus import read_jsonl, verify
from training.sft_export import sample_hash, verify_dataset
from workflow.flywheel_config import digest

FIELDS = {"method", "task", "strategy", "source_name", "language", "modality"}


def groups(records, fields):
    if not isinstance(fields, list) or not fields or len(set(fields)) != len(fields) or not set(fields) <= FIELDS:
        raise ValueError("group_fields must be unique fields from: " + ", ".join(sorted(FIELDS)))
    for record in records:
        labels = []
        for field in fields:
            values = {str(m.get(field) or "unknown") for m in record["metadata"]}
            label = next(iter(values)) if len(values) == 1 else "mixed"
            if "|" in label:
                raise ValueError("group labels must not contain |")
            labels.append(label)
        record["group"] = "|".join(labels)


def load_pool(paths, group_fields, *, max_records=1000000):
    if not isinstance(paths, list) or not paths or len(set(map(str, paths))) != len(paths):
        raise ValueError("inputs must be a nonempty list of distinct snapshot paths")
    records, inputs, seen_paths, additional_origins = {}, [], set(), []
    kind = None
    for value in paths:
        path = Path(value).resolve()
        if path in seen_paths:
            raise ValueError("duplicate snapshot path")
        seen_paths.add(path)
        manifest = verify(path)
        schema = manifest.get("schema_version", "")
        vision = schema == "financial-sft-dataset-v2"
        current_kind = "sft" if vision else "cpt"
        if not vision and schema not in {"continued-pretraining-delta-v2", "continued-pretraining-snapshot-v2"}:
            raise ValueError("mixtures require CPT v2 or SFT v2 packages")
        if kind and kind != current_kind:
            raise ValueError("CPT and SFT require separate mixture recipes")
        kind = current_kind
        manifest_hash = sha256(path / "manifest.json")
        inventory_hash = sha256(path / "checksums.sha256")
        package_id = digest([manifest_hash, inventory_hash])
        inputs.append(
            {
                "path": str(path),
                "manifest_sha256": manifest_hash,
                "inventory_sha256": inventory_hash,
                "package_uid": package_id,
                "schema_version": schema,
            }
        )
        if vision:
            verify_dataset(path)
            evidence = {e["evidence_uid"]: e for e in read_jsonl(path / "evidence.jsonl")}
            rows = read_jsonl(path / "samples.jsonl")
            origins = None
        else:
            origins = defaultdict(list)
            for origin in read_jsonl(path / "provenance.jsonl"):
                origins[origin["sample_uid"]].append(origin)
            rows = read_jsonl(path / "corpus.jsonl")
            ids = {r["sample_id"] for r in rows}
            additional_origins.extend({**o, "package": package_id} for key, items in origins.items() if key not in ids for o in items)
        for row in rows:
            if row.get("split") not in {"train", "validation"}:
                raise ValueError("unsupported or missing split")
            if vision:
                if row.get("decision", {}).get("status") != "accepted":
                    raise ValueError("mixture input includes an unaccepted SFT sample")
                content = sample_hash(row)
                source = row.get("source", evidence[row["evidence_uid"]].get("source", {}))
                metadata = [
                    {
                        "task": row["task"],
                        "strategy": row.get("strategy", "direct"),
                        "method": "sft",
                        "source_name": source.get("source_name"),
                        "language": source.get("language"),
                        "modality": row.get("modality", "text"),
                    }
                ]
                lineage = [
                    {"work_uid": row["work_uid"], "sample_id": row["sample_id"], "artifact_uid": row["artifact_uid"], "package": package_id}
                ]
                count = None
            else:
                if not isinstance(row.get("text"), str) or not row["text"].strip():
                    raise ValueError("CPT text must be nonempty")
                content = hashlib.sha256(row["text"].encode()).hexdigest()
                lineage = [{**o, "package": package_id} for o in origins[row["sample_id"]]]
                if not lineage:
                    raise ValueError("CPT sample has no provenance")
                if any(o.get("split") != row["split"] or o.get("content_hash") != content for o in lineage):
                    raise ValueError("CPT provenance mismatch")
                if any(o.get("decision", {}).get("status") != "accepted" for o in lineage):
                    raise ValueError("CPT provenance contains an unaccepted candidate")
                metadata = [
                    {
                        "method": o["method"],
                        "source_name": o.get("source", {}).get("source_name"),
                        "language": o.get("source", {}).get("language"),
                        "modality": "text",
                    }
                    for o in lineage
                ]
                count = row.get("token_count")
                if type(count) is not int or count <= 0:
                    raise ValueError("CPT token_count must be positive")
            uid = digest([kind, content])
            record = records.get(uid)
            if record:
                if record["split"] != row["split"]:
                    raise ValueError("identical content crosses input splits")
                if not vision and record["tokenizer"] != manifest["tokenizer"]:
                    raise ValueError("CPT inputs must share a tokenizer identity")
                record["metadata"].extend(metadata)
                record["origins"].extend(lineage)
            else:
                records[uid] = {
                    "id": uid,
                    "kind": kind,
                    "content_hash": content,
                    "split": row["split"],
                    "payload": row,
                    "metadata": metadata,
                    "origins": lineage,
                    "token_count": count,
                    "package_path": str(path),
                    "tokenizer": manifest.get("tokenizer"),
                }
            if len(records) > max_records:
                raise ValueError("mixture pool exceeds max_records; select smaller snapshots")
    if not records:
        raise ValueError("empty mixture pool")
    for origin in additional_origins:
        record = records.get(digest(["cpt", origin["content_hash"]]))
        if record is None:
            raise ValueError("a selected delta references earlier samples; use a complete cumulative snapshot")
        if (
            origin.get("split") != record["split"]
            or origin.get("tokenizer") != record["tokenizer"]
            or origin.get("decision", {}).get("status") != "accepted"
        ):
            raise ValueError("cross-delta provenance mismatch")
        record["origins"].append(origin)
        record["metadata"].append(
            {
                "method": origin["method"],
                "source_name": origin.get("source", {}).get("source_name"),
                "language": origin.get("source", {}).get("language"),
                "modality": "text",
            }
        )
    rows = sorted(records.values(), key=lambda r: r["id"])
    # Union all known document families, including cross-source duplicates, before applying family caps.
    parents, assigned, image_splits = {}, {}, {}

    def find(x):
        parents.setdefault(x, x)
        while parents[x] != x:
            parents[x] = parents[parents[x]]
            x = parents[x]
        return x

    for r in rows:
        works = sorted({o["work_uid"] for o in r["origins"]})
        for work in works[1:]:
            a, b = find(works[0]), find(work)
            parents[max(a, b)] = min(a, b)
        r["works"] = works
        for image_hash in r["payload"].get("image_sha256s", []):
            if image_splits.setdefault(image_hash, r["split"]) != r["split"]:
                raise ValueError("same image crosses input splits")
    for r in rows:
        r["family"] = find(r["works"][0])
        if assigned.setdefault(r["family"], r["split"]) != r["split"]:
            raise ValueError("document family crosses input splits")
        r["origins"] = list({digest(o): o for o in r["origins"]}.values())
    groups(rows, group_fields)
    identity = digest({"inputs": sorted(i["package_uid"] for i in inputs), "group_fields": group_fields})
    return rows, {"kind": kind, "inputs": inputs, "pool_uid": identity, "group_fields": group_fields}
