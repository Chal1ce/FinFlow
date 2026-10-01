"""Operate the daily data flywheel without installing a scheduler implicitly."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from contextlib import nullcontext
from pathlib import Path

from storage.flywheel_store import FlywheelStore
from storage.flywheel_review import human_review, trace
from training.flywheel_corpus import build_snapshot, verify
from workflow.flywheel import DailyFlywheel, DailyLock
from workflow.flywheel_config import FlywheelConfig


def main(argv=None):
    parser = argparse.ArgumentParser(description="Daily financial document data flywheel")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config" / "flywheel.json")
    parser.add_argument("--data-root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight", help="check enabled roles, OCR and tokenizer; no network calls")
    run = commands.add_parser("run")
    run.add_argument("--batch-id", default=os.getenv("FIN_DOC_BATCH_ID"))
    run.add_argument("--no-discover", action="store_true", help="drain local queue and backfill existing OCR")
    commands.add_parser("status")
    audit = commands.add_parser("audit", help="list candidates for human sampling/review")
    audit.add_argument("--status", choices=["pending", "accepted", "rejected", "needs_review"])
    audit.add_argument("--method", choices=["original", "visual", "translate", "rewrite"])
    audit.add_argument("--limit", type=int, default=20)
    review = commands.add_parser("review-candidate", help="record an explicit human decision")
    review.add_argument("--candidate-id", required=True)
    review.add_argument("--decision", required=True, choices=["accepted", "rejected", "needs_review"])
    review.add_argument("--reason", required=True)
    review.add_argument("--reviewer", required=True)
    lineage = commands.add_parser("trace")
    identity = lineage.add_mutually_exclusive_group(required=True)
    identity.add_argument("--sample-id")
    identity.add_argument("--artifact-id")
    identity.add_argument("--candidate-id")
    retry = commands.add_parser("retry", help="explicitly retry a failed or reviewed task")
    retry.add_argument("--task-id", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--dataset-id", required=True)
    snapshot.add_argument("--delta", action="append", required=True)
    snapshot.add_argument("--origin-delta", action="append", default=[])
    check = commands.add_parser("verify")
    check.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        config = FlywheelConfig(args.config, data_root=args.data_root)
        if args.command == "preflight":
            result = config.preflight()
        elif args.command == "run":
            result = DailyFlywheel(config).run(batch_id=args.batch_id, discover=not args.no_discover)
        elif args.command == "snapshot":
            result = build_snapshot(config.root, args.dataset_id, args.delta, origin_deltas=args.origin_delta)
        elif args.command == "verify":
            result = {"status": "success", "manifest": verify(args.path)}
        else:
            lock = DailyLock(config.root) if args.command in {"retry", "review-candidate"} else nullcontext()
            with lock, FlywheelStore(config.root / "state" / "pipeline.db") as store:
                if args.command == "audit":
                    if not 1 <= args.limit <= 1000:
                        raise ValueError("audit limit must be between 1 and 1000")
                    rows = store.connection.execute(
                        "SELECT candidate_uid,method,status,data_json,decision_json FROM training_candidate "
                        "WHERE (? IS NULL OR status=?) AND (? IS NULL OR method=?) "
                        "ORDER BY created_at LIMIT ?",
                        (args.status, args.status, args.method, args.method, args.limit),
                    )
                    print(json.dumps({"status": "success", "candidates": [dict(row) for row in rows]}, ensure_ascii=False, indent=2))
                    return 0
                if args.command == "review-candidate":
                    result = human_review(config, store, args.candidate_id, args.decision, reason=args.reason, reviewer=args.reviewer)
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return 0
                if args.command == "trace":
                    result = trace(store, sample_uid=args.sample_id, artifact_uid=args.artifact_id, candidate_uid=args.candidate_id)
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return 0
                if args.command == "retry":
                    row = store.connection.execute("SELECT * FROM processing_task WHERE task_uid=?", (args.task_id,)).fetchone()
                    if not row or row["status"] not in {"failed", "needs_review", "rejected", "deferred"}:
                        raise ValueError("task does not exist or is not retryable")
                    new_task = store.enqueue(
                        row["kind"],
                        row["entity_uid"],
                        json.loads(row["payload_json"]),
                        version="manual-retry-" + uuid.uuid4().hex,
                        dependency=row["dependency_uid"],
                    )
                    with store.connection:
                        store.connection.execute("UPDATE processing_task SET status='superseded' WHERE task_uid=?", (args.task_id,))
                        store.connection.execute(
                            "UPDATE processing_task SET dependency_uid=? WHERE dependency_uid=? "
                            "AND status IN ('pending','retry_wait','deferred')",
                            (new_task, args.task_id),
                        )
                result = {
                    "status": "success",
                    "queue": store.counts(),
                    "candidates": {
                        r[0]: r[1] for r in store.connection.execute("SELECT status,count(*) FROM training_candidate GROUP BY status")
                    },
                    "visuals": {r[0]: r[1] for r in store.connection.execute("SELECT status,count(*) FROM visual_asset GROUP BY status")},
                    "split_conflicts": store.records("split-conflict"),
                }
                if args.command == "retry":
                    result["task_uid"] = new_task
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") == "failed" else 1 if result.get("status") == "partial" else 0
    except Exception as exc:
        # Do not print SDK payloads or credential-bearing exception strings.
        message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else "see persistent stage status"
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "message": message}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
