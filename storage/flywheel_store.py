"""Additive flywheel schema and durable leased task queue."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from core.ids import stable_uid
from storage.state_store import StateStore, utc_now


class FlywheelStore(StateStore):
    def __init__(self, path):
        # Back up an existing database before the first additive migration.
        path = Path(path)
        if path.exists():
            with sqlite3.connect(path) as source:
                installed = source.execute("SELECT 1 FROM sqlite_master WHERE name='flywheel_schema'").fetchone()
                if not installed:
                    backup = path.with_name(path.name + ".before-flywheel.bak")
                    if not backup.exists():
                        with sqlite3.connect(backup) as target:
                            source.backup(target)
        super().__init__(path)
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS flywheel_schema(version INTEGER PRIMARY KEY, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS processing_task(
                task_uid TEXT PRIMARY KEY, kind TEXT NOT NULL, entity_uid TEXT NOT NULL,
                payload_json TEXT NOT NULL, dependency_uid TEXT REFERENCES processing_task(task_uid),
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL DEFAULT 0, lease_until REAL, owner TEXT,
                result_json TEXT, error_type TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_task_ready ON processing_task(status,available_at,created_at);
            CREATE TABLE IF NOT EXISTS flywheel_record(
                kind TEXT NOT NULL, uid TEXT NOT NULL, data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL, PRIMARY KEY(kind,uid));
            CREATE TABLE IF NOT EXISTS visual_asset(
                visual_asset_uid TEXT PRIMARY KEY, artifact_uid TEXT NOT NULL,
                document_uid TEXT NOT NULL, work_uid TEXT NOT NULL, source_asset_uid TEXT NOT NULL,
                asset_kind TEXT NOT NULL CHECK(asset_kind IN ('image','table')),
                ocr_label TEXT NOT NULL, page INTEGER NOT NULL CHECK(page>=1), block_index INTEGER NOT NULL,
                path TEXT, sha256 TEXT, status TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS visual_description(
                description_uid TEXT PRIMARY KEY, visual_asset_uid TEXT NOT NULL REFERENCES visual_asset(visual_asset_uid),
                artifact_uid TEXT NOT NULL, status TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifact_edge(
                child_uid TEXT NOT NULL REFERENCES artifact(artifact_uid),
                parent_uid TEXT NOT NULL REFERENCES artifact(artifact_uid), relation TEXT NOT NULL,
                PRIMARY KEY(child_uid,parent_uid,relation), CHECK(child_uid != parent_uid));
            CREATE TABLE IF NOT EXISTS training_candidate(
                candidate_uid TEXT PRIMARY KEY, work_uid TEXT NOT NULL, artifact_uid TEXT NOT NULL,
                method TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', data_json TEXT NOT NULL,
                decision_json TEXT, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS training_sample(
                content_hash TEXT NOT NULL, recipe_uid TEXT NOT NULL, sample_uid TEXT NOT NULL, dataset_id TEXT NOT NULL,
                split TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(recipe_uid,content_hash));
            CREATE TABLE IF NOT EXISTS training_origin(
                sample_uid TEXT NOT NULL, candidate_uid TEXT NOT NULL REFERENCES training_candidate(candidate_uid),
                segment INTEGER NOT NULL, metadata_json TEXT NOT NULL,
                PRIMARY KEY(sample_uid,candidate_uid,segment));
            INSERT OR IGNORE INTO flywheel_schema VALUES(1,datetime('now'));
        """)

    def put(self, kind, uid, data):
        with self.connection:
            self.connection.execute(
                "INSERT INTO flywheel_record VALUES(?,?,?,?) ON CONFLICT(kind,uid) "
                "DO UPDATE SET data_json=excluded.data_json,updated_at=excluded.updated_at",
                (kind, uid, json.dumps(data, ensure_ascii=False), utc_now()),
            )

    def get(self, kind, uid):
        row = self.connection.execute("SELECT data_json FROM flywheel_record WHERE kind=? AND uid=?", (kind, uid)).fetchone()
        return json.loads(row[0]) if row else None

    def records(self, kind):
        return [
            json.loads(row[0])
            for row in self.connection.execute("SELECT data_json FROM flywheel_record WHERE kind=? ORDER BY uid", (kind,))
        ]

    def enqueue(self, kind, entity_uid, payload, *, version, dependency=None):
        uid = stable_uid("task", kind, entity_uid, version)
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO processing_task"
                "(task_uid,kind,entity_uid,payload_json,dependency_uid,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (uid, kind, entity_uid, json.dumps(payload, ensure_ascii=False), dependency, utc_now(), utc_now()),
            )
        return uid

    def claim(self, owner, *, lease_seconds=3600, refiner_identity=None):
        now = time.time()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "UPDATE processing_task SET status='retry_wait',owner=NULL,lease_until=NULL,"
                "available_at=? WHERE status='running' AND lease_until<?",
                (now, now),
            )
            row = self.connection.execute(
                "SELECT t.* FROM processing_task t LEFT JOIN processing_task d "
                "ON d.task_uid=t.dependency_uid WHERE t.status IN ('pending','retry_wait','deferred') "
                "AND t.available_at<=? AND (t.dependency_uid IS NULL OR d.status='succeeded') "
                "AND (t.kind<>'refine' OR json_extract(t.payload_json,'$.refiner_identity')=?) "
                "ORDER BY t.created_at,t.task_uid LIMIT 1",
                (now, refiner_identity),
            ).fetchone()
            if row:
                self.connection.execute(
                    "UPDATE processing_task SET status='running',attempts=attempts+1,lease_until=?,owner=?,updated_at=? WHERE task_uid=?",
                    (now + lease_seconds, owner, utc_now(), row["task_uid"]),
                )
            self.connection.commit()
            return (
                {**dict(row), "owner": owner, "payload": json.loads(row["payload_json"]), "attempts": row["attempts"] + 1} if row else None
            )
        except BaseException:
            self.connection.rollback()
            raise

    def finish(self, task, status, result=None, *, retry_after=0, error_type=None):
        with self.connection:
            changed = self.connection.execute(
                "UPDATE processing_task SET status=?,result_json=?,error_type=?,"
                "available_at=?,lease_until=NULL,owner=NULL,updated_at=? "
                "WHERE task_uid=? AND status='running' AND owner=?",
                (
                    status,
                    json.dumps(result or {}, ensure_ascii=False),
                    error_type,
                    time.time() + retry_after,
                    utc_now(),
                    task["task_uid"],
                    task["owner"],
                ),
            ).rowcount
            if not changed:
                raise RuntimeError("task lease ownership changed")
            if status == "deferred":
                self.connection.execute("UPDATE processing_task SET attempts=max(0,attempts-1) WHERE task_uid=?", (task["task_uid"],))

    def edge(self, child, parent, relation="derived_from"):
        cycle = self.connection.execute(
            "WITH RECURSIVE ancestors(uid) AS (SELECT parent_uid FROM artifact_edge "
            "WHERE child_uid=? UNION SELECT e.parent_uid FROM artifact_edge e JOIN ancestors a "
            "ON e.child_uid=a.uid) SELECT 1 FROM ancestors WHERE uid=?",
            (parent, child),
        ).fetchone()
        if child == parent or cycle:
            raise ValueError("artifact edge would create a cycle")
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO artifact_edge VALUES(?,?,?)", (child, parent, relation))

    def counts(self):
        return {row[0]: row[1] for row in self.connection.execute("SELECT status,count(*) FROM processing_task GROUP BY status")}

    def active_counts(self, refiner_identity):
        return {
            row[0]: row[1]
            for row in self.connection.execute(
                "SELECT status,count(*) FROM processing_task WHERE kind<>'refine' "
                "OR json_extract(payload_json,'$.refiner_identity')=? GROUP BY status",
                (refiner_identity,),
            )
        }
