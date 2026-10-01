"""v7 evidence snapshots include dependency files, images and multi-parent lineage."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from core.flywheel_files import safe_path, sha256, write_json
from storage.state_store import utc_now
from training.flywheel_corpus import checksums, jsonl, verify


def publish_evidence(root, store, release_id):
    root = Path(root)
    destination = safe_path(root / "published" / "flywheel", release_id)
    if destination.exists():
        verify(destination)
        return {
            "path": str(destination.relative_to(root)),
            "release_id": release_id,
            "manifest_sha256": sha256(destination / "manifest.json"),
        }
    candidates = [dict(row) for row in store.connection.execute("SELECT * FROM training_candidate ORDER BY candidate_uid")]
    visuals = [dict(row) for row in store.connection.execute("SELECT * FROM visual_asset ORDER BY visual_asset_uid")]
    descriptions = [dict(row) for row in store.connection.execute("SELECT * FROM visual_description ORDER BY description_uid")]
    roots = {x["artifact_uid"] for x in candidates + visuals + descriptions}
    roots |= {json.loads(x["decision_json"])["artifact_uid"] for x in candidates if x["decision_json"]}
    roots |= {r[0] for r in store.connection.execute("SELECT artifact_uid FROM artifact WHERE artifact_type='flywheel-governed'")}
    roots |= {r[0] for r in store.connection.execute("SELECT artifact_uid FROM artifact WHERE artifact_type IN "
                                                   "('ocr-image-output','table-representation','visual-context',"
                                                   "'training-transform','governance-model-response')")}
    edges = [dict(row) for row in store.connection.execute("SELECT * FROM artifact_edge ORDER BY child_uid,parent_uid,relation")]
    closure = set(roots)
    while True:
        expanded = closure | {e["parent_uid"] for e in edges if e["child_uid"] in closure}
        if expanded == closure:
            break
        closure = expanded
    artifacts = []
    for uid in sorted(closure):
        row = store.connection.execute("SELECT * FROM artifact WHERE artifact_uid=?", (uid,)).fetchone()
        if row is None:
            raise ValueError("missing evidence artifact")
        artifacts.append(dict(row))
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".release-", dir=destination.parent))
    try:
        for artifact in artifacts:
            source = safe_path(root, artifact["path"])
            if sha256(source) != artifact["sha256"]:
                raise ValueError("evidence artifact checksum mismatch")
            target = staging / "evidence" / artifact["artifact_uid"] / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            artifact["source_path"] = artifact["path"]
            artifact["path"] = target.relative_to(staging).as_posix()
        jsonl(staging / "artifacts.jsonl", artifacts)
        jsonl(staging / "artifact-edges.jsonl", [e for e in edges if e["child_uid"] in closure])
        jsonl(staging / "visual-assets.jsonl", visuals)
        jsonl(staging / "visual-descriptions.jsonl", descriptions)
        jsonl(staging / "training-candidates.jsonl", candidates)
        jsonl(staging / "governed-documents.jsonl", store.records("governed"))
        write_json(
            staging / "manifest.json",
            {
                "format_version": "financial-document-delivery-v7",
                "release_id": release_id,
                "created_at": utc_now(),
                "scope": "cumulative-evidence-snapshot",
                "artifacts": len(artifacts),
                "path_resolution": "artifacts.jsonl maps source paths to package-relative evidence files",
            },
        )
        checksums(staging)
        verify(staging)
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"path": str(destination.relative_to(root)), "release_id": release_id, "manifest_sha256": sha256(destination / "manifest.json")}
