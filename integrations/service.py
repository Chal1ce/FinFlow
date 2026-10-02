"""Application-neutral operations shared by channels, CLI and MCP."""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.context import PipelineContext
from core.flywheel_files import register_file, safe_path, sha256, write_json
from core.ids import stable_uid
from ingestion.local_documents import LocalPdfImporter
from integrations.config import Actor, IntegrationConfig, CHANNELS
from integrations.store import IntegrationStore
from spiders.downloader import inspect_pdf
from storage.flywheel_review import human_review, trace
from storage.flywheel_store import FlywheelStore
from workflow.flywheel import DailyFlywheel, DailyLock
from workflow.flywheel_config import digest


@contextmanager
def readonly_database(path: Path):
    if not path.is_file():
        yield None
        return
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def tables(connection):
    return {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")} if connection else set()


class FinFlowService:
    def __init__(self, config: IntegrationConfig):
        self.config = config
        self.root = config.root

    def status(self, actor: Actor) -> dict:
        actor.require("viewer")
        result = {"status": "success", "queue": {}, "candidates": {}, "visuals": {}, "jobs": {}, "deliveries": {}}
        with readonly_database(self.root / "state/pipeline.db") as db:
            available = tables(db)
            for key, table in (("queue", "processing_task"), ("candidates", "training_candidate"), ("visuals", "visual_asset")):
                if table in available:
                    result[key] = {r[0]: r[1] for r in db.execute(f"SELECT status,count(*) FROM {table} GROUP BY status")}
        with readonly_database(self.root / "state/integrations.db") as db:
            if db:
                for key, table in (("jobs", "app_job"), ("deliveries", "app_outbox"), ("events", "app_event")):
                    result[key] = {r[0]: r[1] for r in db.execute(f"SELECT status,count(*) FROM {table} GROUP BY status")}
        return result

    def report(self, actor: Actor, date: str | None = None) -> dict:
        actor.require("viewer")
        wanted = datetime.strptime(date, "%Y-%m-%d").date() if date else datetime.now(self.config.timezone).date()
        runs = []
        with readonly_database(self.root / "state/pipeline.db") as db:
            if "flywheel_record" in tables(db):
                for row in db.execute("SELECT data_json FROM flywheel_record WHERE kind='daily' ORDER BY updated_at"):
                    report = json.loads(row[0])
                    stamp = datetime.fromisoformat(report["created_at"])
                    if stamp.astimezone(self.config.timezone).date() == wanted:
                        runs.append(report)
        # Counts of candidates/queue are cumulative snapshots, not daily additions.
        return {
            "date": wanted.isoformat(),
            "timezone": str(self.config.timezone),
            "run_count": len(runs),
            "completed_tasks": sum(r.get("completed_tasks", 0) for r in runs),
            "model_requests": sum(r.get("model_requests") or 0 for r in runs),
            "usage_missing": any(r.get("model_usage") is None for r in runs),
            "latest": runs[-1] if runs else None,
            "runs": [{k: r.get(k) for k in ("run_id", "status", "created_at", "dataset", "errors")} for r in runs],
        }

    def lineage(self, actor: Actor, kind: str, identity: str) -> dict:
        actor.require("viewer")
        if kind not in {"sample", "candidate", "artifact"}:
            raise ValueError("kind must be sample, candidate or artifact")
        with readonly_database(self.root / "state/pipeline.db") as db:
            if "artifact_edge" not in tables(db):
                raise ValueError("no lineage database yet")
            # trace() only reads .connection, avoiding schema initialization here.
            from types import SimpleNamespace

            return trace(SimpleNamespace(connection=db), **{kind + "_uid": identity})

    def candidates(self, actor: Actor, status="needs_review", limit=20) -> dict:
        actor.require("viewer")
        if status not in {"pending", "accepted", "rejected", "needs_review"} or not 1 <= limit <= 100:
            raise ValueError("invalid status or limit (1..100)")
        with readonly_database(self.root / "state/pipeline.db") as db:
            if "training_candidate" not in tables(db):
                return {"candidates": []}
            rows = db.execute(
                "SELECT candidate_uid,method,status,created_at FROM training_candidate WHERE status=? ORDER BY created_at LIMIT ?",
                (status, limit),
            ).fetchall()
        return {"candidates": [dict(r) for r in rows]}

    def candidate(self, actor: Actor, candidate_id: str) -> dict:
        actor.require("viewer")
        with readonly_database(self.root / "state/pipeline.db") as db:
            row = (
                db.execute("SELECT * FROM training_candidate WHERE candidate_uid=?", (candidate_id,)).fetchone()
                if ("training_candidate" in tables(db))
                else None
            )
            if row is None:
                raise ValueError("candidate does not exist")
        value = json.loads(row["data_json"])
        if sha256(safe_path(self.root, value["path"])) != value["sha256"]:
            raise ValueError("candidate checksum mismatch")
        if value.get("image_path") and sha256(safe_path(self.root, value["image_path"])) != value.get("image_sha256"):
            raise ValueError("evidence image checksum mismatch")
        return {
            "candidate_id": candidate_id,
            "status": row["status"],
            "method": row["method"],
            "sha256": value["sha256"],
            "decision_version": digest(row["decision_json"] or ""),
            "decision": json.loads(row["decision_json"] or "null"),
            "data": value,
        }

    def job(self, actor: Actor, job_id: str) -> dict:
        actor.require("viewer")
        with readonly_database(self.root / "state/integrations.db") as db:
            row = db.execute("SELECT * FROM app_job WHERE job_id=?", (job_id,)).fetchone() if db else None
        if row is None:
            raise ValueError("job does not exist")
        job = IntegrationStore.decode_job(row)
        return {key: job[key] for key in ("job_id", "action", "status", "result", "error_type", "created_at", "updated_at")}

    def deliveries(self, actor: Actor, status="failed", limit=20) -> dict:
        actor.require("viewer")
        if actor.channel != "local":
            raise PermissionError("delivery administration requires the local CLI")
        if status not in {"pending", "sent", "failed", "rejected"} or not 1 <= limit <= 100:
            raise ValueError("invalid delivery status or limit")
        with readonly_database(self.root / "state/integrations.db") as db:
            rows = (
                db.execute(
                    "SELECT delivery_id,status,attempts,error_type FROM app_outbox WHERE status=? ORDER BY rowid DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
                if db
                else []
            )
        return {"deliveries": [dict(row) for row in rows]}

    def retry_delivery(self, actor: Actor, delivery_id: str) -> dict:
        actor.require("operator")
        if actor.channel != "local":
            raise PermissionError("delivery administration requires the local CLI")
        with IntegrationStore(self.root) as store, store.db:
            changed = store.db.execute(
                "UPDATE app_outbox SET status='pending',attempts=0,available_at=0,error_type=NULL WHERE delivery_id=? AND status='failed'",
                (delivery_id,),
            ).rowcount
            if not changed:
                raise ValueError("delivery does not exist or is not failed")
            store.audit(actor.id, "retry_delivery", delivery_id, "queued")
        return {"delivery_id": delivery_id, "status": "pending"}

    def submit(self, actor: Actor, action: str, payload: dict, request_id: str, *, reply=None) -> dict:
        actor.require("reviewer" if action == "review" else "operator")
        if action not in {"run", "import_pdf", "retry_task", "review"}:
            raise ValueError("unsupported action")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", request_id):
            raise ValueError("request_id must be 1..200 ASCII letters, digits or ._:-")
        with IntegrationStore(self.root) as store:
            job = store.submit(action, actor, payload, digest([actor.id, request_id]), reply, max_jobs=self.config.max_jobs)
        return {"job_id": job["job_id"], "status": job["status"], "action": job["action"]}

    def start_run(self, actor, request_id, *, discover=True, reply=None):
        if not isinstance(discover, bool):
            raise ValueError("discover must be boolean")
        return self.submit(actor, "run", {"discover": discover}, request_id, reply=reply)

    def retry(self, actor, identity, request_id, *, reply=None):
        actor.require("operator")
        if identity.startswith("job-"):
            with IntegrationStore(self.root) as store:
                old = store.get_job(identity)
            if old["status"] not in {"failed", "interrupted", "partial"} or old["action"] == "review":
                raise ValueError("job is not retryable; obtain fresh evidence to repeat a review")
            return self.submit(actor, old["action"], old["payload"], request_id, reply=reply)
        return self.submit(actor, "retry_task", {"task_id": identity}, request_id, reply=reply)

    def import_pdf(self, actor, source_path, request_id, *, origin=None, reply=None, trusted_attachment=False):
        actor.require("operator")
        source_path = Path(source_path).resolve(strict=True)
        if actor.channel == "mcp":
            roots = [Path(p).expanduser().resolve() for p in self.config.mcp.get("import_roots", [])]
            if not any(source_path.is_relative_to(root) for root in roots):
                raise PermissionError("PDF is outside configured MCP import_roots")
        if actor.channel in CHANNELS and not trusted_attachment:
            raise PermissionError("channel import requires a verified message attachment")
        if not source_path.is_file() or source_path.suffix.lower() != ".pdf":
            raise ValueError("provide one PDF file")
        inbox = self.root / "integrations" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(suffix=".pdf", dir=inbox)
        temporary = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as output, source_path.open("rb") as source:
                total = 0
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > self.config.max_bytes:
                        raise ValueError("PDF exceeds the attachment limit")
                    output.write(chunk)
            inspection = inspect_pdf(temporary, min_size_bytes=100)
            if inspection.error:
                raise ValueError("invalid PDF attachment")
            content_hash = sha256(temporary)
            target = inbox / (content_hash + ".pdf")
            if target.exists() and sha256(target) != content_hash:
                raise ValueError("existing inbox file checksum mismatch")
            os.replace(temporary, target)
            metadata = {
                k: str(v)[:500]
                for k, v in (origin or {}).items()
                if k in {"app_id", "chat_id", "message_id", "file_id", "filename", "received_at", "thread_id"}
            }
            metadata.update({"actor_id": actor.id, "channel": actor.channel})
            payload = {
                "path": target.relative_to(self.root).as_posix(),
                "sha256": content_hash,
                "origin": metadata,
                "title": metadata.get("filename") or source_path.name,
                "source_uid": stable_uid("application-source", digest([actor.id, request_id])),
            }
            return self.submit(actor, "import_pdf", payload, request_id, reply=reply)
        finally:
            temporary.unlink(missing_ok=True)

    def review_ticket(self, actor: Actor, candidate_id: str, chat_id: str, *, chat_type="p2p") -> tuple[dict, str]:
        actor.require("reviewer")
        if actor.channel not in {*CHANNELS, "local"}:
            raise PermissionError("human review is available only to authenticated people")
        if chat_type not in {"p2p", "group"}:
            raise ValueError("unsupported conversation type")
        candidate = self.candidate(actor, candidate_id)
        ticket = secrets.token_urlsafe(24)
        with IntegrationStore(self.root) as store, store.db:
            store.db.execute(
                "INSERT INTO app_review VALUES(?,?,?,?,?,?,?,NULL,?)",
                (
                    digest(ticket),
                    actor.id,
                    chat_id,
                    candidate_id,
                    candidate["sha256"],
                    candidate["decision_version"],
                    time.time() + self.config.review_ttl,
                    chat_type,
                ),
            )
        return candidate, ticket

    def submit_review(self, actor, ticket, decision, reason, chat_id, *, reply=None):
        actor.require("reviewer")
        if actor.channel not in {*CHANNELS, "local"}:
            raise PermissionError("MCP cannot record a human decision")
        if decision not in {"accepted", "rejected", "needs_review"} or not 1 <= len(reason.strip()) <= 1000:
            raise ValueError("invalid decision or reason")
        with IntegrationStore(self.root) as store:
            row = store.db.execute("SELECT * FROM app_review WHERE ticket_hash=?", (digest(ticket),)).fetchone()
        if row is None or row["actor_id"] != actor.id or row["chat_id"] != chat_id:
            raise PermissionError("review ticket does not belong to this user and conversation")
        if actor.channel in CHANNELS:
            self.config.check_chat(actor.channel, chat_id, row["chat_type"])
        if row["expires_at"] < time.time():
            raise ValueError("review ticket expired; request the candidate again")
        payload = {
            "candidate_id": row["candidate_uid"],
            "candidate_sha": row["candidate_sha"],
            "decision_version": row["decision_version"],
            "decision": decision,
            "reason": reason.strip(),
            "expires_at": row["expires_at"],
            "ticket_hash": row["ticket_hash"],
        }
        if reply:
            reply = {**reply, "chat_type": row["chat_type"]}
        result = self.submit(actor, "review", payload, "review:" + row["ticket_hash"], reply=reply)
        with IntegrationStore(self.root) as store, store.db:
            store.db.execute("UPDATE app_review SET job_id=? WHERE ticket_hash=?", (result["job_id"], row["ticket_hash"]))
        return result

    def execute(self, job: dict) -> dict:
        actor = self.config.current_actor(Actor.from_dict(job["actor"]))
        action, payload = job["action"], job["payload"]
        actor.require("reviewer" if action == "review" else "operator")
        reply = job.get("reply") or {}
        if actor.channel in CHANNELS:
            self.config.check_chat(actor.channel, reply.get("chat_id"), reply.get("chat_type"))
        if action == "run":
            return DailyFlywheel(self.config.flywheel).run(batch_id=job["job_id"], discover=payload["discover"])
        if action == "review":
            if actor.channel not in {*CHANNELS, "local"}:
                raise PermissionError("MCP cannot record a human decision")
            with DailyLock(self.root), FlywheelStore(self.root / "state/pipeline.db") as store:
                current = self.candidate(actor, payload["candidate_id"])
                if (
                    payload["expires_at"] < time.time()
                    or current["sha256"] != payload["candidate_sha"]
                    or current["decision_version"] != payload["decision_version"]
                ):
                    raise ValueError("review evidence changed or expired; request a fresh review")
                return human_review(
                    self.config.flywheel, store, payload["candidate_id"], payload["decision"], reason=payload["reason"], reviewer=actor.id
                )
        if action == "import_pdf":
            imported = self._import_job(job)
            preflight = self.config.flywheel.preflight()
            if preflight["status"] != "success":
                return {
                    "status": "partial",
                    "import": imported,
                    "pipeline": preflight,
                    "message": "PDF and source lineage saved; OCR remains queued until configuration is complete",
                }
            return {"import": imported, **DailyFlywheel(self.config.flywheel).run(batch_id=job["job_id"], discover=False)}
        if action == "retry_task":
            with DailyLock(self.root), FlywheelStore(self.root / "state/pipeline.db") as store:
                previous = store.get("app-retry", job["job_id"])
                if previous:
                    return previous
                row = store.connection.execute("SELECT * FROM processing_task WHERE task_uid=?", (payload["task_id"],)).fetchone()
                if not row or row["status"] not in {"failed", "needs_review", "rejected", "deferred"}:
                    raise ValueError("task does not exist or is not retryable")
                replacement = store.enqueue(
                    row["kind"], row["entity_uid"], json.loads(row["payload_json"]), version=job["job_id"], dependency=row["dependency_uid"]
                )
                with store.connection:
                    store.connection.execute("UPDATE processing_task SET status='superseded' WHERE task_uid=?", (payload["task_id"],))
                    store.connection.execute(
                        "UPDATE processing_task SET dependency_uid=? WHERE dependency_uid=? "
                        "AND status IN ('pending','retry_wait','deferred')",
                        (replacement, payload["task_id"]),
                    )
                result = {"status": "success", "task_id": replacement, "message": "Task queued; run the pipeline to process it"}
                store.put("app-retry", job["job_id"], result)
                return result
        raise ValueError("unsupported action")

    def _import_job(self, job):
        payload = job["payload"]
        path = safe_path(self.root, payload["path"])
        if sha256(path) != payload["sha256"]:
            raise ValueError("uploaded PDF checksum mismatch")
        source_uid = payload["source_uid"]
        with DailyLock(self.root), FlywheelStore(self.root / "state/pipeline.db") as store:
            previous = store.get("app-import", source_uid)
            if previous:
                return previous
            source = {
                **payload["origin"],
                "source_record_uid": source_uid,
                "title": payload["title"],
                "source_name": "app-" + payload["origin"]["channel"],
                "source_id": source_uid,
                "sha256": payload["sha256"],
                "received_at": payload["origin"].get("received_at") or job["created_at"],
            }
            imported = LocalPdfImporter(self.root, state_store=store).run(
                path,
                batch_id=job["job_id"],
                metadata={
                    "source_name": source["source_name"],
                    "source_id": source_uid,
                    "title": payload["title"],
                    "source_metadata": source,
                },
            )
            if imported["status"] != "success":
                raise ValueError("PDF import failed validation")
            record = imported["records"][0]
            context = PipelineContext.create(self.root, batch_id=job["job_id"], config_version=self.config.flywheel.version)
            store.start_run(context, dry_run=False)
            try:
                source_path = self.root / "manifests/source_records" / (source_uid + ".json")
                write_json(source_path, source)
                origin_artifact = register_file(store, context, source_path, "application-source", identity=source_uid)
                pdf_artifact = register_file(
                    store,
                    context,
                    safe_path(self.root, record["raw_path"]),
                    "flywheel-pdf",
                    (origin_artifact,),
                    document_uid=record["document_uid"],
                )
                document = {
                    "storage_group": "financial",
                    "source": source,
                    "work_uid": record["work_uid"],
                    "document_uid": record["document_uid"],
                    "asset_uid": record["asset_uid"],
                    "raw_path": record["raw_path"],
                    "raw_file_hash": record["raw_file_hash"],
                    "page_count": record["page_count"],
                    "pdf_artifact_uid": pdf_artifact,
                    "source_record_uid": source_uid,
                }
                store.put("document", stable_uid(record["work_uid"], record["asset_uid"], source_uid), document)
                task = store.enqueue(
                    "ocr", stable_uid(record["work_uid"], record["asset_uid"], source_uid), document, version=self.config.flywheel.version
                )
                result = {
                    "document_id": record["document_uid"],
                    "asset_id": record["asset_uid"],
                    "source_artifact": origin_artifact,
                    "pdf_artifact": pdf_artifact,
                    "task_id": task,
                }
                store.put("app-import", source_uid, result)
                store.finish_run(context.run_id, "success")
                return result
            except BaseException as exc:
                store.finish_run(context.run_id, "failed", type(exc).__name__)
                raise
