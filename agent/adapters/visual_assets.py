"""Import governed evidence into agent assets without another review workflow."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from pathlib import Path

from core.flywheel_files import safe_path, sha256, write_json
from training.flywheel_corpus import checksums, jsonl, verify
from training.sft_evidence import EvidenceLoader
from workflow.flywheel_config import digest


def import_assets(data_root, release, output, *, source_policy, policy=None):
    """Reuse the SFT evidence loader and export local, portable agent assets.

    The database is opened read-only. Accepted candidates retain their upstream
    decision and occurrence identities; this command does not create reviews,
    model requests, training examples, or a separate split ledger.
    """
    root = Path(data_root).resolve()
    release_path = safe_path(root / "published" / "flywheel", release)
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("asset output already exists; choose a new directory")
    if output == release_path or release_path in output.parents:
        raise ValueError("asset output must be outside the source release")
    manifest = verify(release_path)
    if manifest.get("format_version") != "financial-document-delivery-v7":
        raise ValueError("agent assets require the existing v7 evidence release")
    if not isinstance(source_policy, dict) or any(not isinstance(value, dict) for value in source_policy.values()):
        raise ValueError("source_policy must map source names to policy objects")
    # Select both text and visual evidence; generation settings and model roles
    # are irrelevant when importing already governed assets.
    policy = {**(policy or {}), "tasks": ["document_qa", "visual_qa"]}
    database = root / "state" / "pipeline.db"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        evidence, excluded = EvidenceLoader(root, connection, policy, source_policy).load(release_path)
        # Reuse existing work assignments when available. Unassigned assets can
        # still be imported; the existing SFT splitter assigns them at generation.
        assignments = {
            row["uid"]: json.loads(row["data_json"])["split"]
            for row in connection.execute("SELECT uid,data_json FROM flywheel_record WHERE kind='sft-work-split'")
        }

    release_ref = {
        "path": release_path.relative_to(root).as_posix(),
        "manifest_sha256": sha256(release_path / "manifest.json"),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".agent-assets-", dir=output.parent) as temporary:
        staging = Path(temporary) / "snapshot"
        staging.mkdir()
        records = []
        for item in sorted(evidence, key=lambda value: value["evidence_uid"]):
            image = None
            if item.get("image_path"):
                relative = f"media/{item['image_sha256']}.png"
                destination = staging / relative
                if not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(safe_path(root, item["image_path"]), destination)
                    if sha256(destination) != item["image_sha256"]:
                        raise ValueError("upstream image changed during asset import")
                image = {
                    "path": relative,
                    "sha256": item["image_sha256"],
                    "artifact_uid": item["image_artifact_uid"],
                    **item["image_info"],
                }
            records.append(
                {
                    "asset_id": item["evidence_uid"],
                    "kind": item["kind"],
                    "work_uid": item["work_uid"],
                    "split": assignments.get(item["work_uid"]),
                    "source": item["source"],
                    "text": item["text"],
                    "content_hash": item["content_hash"],
                    "image": image,
                    "visual_asset_uid": item["visual_asset_uid"],
                    "page": item["page"],
                    "block_index": item["block_index"],
                    "image_origin": item["image_origin"],
                    "source_offsets": item["source_offsets"],
                    "source_chunk_versions": item["source_chunk_versions"],
                    "upstream": {
                        "candidate_uid": item["candidate_uid"],
                        "decision_artifact_uid": item["upstream_decision_artifact_uid"],
                        "artifact_uids": item["parents"],
                        "decision": "accepted",
                        "release": release_ref,
                    },
                }
            )
        jsonl(staging / "assets.jsonl", records)
        jsonl(staging / "excluded.jsonl", excluded)
        summary = {
            "assets": len(records),
            "kinds": dict(sorted(Counter(item["kind"] for item in records).items())),
            "images": len({item["image"]["sha256"] for item in records if item["image"]}),
            "excluded": len(excluded),
        }
        result = {
            "schema_version": "agent-governed-assets-v1",
            "status": "complete",
            "release": release_ref,
            "policy_sha256": digest({"evidence": policy, "sources": source_policy}),
            "governance": "inherited_from_sft_evidence_loader",
            "summary": summary,
        }
        result["snapshot_id"] = digest(
            {
                **result,
                "assets_sha256": sha256(staging / "assets.jsonl"),
                "excluded_sha256": sha256(staging / "excluded.jsonl"),
            }
        )
        write_json(staging / "manifest.json", result)
        checksums(staging)
        staging.rename(output)
    return result
