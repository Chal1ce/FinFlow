"""Durable ingress, command queue, review tickets and delivery outbox."""

from __future__ import annotations

import fcntl
import json
import sqlite3
import time
import uuid
from pathlib import Path

from storage.state_store import utc_now


class ProcessLock:
    def __init__(self, root: Path, name: str):
        self.path = root / "state" / f".{name}.lock"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.handle.close()
            raise
        return self

    def __exit__(self, *_):
        self.handle.close()


class IntegrationStore:
    def __init__(self, root: Path):
        path = root / "state" / "integrations.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS app_job(
                job_id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, action TEXT NOT NULL,
                actor_json TEXT NOT NULL, payload_json TEXT NOT NULL, reply_json TEXT,
                status TEXT NOT NULL, result_json TEXT, error_type TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS app_event(
                event_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0, error_type TEXT);
            CREATE TABLE IF NOT EXISTS app_outbox(
                delivery_id TEXT PRIMARY KEY, delivery_key TEXT UNIQUE NOT NULL, target_json TEXT NOT NULL,
                payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL DEFAULT 0, error_type TEXT, receipt_json TEXT);
            CREATE TABLE IF NOT EXISTS app_review(
                ticket_hash TEXT PRIMARY KEY, actor_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                candidate_uid TEXT NOT NULL, candidate_sha TEXT NOT NULL, decision_version TEXT NOT NULL,
                expires_at REAL NOT NULL, job_id TEXT, chat_type TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS app_audit(
                id INTEGER PRIMARY KEY, actor_id TEXT NOT NULL, action TEXT NOT NULL,
                resource TEXT NOT NULL, outcome TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS app_cursor(cursor_key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def audit(self, actor_id, action, resource, outcome):
        self.db.execute("INSERT INTO app_audit VALUES(NULL,?,?,?,?,?)", (actor_id, action, resource, outcome, utc_now()))

    def submit(self, action, actor, payload, request_key, reply, *, max_jobs):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT * FROM app_job WHERE request_key=?", (request_key,)).fetchone()
            if row:
                if (
                    row["action"] != action
                    or json.loads(row["payload_json"]) != payload
                    or json.loads(row["actor_json"]) != actor.to_dict()
                ):
                    raise ValueError("request_id was already used with different parameters")
                self.db.commit()
                return self.decode_job(row)
            count = self.db.execute("SELECT count(*) FROM app_job WHERE status IN ('pending','running')").fetchone()[0]
            if count >= max_jobs:
                raise ValueError("application job queue is full")
            job = "job-" + uuid.uuid4().hex
            now = utc_now()
            self.db.execute(
                "INSERT INTO app_job VALUES(?,?,?,?,?,?,?,NULL,NULL,?,?)",
                (
                    job,
                    request_key,
                    action,
                    json.dumps(actor.to_dict()),
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(reply) if reply else None,
                    "pending",
                    now,
                    now,
                ),
            )
            self.audit(actor.id, action, job, "queued")
            self.db.commit()
            return self.get_job(job)
        except BaseException:
            self.db.rollback()
            raise

    @staticmethod
    def decode_job(row):
        value = dict(row)
        for key in ("actor", "payload", "reply", "result"):
            value[key] = json.loads(value.pop(key + "_json") or "null")
        return value

    def get_job(self, job_id):
        row = self.db.execute("SELECT * FROM app_job WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("job does not exist")
        return self.decode_job(row)

    def recover_jobs(self):
        # Called only while holding the worker's process lock. Uncertain writes are
        # surfaced for explicit inspection, never automatically executed again.
        with self.db:
            rows = self.db.execute("SELECT * FROM app_job WHERE status='running'").fetchall()
            for row in rows:
                self.finish_job(row["job_id"], "interrupted", error_type="WorkerInterrupted")

    def claim_job(self):
        with self.db:
            row = self.db.execute("SELECT job_id FROM app_job WHERE status='pending' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            self.db.execute("UPDATE app_job SET status='running',updated_at=? WHERE job_id=?", (utc_now(), row[0]))
        return self.get_job(row[0])

    def finish_job(self, job_id, status, result=None, error_type=None):
        job = self.get_job(job_id)
        self.db.execute(
            "UPDATE app_job SET status=?,result_json=?,error_type=?,updated_at=? WHERE job_id=?",
            (status, json.dumps(result, ensure_ascii=False), error_type, utc_now(), job_id),
        )
        self.audit(job["actor"]["id"], job["action"], job_id, status)
        if job["reply"]:
            self.queue_delivery(
                "job:" + job_id + ":" + status,
                job["reply"],
                {
                    "kind": "text",
                    "text": f"FinFlow {job_id}\n状态：{status}\n"
                    + (f"错误类型：{error_type}" if error_type else json.dumps(result, ensure_ascii=False)[:6000]),
                },
            )

    def queue_event(self, event_key, payload):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO app_event(event_key,payload_json) VALUES(?,?)", (event_key, json.dumps(payload, ensure_ascii=False))
            )

    def queue_delivery(self, key, target, payload):
        self.db.execute(
            "INSERT OR IGNORE INTO app_outbox(delivery_id,delivery_key,target_json,payload_json) VALUES(?,?,?,?)",
            (uuid.uuid5(uuid.NAMESPACE_URL, key).hex, key, json.dumps(target), json.dumps(payload, ensure_ascii=False)),
        )

    def next_item(self, table):
        if table not in {"app_event", "app_outbox"}:
            raise ValueError("invalid queue")
        return self.db.execute(
            f"SELECT * FROM {table} WHERE status='pending' AND available_at<=? ORDER BY rowid LIMIT 1", (time.time(),)
        ).fetchone()

    def fail_item(self, table, key, error_type, *, retry_after=0):
        if table not in {"app_event", "app_outbox"}:
            raise ValueError("invalid queue")
        column = "event_key" if table == "app_event" else "delivery_id"
        row = self.db.execute(f"SELECT attempts FROM {table} WHERE {column}=?", (key,)).fetchone()
        attempts = row[0] + 1
        with self.db:
            self.db.execute(
                f"UPDATE {table} SET attempts=?,status=?,available_at=?,error_type=? WHERE {column}=?",
                (
                    attempts,
                    "failed" if attempts >= 5 else "pending",
                    time.time() + max(retry_after, min(3600, 5 * 2**attempts)),
                    error_type,
                    key,
                ),
            )
