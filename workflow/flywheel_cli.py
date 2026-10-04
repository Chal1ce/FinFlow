"""Operate the daily data flywheel without installing a scheduler implicitly."""

from __future__ import annotations

import argparse
import json
import os
import uuid
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import nullcontext
from pathlib import Path

from storage.flywheel_store import FlywheelStore
from storage.flywheel_review import human_review, trace
from training.flywheel_corpus import build_snapshot, verify
from workflow.flywheel import DailyFlywheel, DailyLock
from workflow.flywheel_config import FlywheelConfig
from core.context import PipelineContext
from workflow.daily_report import aggregate, inventory, persist_daily, recover_interrupted
from storage.state_store import utc_now


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
    report = commands.add_parser("report", help="read a calendar-day report without calling providers")
    report.add_argument("--date", help="YYYY-MM-DD in FIN_DOC_REPORT_TIMEZONE")
    publish = commands.add_parser("publish-training", help="resume training publication only; no discovery, OCR or models")
    publish.add_argument("--sft-dataset", type=Path, help="explicit completed SFT snapshot; otherwise reuse current evidence export")
    release_check = commands.add_parser("verify-training", help="verify a portable final training release")
    release_check.add_argument("path", type=Path)
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
        elif args.command == "verify-training":
            from training.daily_release import verify_release

            result = {"status": "success", "manifest": verify_release(args.path)}
        elif args.command == "publish-training":
            from training.daily_release import DailyTrainingRelease
            from training.flywheel_corpus import FlywheelCorpusBuilder, TokenSplitter
            from delivery.flywheel_release import evidence_identity
            from training.sft import SFTBuilder

            if not config.release_options["enabled"]:
                raise ValueError("set FIN_DOC_RELEASE_ENABLED=true to publish training versions")
            splitter = TokenSplitter(config.tokenizer, config.max_tokens)
            with DailyLock(config.root), FlywheelStore(config.root / "state" / "pipeline.db") as store:
                recover_interrupted(config.root, store, config.report_timezone)
                context = PipelineContext.create(config.root, config_version=config.version)
                store.start_run(context, dry_run=False)
                before = inventory(store)
                store.put("daily-start", context.run_id, {**context.as_mapping(), "inventory": before})
                started = time.monotonic()
                builder = FlywheelCorpusBuilder(
                    config.root, store, splitter, validation_fraction=float(config.policy.get("validation_fraction", 0.05))
                )
                sft_dataset = {"path": str(args.sft_dataset.resolve())} if args.sft_dataset else None
                try:
                    if config.sft and not sft_dataset:
                        release_id = evidence_identity(store, config.version)
                        if (config.root / "published" / "flywheel" / release_id / "manifest.json").exists():
                            sft_dataset = SFTBuilder(
                                config.root, store, context, config.sft, config.policy.get("source_policy", {})
                            ).reusable_snapshot(release_id)
                    publication = DailyTrainingRelease(config, store, context, deadline=started + config.deadline_seconds).run(
                        cpt_recipe=builder.recipe_uid, sft_dataset=sft_dataset
                    )
                except Exception as exc:
                    publication = {"status": "failed", "stage": "publication_inputs", "error_type": type(exc).__name__}
                result = {
                    "status": "partial" if publication["status"] in {"failed", "deferred"} else publication["status"],
                    "operation": "publish-training",
                    "run_id": context.run_id,
                    "batch_id": context.batch_id,
                    "created_at": utc_now(),
                    "completed_tasks": 0,
                    "model_requests": 0,
                    "model_usage": {},
                    "queue": store.counts(),
                    "training_release": publication,
                    "additions": {k: v - before[k] for k, v in inventory(store).items()},
                    "errors": (
                        [{"stage": publication.get("stage", "latest"), "error_type": publication["error_type"]}]
                        if publication.get("error_type")
                        else []
                    ),
                }
                persist_daily(config.root, store, result, timezone=config.report_timezone)
                store.finish_run(context.run_id, result["status"])
        elif args.command == "snapshot":
            result = build_snapshot(config.root, args.dataset_id, args.delta, origin_deltas=args.origin_delta)
        elif args.command == "verify":
            result = {"status": "success", "manifest": verify(args.path)}
        else:
            lock = DailyLock(config.root) if args.command in {"retry", "review-candidate"} else nullcontext()
            with lock, FlywheelStore(config.root / "state" / "pipeline.db") as store:
                if args.command == "report":
                    zone = ZoneInfo(config.report_timezone)
                    date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(zone).date()
                    runs = [r for r in store.records("daily") if datetime.fromisoformat(r["created_at"]).astimezone(zone).date() == date]
                    print(json.dumps(aggregate(runs, date.isoformat(), config.report_timezone), ensure_ascii=False, indent=2))
                    return 0
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
                    "training_latest": store.get("training-latest", "current"),
                    "training_stages": store.records("training-stage"),
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
