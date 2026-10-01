"""Explicit human decisions and sample-to-source inspection."""

from __future__ import annotations

import json
import uuid

from core.context import PipelineContext
from core.flywheel_files import register_file, safe_path, sha256, write_json
from storage.state_store import utc_now


def human_review(config, store, candidate_uid, status, *, reason, reviewer):
    row = store.connection.execute("SELECT * FROM training_candidate WHERE candidate_uid=?", (candidate_uid,)).fetchone()
    if row is None:
        raise ValueError("candidate does not exist")
    candidate = json.loads(row["data_json"])
    policy = config.policy.get("source_policy", {}).get(candidate["source"].get("source_name"), {})
    if status == "accepted" and policy.get("training") != "approved":
        raise ValueError("approve the source training policy before accepting a candidate")
    if not reason.strip() or not reviewer.strip():
        raise ValueError("reason and reviewer are required")
    if sha256(safe_path(config.root, candidate["path"])) != candidate["sha256"]:
        raise ValueError("candidate file checksum mismatch")
    context = PipelineContext.create(config.root, config_version=config.version)
    store.start_run(context, dry_run=False)
    try:
        decision = {
            "status": status,
            "candidate_uid": candidate_uid,
            "input_sha256": candidate["sha256"],
            "reasons": [reason],
            "reviewer": reviewer,
            "decision_type": "human",
            "source_policy": policy,
            "policy_version": config.version,
            "created_at": utc_now(),
        }
        path = config.root / "training" / "decisions" / ("human-" + uuid.uuid4().hex + ".json")
        write_json(path, decision)
        parents = [candidate["artifact_uid"]]
        if row["decision_json"]:
            parents.append(json.loads(row["decision_json"])["artifact_uid"])
        decision["artifact_uid"] = register_file(store, context, path, "training-decision", tuple(parents))
        with store.connection:
            store.connection.execute(
                "UPDATE training_candidate SET status=?,decision_json=? WHERE candidate_uid=?",
                (status, json.dumps(decision, ensure_ascii=False), candidate_uid),
            )
            store.connection.execute(
                "UPDATE processing_task SET status='superseded' WHERE kind='review' AND entity_uid=? "
                "AND status IN ('needs_review','rejected','failed','pending','retry_wait')",
                (candidate_uid,),
            )
        store.finish_run(context.run_id, "success")
        return {"status": "success", "decision": decision}
    except BaseException as exc:
        store.finish_run(context.run_id, "failed", type(exc).__name__)
        raise


def trace(store, *, sample_uid=None, artifact_uid=None, candidate_uid=None):
    roots, origins = set(), []
    if sample_uid:
        origins = [
            json.loads(r[0])
            for r in store.connection.execute(
                "SELECT metadata_json FROM training_origin WHERE sample_uid=? ORDER BY candidate_uid,segment", (sample_uid,)
            )
        ]
        for origin in origins:
            roots.add(origin["artifact_uid"])
            roots.add(origin["decision"]["artifact_uid"])
    elif candidate_uid:
        row = store.connection.execute(
            "SELECT artifact_uid,decision_json FROM training_candidate WHERE candidate_uid=?", (candidate_uid,)
        ).fetchone()
        if row:
            roots.add(row[0])
            if row[1]:
                roots.add(json.loads(row[1])["artifact_uid"])
    else:
        roots.add(artifact_uid)
    if not roots:
        raise ValueError("no lineage record found")
    edges, artifacts, visited = [], [], set()
    while roots:
        uid = roots.pop()
        if uid in visited:
            continue
        visited.add(uid)
        artifact = store.connection.execute("SELECT * FROM artifact WHERE artifact_uid=?", (uid,)).fetchone()
        if not artifact:
            raise ValueError("missing lineage artifact")
        artifacts.append(dict(artifact))
        for edge in store.connection.execute("SELECT * FROM artifact_edge WHERE child_uid=?", (uid,)):
            edges.append(dict(edge))
            roots.add(edge["parent_uid"])
    return {
        "status": "success",
        "origins": origins,
        "artifacts": sorted(artifacts, key=lambda x: x["artifact_uid"]),
        "edges": sorted(edges, key=lambda x: (x["child_uid"], x["parent_uid"])),
    }
