"""SQLite state store for batches, steps, assets and QC results.

SQLite is used as the local implementation behind a small repository-style
interface.  The tables are intentionally close to the future PostgreSQL model
so the storage implementation can be replaced without changing collection
business logic.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.context import PipelineContext
from core.ids import artifact_uid, stable_uid
from core.logging import get_logger, log_event


LOGGER = get_logger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_mapping(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _status_log_level(status: str) -> str:
    if status == "failed":
        return "ERROR"
    if status in {"partial", "needs_review"}:
        return "WARNING"
    return "INFO"


class StateStore:
    """Transactional SQLite repository for pipeline lineage."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self._create_schema()
        log_event(LOGGER, "DEBUG", "state_store_opened", state_path=str(self.path))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS pipeline_batch (
                batch_id TEXT PRIMARY KEY,
                pipeline_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                status TEXT NOT NULL,
                last_run_id TEXT,
                config_version TEXT,
                rule_version TEXT,
                model_version TEXT,
                error_msg TEXT
            );

            CREATE TABLE IF NOT EXISTS pipeline_run (
                run_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                dry_run INTEGER NOT NULL DEFAULT 0,
                config_version TEXT,
                rule_version TEXT,
                model_version TEXT,
                error_msg TEXT
            );

            CREATE TABLE IF NOT EXISTS pipeline_step (
                step_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES pipeline_run(run_id),
                batch_id TEXT NOT NULL,
                entity_uid TEXT,
                step_name TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                error_msg TEXT,
                metadata_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_pipeline_step_run
                ON pipeline_step(run_id, step_name, entity_uid);

            CREATE TABLE IF NOT EXISTS report_asset (
                asset_uid TEXT PRIMARY KEY,
                report_uid TEXT NOT NULL,
                candidate_uid TEXT,
                source_name TEXT,
                source_id TEXT,
                source_url TEXT NOT NULL,
                raw_file_hash TEXT NOT NULL,
                raw_path TEXT,
                file_size INTEGER,
                page_count INTEGER,
                status TEXT NOT NULL,
                first_seen_run_id TEXT,
                last_seen_run_id TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_report_asset_report
                ON report_asset(report_uid, status);

            CREATE TABLE IF NOT EXISTS scholarly_work (
                work_uid TEXT PRIMARY KEY,
                doi TEXT,
                title TEXT NOT NULL,
                work_type TEXT NOT NULL,
                journal_title TEXT,
                journal_issn TEXT,
                published_date TEXT,
                source_updated_at TEXT,
                metadata_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                first_seen_run_id TEXT NOT NULL,
                last_seen_run_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_scholarly_work_doi
                ON scholarly_work(doi);

            CREATE TABLE IF NOT EXISTS scholarly_source_record (
                candidate_uid TEXT PRIMARY KEY,
                work_uid TEXT NOT NULL REFERENCES scholarly_work(work_uid),
                source_name TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                pdf_url TEXT,
                source_priority INTEGER NOT NULL,
                metadata_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                first_seen_run_id TEXT NOT NULL,
                last_seen_run_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_name, source_id)
            );

            CREATE INDEX IF NOT EXISTS idx_scholarly_source_work
                ON scholarly_source_record(work_uid, source_name);

            CREATE TABLE IF NOT EXISTS scholarly_asset (
                asset_uid TEXT PRIMARY KEY,
                work_uid TEXT NOT NULL,
                candidate_uid TEXT,
                source_name TEXT,
                source_id TEXT,
                source_url TEXT NOT NULL,
                raw_file_hash TEXT NOT NULL,
                raw_path TEXT,
                file_size INTEGER,
                page_count INTEGER,
                status TEXT NOT NULL,
                first_seen_run_id TEXT NOT NULL,
                last_seen_run_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_scholarly_asset_work
                ON scholarly_asset(work_uid, status);

            CREATE TABLE IF NOT EXISTS source_sync_state (
                source_name TEXT NOT NULL,
                target_uid TEXT NOT NULL,
                last_success_at TEXT,
                cursor TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(source_name, target_uid)
            );

            CREATE TABLE IF NOT EXISTS qc_result (
                qc_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES pipeline_run(run_id),
                batch_id TEXT NOT NULL,
                entity_uid TEXT NOT NULL,
                qc_stage TEXT NOT NULL,
                check_name TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT,
                value_json TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, entity_uid, qc_stage, check_name)
            );

            CREATE TABLE IF NOT EXISTS document_registry (
                document_uid TEXT PRIMARY KEY,
                document_type TEXT NOT NULL,
                title TEXT,
                source_name TEXT,
                source_id TEXT,
                metadata_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                status TEXT NOT NULL,
                first_seen_run_id TEXT,
                last_seen_run_id TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_document_registry_type
                ON document_registry(document_type, status);

            CREATE TABLE IF NOT EXISTS financial_document_metadata (
                asset_uid TEXT PRIMARY KEY,
                document_uid TEXT NOT NULL,
                metadata_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                status TEXT NOT NULL,
                first_seen_run_id TEXT NOT NULL,
                last_seen_run_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_financial_document_metadata_document
                ON financial_document_metadata(document_uid, status);

            CREATE TABLE IF NOT EXISTS financial_metadata_override (
                asset_uid TEXT PRIMARY KEY,
                document_uid TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                overrides_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS financial_metadata_override_audit (
                audit_uid TEXT PRIMARY KEY,
                asset_uid TEXT NOT NULL,
                document_uid TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                field_changes_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_financial_metadata_override_audit_asset
                ON financial_metadata_override_audit(asset_uid, created_at);

            CREATE TABLE IF NOT EXISTS financial_report_version (
                asset_uid TEXT PRIMARY KEY,
                report_group_uid TEXT NOT NULL,
                document_uid TEXT NOT NULL,
                metadata_hash TEXT NOT NULL,
                version_status TEXT NOT NULL,
                selection_reason TEXT,
                selected_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_financial_report_version_group
                ON financial_report_version(report_group_uid, version_status);

            CREATE TABLE IF NOT EXISTS financial_report_version_selection_audit (
                audit_uid TEXT PRIMARY KEY,
                report_group_uid TEXT NOT NULL,
                selected_asset_uid TEXT NOT NULL,
                previous_asset_uid TEXT,
                batch_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_financial_report_version_selection_audit_group
                ON financial_report_version_selection_audit(report_group_uid, created_at);

            CREATE TABLE IF NOT EXISTS ocr_quality (
                asset_uid TEXT PRIMARY KEY,
                document_uid TEXT NOT NULL,
                backend TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                checks_json TEXT NOT NULL,
                status TEXT NOT NULL,
                first_seen_run_id TEXT NOT NULL,
                last_seen_run_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_ocr_quality_status
                ON ocr_quality(status, updated_at);

            CREATE TABLE IF NOT EXISTS governed_document (
                governed_uid TEXT PRIMARY KEY,
                document_uid TEXT NOT NULL,
                work_uid TEXT NOT NULL,
                asset_uid TEXT NOT NULL,
                backend TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES pipeline_run(run_id),
                batch_id TEXT NOT NULL,
                rule_version TEXT NOT NULL,
                input_path TEXT NOT NULL,
                output_path TEXT NOT NULL,
                status TEXT NOT NULL,
                block_count INTEGER NOT NULL,
                char_count INTEGER NOT NULL,
                model_version TEXT,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_governed_document_work
                ON governed_document(work_uid, asset_uid, backend);

            CREATE TABLE IF NOT EXISTS document_chunk (
                chunk_uid TEXT PRIMARY KEY,
                chunk_id TEXT,
                chunk_version_uid TEXT,
                governed_uid TEXT NOT NULL REFERENCES governed_document(governed_uid),
                document_uid TEXT NOT NULL,
                work_uid TEXT NOT NULL,
                asset_uid TEXT NOT NULL,
                backend TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                page INTEGER,
                content_type TEXT NOT NULL,
                title_context TEXT NOT NULL,
                char_start INTEGER NOT NULL,
                char_end INTEGER NOT NULL,
                text_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_document_chunk_governed
                ON document_chunk(governed_uid, chunk_index);

            CREATE TABLE IF NOT EXISTS artifact (
                artifact_uid TEXT PRIMARY KEY,
                artifact_type TEXT NOT NULL,
                document_uid TEXT,
                asset_uid TEXT,
                governed_uid TEXT,
                parent_artifact_uid TEXT,
                run_id TEXT NOT NULL REFERENCES pipeline_run(run_id),
                batch_id TEXT NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT,
                status TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_artifact_document
                ON artifact(document_uid, artifact_type, status);

            CREATE INDEX IF NOT EXISTS idx_artifact_parent
                ON artifact(parent_artifact_uid);

            CREATE TABLE IF NOT EXISTS llm_governance (
                governance_uid TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES pipeline_run(run_id),
                entity_uid TEXT NOT NULL,
                stage TEXT NOT NULL,
                model TEXT NOT NULL,
                model_version TEXT,
                status TEXT NOT NULL,
                result_json TEXT,
                error_msg TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS workflow_run (
                workflow_run_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                pipeline_name TEXT NOT NULL,
                stages_json TEXT NOT NULL,
                parameters_json TEXT NOT NULL DEFAULT '{}',
                resume_from_run_id TEXT,
                status TEXT NOT NULL,
                dry_run INTEGER NOT NULL DEFAULT 0,
                error_msg TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_workflow_run_batch
                ON workflow_run(batch_id, started_at DESC);

            CREATE TABLE IF NOT EXISTS workflow_stage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_run_id TEXT NOT NULL REFERENCES workflow_run(workflow_run_id),
                stage_name TEXT NOT NULL,
                stage_position INTEGER NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                output_json TEXT,
                error_msg TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                UNIQUE(workflow_run_id, stage_name)
            );

            CREATE INDEX IF NOT EXISTS idx_workflow_stage_run
                ON workflow_stage(workflow_run_id, stage_position);
            """
        )
        self._ensure_column("pipeline_run", "attempt", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("document_chunk", "chunk_id", "TEXT")
        self._ensure_column("document_chunk", "chunk_version_uid", "TEXT")
        self._ensure_column("workflow_run", "parameters_json", "TEXT")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_document_chunk_logical "
            "ON document_chunk(document_uid, chunk_id)"
        )
        self.connection.commit()

    def _ensure_column(self, table_name: str, column_name: str, definition: str) -> None:
        """Apply the small additive migrations needed by local state databases."""

        columns = self.connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        if any(str(column[1]) == column_name for column in columns):
            return
        self.connection.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
        )

    def start_run(self, context: PipelineContext, *, dry_run: bool) -> None:
        now = utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO pipeline_batch (
                    batch_id, pipeline_name, created_at, started_at, status,
                    last_run_id, config_version, rule_version, model_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(batch_id) DO UPDATE SET
                    started_at = excluded.started_at,
                    finished_at = NULL,
                    status = excluded.status,
                    last_run_id = excluded.last_run_id,
                    config_version = excluded.config_version,
                    rule_version = excluded.rule_version,
                    model_version = excluded.model_version,
                    error_msg = NULL
                """,
                (
                    context.batch_id,
                    "fin-doc-governance",
                    now,
                    context.started_at,
                    "running",
                    context.run_id,
                    context.config_version,
                    context.rule_version,
                    context.model_version,
                ),
            )
            previous = self.connection.execute(
                "SELECT COALESCE(MAX(attempt), 0) AS attempt FROM pipeline_run WHERE batch_id = ?",
                (context.batch_id,),
            ).fetchone()
            attempt = int(previous["attempt"]) + 1
            self.connection.execute(
                """
                INSERT INTO pipeline_run (
                    run_id, batch_id, attempt, started_at, status, dry_run,
                    config_version, rule_version, model_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    context.run_id,
                    context.batch_id,
                    attempt,
                    context.started_at,
                    "running",
                    int(dry_run),
                    context.config_version,
                    context.rule_version,
                    context.model_version,
                ),
            )
        log_event(
            LOGGER,
            "INFO",
            "pipeline_run_started",
            batch_id=context.batch_id,
            run_id=context.run_id,
            dry_run=dry_run,
            config_version=context.config_version,
            rule_version=context.rule_version,
            model_version=context.model_version,
        )

    def finish_run(self, run_id: str, status: str, error_msg: str | None = None) -> None:
        finished_at = utc_now()
        with self.connection:
            row = self.connection.execute(
                "SELECT batch_id FROM pipeline_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            self.connection.execute(
                "UPDATE pipeline_run SET finished_at = ?, status = ?, error_msg = ? WHERE run_id = ?",
                (finished_at, status, error_msg, run_id),
            )
            if row:
                self.connection.execute(
                    """
                    UPDATE pipeline_batch
                    SET finished_at = ?, status = ?, last_run_id = ?, error_msg = ?
                    WHERE batch_id = ?
                    """,
                    (finished_at, status, run_id, error_msg, row["batch_id"]),
                )
        log_event(
            LOGGER,
            _status_log_level(status),
            "pipeline_run_finished",
            batch_id=row["batch_id"] if row else None,
            run_id=run_id,
            status=status,
            error_msg=error_msg,
        )

    def start_step(
        self,
        context: PipelineContext,
        step_name: str,
        entity_uid: str | None = None,
        *,
        attempt: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        if attempt is None:
            previous = self.connection.execute(
                """
                SELECT COALESCE(MAX(attempt), 0) AS attempt
                FROM pipeline_step
                WHERE batch_id = ? AND step_name = ?
                  AND (entity_uid = ? OR (entity_uid IS NULL AND ? IS NULL))
                """,
                (context.batch_id, step_name, entity_uid, entity_uid),
            ).fetchone()
            attempt = int(previous["attempt"]) + 1
        cursor = self.connection.execute(
            """
            INSERT INTO pipeline_step (
                run_id, batch_id, entity_uid, step_name, attempt,
                started_at, status, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                context.run_id,
                context.batch_id,
                entity_uid,
                step_name,
                int(attempt),
                utc_now(),
                "running",
                json.dumps(dict(metadata or {}), ensure_ascii=False, sort_keys=True),
            ),
        )
        self.connection.commit()
        step_id = int(cursor.lastrowid)
        log_event(
            LOGGER,
            "INFO",
            "pipeline_step_started",
            batch_id=context.batch_id,
            run_id=context.run_id,
            step_id=step_id,
            step_name=step_name,
            entity_uid=entity_uid,
            attempt=attempt,
        )
        return step_id

    def finish_step(self, step_id: int, status: str, error_msg: str | None = None) -> None:
        row = self.connection.execute(
            """
            SELECT batch_id, entity_uid, run_id, step_name, attempt
            FROM pipeline_step WHERE step_id = ?
            """,
            (step_id,),
        ).fetchone()
        self.connection.execute(
            "UPDATE pipeline_step SET finished_at = ?, status = ?, error_msg = ? WHERE step_id = ?",
            (utc_now(), status, error_msg, step_id),
        )
        self.connection.commit()
        log_event(
            LOGGER,
            _status_log_level(status),
            "pipeline_step_finished",
            batch_id=row["batch_id"] if row else None,
            run_id=row["run_id"] if row else None,
            step_id=step_id,
            step_name=row["step_name"] if row else None,
            entity_uid=row["entity_uid"] if row else None,
            attempt=row["attempt"] if row else None,
            status=status,
            error_msg=error_msg,
        )

    def get_batch(self, batch_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM pipeline_batch WHERE batch_id = ?", (batch_id,)
        ).fetchone()

    def list_batches(self, *, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        """Return recent batches for local operational inspection."""

        limit = self._validated_limit(limit)
        query = "SELECT * FROM pipeline_batch"
        parameters: list[Any] = []
        if status:
            query += " WHERE status = ?"
            parameters.append(status)
        query += " ORDER BY created_at DESC, batch_id DESC LIMIT ?"
        parameters.append(limit)
        return [dict(row) for row in self.connection.execute(query, parameters).fetchall()]

    def backup_to(self, output_path: Path | str) -> dict[str, Any]:
        """Create a consistent, independently readable SQLite state backup."""

        destination = Path(output_path)
        if destination.exists():
            raise FileExistsError(f"state backup already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        backup_connection: sqlite3.Connection | None = None
        try:
            backup_connection = sqlite3.connect(temporary_path)
            self.connection.backup(backup_connection)
            integrity = backup_connection.execute("PRAGMA integrity_check").fetchone()[0]
            backup_connection.commit()
            backup_connection.close()
            backup_connection = None
            if integrity != "ok":
                raise RuntimeError(f"SQLite backup integrity check failed: {integrity}")
            os.replace(temporary_path, destination)
            result = {
                "status": "success",
                "backup_path": str(destination),
                "sha256": self._sha256_file(destination),
                "integrity_check": integrity,
            }
            log_event(LOGGER, "INFO", "state_backup_created", **result)
            return result
        finally:
            if backup_connection is not None:
                backup_connection.close()
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _validated_limit(value: int) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return limit

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def latest_step(
        self, batch_id: str, step_name: str, entity_uid: str | None = None
    ) -> sqlite3.Row | None:
        """Return the latest attempt for one entity within a reusable batch."""

        return self.connection.execute(
            """
            SELECT * FROM pipeline_step
            WHERE batch_id = ? AND step_name = ?
              AND (entity_uid = ? OR (entity_uid IS NULL AND ? IS NULL))
            ORDER BY attempt DESC, step_id DESC
            LIMIT 1
            """,
            (batch_id, step_name, entity_uid, entity_uid),
        ).fetchone()

    def upsert_document(
        self, context: PipelineContext, record: Mapping[str, Any]
    ) -> None:
        """Persist canonical document metadata independently of content files."""

        document_uid = str(record.get("document_uid") or "")
        if not document_uid:
            return
        metadata = dict(record.get("metadata") or {})
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        metadata_hash = str(
            record.get("metadata_hash") or stable_uid("document-metadata", metadata_json)
        )
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO document_registry (
                document_uid, document_type, title, source_name, source_id,
                metadata_hash, metadata_json, status, first_seen_run_id,
                last_seen_run_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_uid) DO UPDATE SET
                document_type = excluded.document_type,
                title = excluded.title,
                source_name = excluded.source_name,
                source_id = excluded.source_id,
                metadata_hash = excluded.metadata_hash,
                metadata_json = excluded.metadata_json,
                status = excluded.status,
                last_seen_run_id = excluded.last_seen_run_id,
                updated_at = excluded.updated_at
            """,
            (
                document_uid,
                str(record.get("document_type") or "unknown"),
                record.get("title"),
                record.get("source_name"),
                record.get("source_id"),
                metadata_hash,
                metadata_json,
                str(record.get("status") or "active"),
                context.run_id,
                context.run_id,
                now,
            ),
        )
        self.connection.commit()

    def upsert_artifact(
        self, context: PipelineContext, record: Mapping[str, Any]
    ) -> None:
        """Register one local file artifact and its parent lineage."""

        artifact_id = str(record.get("artifact_uid") or "")
        path = str(record.get("path") or "")
        if not artifact_id or not path:
            return
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO artifact (
                artifact_uid, artifact_type, document_uid, asset_uid,
                governed_uid, parent_artifact_uid, run_id, batch_id, path,
                sha256, status, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_uid) DO UPDATE SET
                document_uid = excluded.document_uid,
                asset_uid = excluded.asset_uid,
                governed_uid = excluded.governed_uid,
                parent_artifact_uid = excluded.parent_artifact_uid,
                run_id = excluded.run_id,
                batch_id = excluded.batch_id,
                path = excluded.path,
                sha256 = excluded.sha256,
                status = excluded.status,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                artifact_id,
                str(record.get("artifact_type") or "unknown"),
                record.get("document_uid"),
                record.get("asset_uid"),
                record.get("governed_uid"),
                record.get("parent_artifact_uid"),
                context.run_id,
                context.batch_id,
                path,
                record.get("sha256"),
                str(record.get("status") or "success"),
                json.dumps(dict(record.get("metadata") or {}), ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        self.connection.commit()

    def upsert_asset(self, context: PipelineContext, record: Mapping[str, Any]) -> None:
        asset_id = record.get("asset_uid")
        raw_file_hash = record.get("raw_file_hash")
        if not asset_id or not raw_file_hash:
            return
        self.connection.execute(
            """
            INSERT INTO report_asset (
                asset_uid, report_uid, candidate_uid, source_name, source_id,
                source_url, raw_file_hash, raw_path, file_size, page_count,
                status, first_seen_run_id, last_seen_run_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_uid) DO UPDATE SET
                report_uid = excluded.report_uid,
                candidate_uid = excluded.candidate_uid,
                source_name = excluded.source_name,
                source_id = excluded.source_id,
                source_url = excluded.source_url,
                raw_path = excluded.raw_path,
                file_size = excluded.file_size,
                page_count = excluded.page_count,
                status = excluded.status,
                last_seen_run_id = excluded.last_seen_run_id,
                updated_at = excluded.updated_at
            """,
            (
                asset_id,
                record.get("report_uid"),
                record.get("candidate_uid"),
                record.get("source_name"),
                record.get("source_id"),
                record.get("source_url"),
                raw_file_hash,
                record.get("raw_path"),
                record.get("file_size"),
                record.get("page_count"),
                record.get("status", "unknown"),
                context.run_id,
                context.run_id,
                utc_now(),
            ),
        )
        self.connection.commit()
        report_uid = record.get("report_uid")
        if report_uid:
            self.upsert_document(
                context,
                {
                    "document_uid": report_uid,
                    "document_type": "financial-report",
                    "title": record.get("title"),
                    "source_name": record.get("source_name"),
                    "source_id": record.get("source_id"),
                    "metadata": dict(record),
                },
            )
        raw_path = record.get("raw_path")
        if raw_path:
            self.upsert_artifact(
                context,
                {
                    "artifact_uid": artifact_uid("raw-pdf", asset_id),
                    "artifact_type": "raw-pdf",
                    "document_uid": report_uid,
                    "asset_uid": asset_id,
                    "path": raw_path,
                    "sha256": raw_file_hash,
                    "metadata": {"source_url": record.get("source_url")},
                },
            )

    def record_qc(
        self,
        context: PipelineContext,
        entity_uid: str,
        qc_stage: str,
        checks: Iterable[Mapping[str, Any]],
    ) -> None:
        for check in checks:
            check_name = str(check["check_name"])
            qc_id = stable_uid(context.run_id, entity_uid, qc_stage, check_name)
            self.connection.execute(
                """
                INSERT INTO qc_result (
                    qc_id, run_id, batch_id, entity_uid, qc_stage,
                    check_name, status, message, value_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, entity_uid, qc_stage, check_name) DO UPDATE SET
                    status = excluded.status,
                    message = excluded.message,
                    value_json = excluded.value_json,
                    created_at = excluded.created_at
                """,
                (
                    qc_id,
                    context.run_id,
                    context.batch_id,
                    entity_uid,
                    qc_stage,
                    check_name,
                    check.get("status", "unknown"),
                    check.get("message"),
                    json.dumps(check.get("value"), ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )
        self.connection.commit()

    def upsert_scholarly_candidate(
        self, context: PipelineContext, record: Mapping[str, Any]
    ) -> str:
        """Upsert one scholarly source record and return new/changed/unchanged."""

        candidate_uid = str(record.get("candidate_uid") or "")
        work_uid = str(record.get("work_uid") or "")
        source_name = str(record.get("source_name") or "")
        source_id = str(record.get("source_id") or "")
        metadata_hash = str(record.get("metadata_hash") or "")
        if not all((candidate_uid, work_uid, source_name, source_id, metadata_hash)):
            raise ValueError("scholarly candidate is missing a stable ID or metadata_hash")

        previous = self.connection.execute(
            "SELECT metadata_hash FROM scholarly_source_record WHERE candidate_uid = ?",
            (candidate_uid,),
        ).fetchone()
        if previous is None:
            change_status = "new"
        elif previous["metadata_hash"] == metadata_hash:
            change_status = "unchanged"
        else:
            change_status = "changed"

        now = utc_now()
        metadata_json = json.dumps(dict(record), ensure_ascii=False, sort_keys=True)
        self.connection.execute(
            """
            INSERT INTO scholarly_work (
                work_uid, doi, title, work_type, journal_title, journal_issn,
                published_date, source_updated_at, metadata_hash, metadata_json,
                first_seen_run_id, last_seen_run_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(work_uid) DO UPDATE SET
                doi = excluded.doi,
                title = excluded.title,
                work_type = excluded.work_type,
                journal_title = excluded.journal_title,
                journal_issn = excluded.journal_issn,
                published_date = excluded.published_date,
                source_updated_at = excluded.source_updated_at,
                metadata_hash = excluded.metadata_hash,
                metadata_json = excluded.metadata_json,
                last_seen_run_id = excluded.last_seen_run_id,
                updated_at = excluded.updated_at
            """,
            (
                work_uid,
                record.get("doi"),
                record.get("title", ""),
                record.get("work_type", "unknown"),
                record.get("journal_title"),
                record.get("journal_issn"),
                record.get("published_date"),
                record.get("source_updated_at"),
                metadata_hash,
                metadata_json,
                context.run_id,
                context.run_id,
                now,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO scholarly_source_record (
                candidate_uid, work_uid, source_name, source_id, source_url,
                pdf_url, source_priority, metadata_hash, metadata_json,
                first_seen_run_id, last_seen_run_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_uid) DO UPDATE SET
                work_uid = excluded.work_uid,
                source_name = excluded.source_name,
                source_id = excluded.source_id,
                source_url = excluded.source_url,
                pdf_url = excluded.pdf_url,
                source_priority = excluded.source_priority,
                metadata_hash = excluded.metadata_hash,
                metadata_json = excluded.metadata_json,
                last_seen_run_id = excluded.last_seen_run_id,
                updated_at = excluded.updated_at
            """,
            (
                candidate_uid,
                work_uid,
                source_name,
                source_id,
                record.get("source_url", ""),
                record.get("pdf_url"),
                int(record.get("source_priority", 100)),
                metadata_hash,
                metadata_json,
                context.run_id,
                context.run_id,
                now,
            ),
        )
        self.connection.commit()
        self.upsert_document(
            context,
            {
                "document_uid": work_uid,
                "document_type": "scholarly-work",
                "title": record.get("title"),
                "source_name": source_name,
                "source_id": source_id,
                "metadata_hash": metadata_hash,
                "metadata": dict(record),
            },
        )
        return change_status

    def get_sync_state(self, source_name: str, target_uid: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT source_name, target_uid, last_success_at, cursor, updated_at
            FROM source_sync_state
            WHERE source_name = ? AND target_uid = ?
            """,
            (source_name, target_uid),
        ).fetchone()

    def upsert_sync_state(
        self,
        source_name: str,
        target_uid: str,
        *,
        last_success_at: str,
        cursor: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO source_sync_state (
                source_name, target_uid, last_success_at, cursor, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_name, target_uid) DO UPDATE SET
                last_success_at = excluded.last_success_at,
                cursor = excluded.cursor,
                updated_at = excluded.updated_at
            """,
            (source_name, target_uid, last_success_at, cursor, utc_now()),
        )
        self.connection.commit()

    def upsert_scholarly_asset(
        self, context: PipelineContext, record: Mapping[str, Any]
    ) -> None:
        """Persist a downloaded scholarly PDF asset and its lineage."""

        asset_uid = record.get("asset_uid")
        raw_file_hash = record.get("raw_file_hash")
        work_uid = record.get("work_uid")
        if not asset_uid or not raw_file_hash or not work_uid:
            return
        self.connection.execute(
            """
            INSERT INTO scholarly_asset (
                asset_uid, work_uid, candidate_uid, source_name, source_id,
                source_url, raw_file_hash, raw_path, file_size, page_count,
                status, first_seen_run_id, last_seen_run_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_uid) DO UPDATE SET
                work_uid = excluded.work_uid,
                candidate_uid = excluded.candidate_uid,
                source_name = excluded.source_name,
                source_id = excluded.source_id,
                source_url = excluded.source_url,
                raw_file_hash = excluded.raw_file_hash,
                raw_path = excluded.raw_path,
                file_size = excluded.file_size,
                page_count = excluded.page_count,
                status = excluded.status,
                last_seen_run_id = excluded.last_seen_run_id,
                updated_at = excluded.updated_at
            """,
            (
                asset_uid,
                work_uid,
                record.get("candidate_uid"),
                record.get("source_name"),
                record.get("source_id"),
                record.get("source_url", ""),
                raw_file_hash,
                record.get("raw_path"),
                record.get("file_size"),
                record.get("page_count"),
                record.get("status", "unknown"),
                context.run_id,
                context.run_id,
                utc_now(),
            ),
        )
        self.connection.commit()
        raw_path = record.get("raw_path")
        if raw_path:
            self.upsert_artifact(
                context,
                {
                    "artifact_uid": artifact_uid("raw-pdf", asset_uid),
                    "artifact_type": "raw-pdf",
                    "document_uid": work_uid,
                    "asset_uid": asset_uid,
                    "path": raw_path,
                    "sha256": raw_file_hash,
                    "metadata": {"source_url": record.get("source_url")},
                },
            )

    def upsert_governed_document(
        self, context: PipelineContext, record: Mapping[str, Any]
    ) -> None:
        """Persist one governed document and its cleaning lineage."""

        governed_uid = record.get("governed_uid")
        if not governed_uid:
            return
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO governed_document (
                governed_uid, document_uid, work_uid, asset_uid, backend,
                run_id, batch_id, rule_version, input_path, output_path,
                status, block_count, char_count, model_version,
                metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(governed_uid) DO UPDATE SET
                document_uid = excluded.document_uid,
                work_uid = excluded.work_uid,
                asset_uid = excluded.asset_uid,
                backend = excluded.backend,
                run_id = excluded.run_id,
                batch_id = excluded.batch_id,
                rule_version = excluded.rule_version,
                input_path = excluded.input_path,
                output_path = excluded.output_path,
                status = excluded.status,
                block_count = excluded.block_count,
                char_count = excluded.char_count,
                model_version = excluded.model_version,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                governed_uid,
                record.get("document_uid", ""),
                record.get("work_uid", ""),
                record.get("asset_uid", ""),
                record.get("backend", ""),
                context.run_id,
                context.batch_id,
                record.get("rule_version", ""),
                record.get("input_path", ""),
                record.get("output_path", ""),
                record.get("status", "success"),
                int(record.get("block_count", 0)),
                int(record.get("char_count", 0)),
                record.get("model_version"),
                json.dumps(
                    {
                        "stats": record.get("stats"),
                        "cleaning_steps": record.get("cleaning_steps"),
                        "document_type": record.get("document_type"),
                        "title": record.get("title"),
                        "source_name": record.get("source_name"),
                        "source_id": record.get("source_id"),
                        "candidate_uid": record.get("candidate_uid"),
                        "raw_path": record.get("raw_path"),
                        "named_path": record.get("named_path"),
                        "pdf_url": record.get("pdf_url"),
                        "chunks_path": record.get("chunks_path"),
                        "llm_path": record.get("llm_path"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
                now,
            ),
        )
        self.connection.commit()

    def replace_document_chunks(
        self, context: PipelineContext, governed_uid: str, chunks: Iterable[Mapping[str, Any]]
    ) -> None:
        """Replace all chunks for a governed document in one transaction."""

        with self.connection:
            self.connection.execute(
                "DELETE FROM document_chunk WHERE governed_uid = ?", (governed_uid,)
            )
            for chunk in chunks:
                self.connection.execute(
                    """
                    INSERT INTO document_chunk (
                        chunk_uid, chunk_id, chunk_version_uid, governed_uid,
                        document_uid, work_uid, asset_uid, backend, chunk_index,
                        page, content_type, title_context, char_start, char_end,
                        text_hash, status, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.get("chunk_uid"),
                        chunk.get("chunk_id") or chunk.get("chunk_uid"),
                        chunk.get("chunk_version_uid") or chunk.get("chunk_uid"),
                        governed_uid,
                        chunk.get("document_uid", ""),
                        chunk.get("work_uid", ""),
                        chunk.get("asset_uid", ""),
                        chunk.get("backend", ""),
                        int(chunk.get("chunk_index", 0)),
                        chunk.get("page"),
                        chunk.get("content_type", ""),
                        chunk.get("title_context", ""),
                        int(chunk.get("char_start", 0)),
                        int(chunk.get("char_end", 0)),
                        chunk.get("text_hash", ""),
                        chunk.get("status", "success"),
                        json.dumps(
                            {
                                "text": chunk.get("text"),
                                "table": chunk.get("table"),
                                "table_source": chunk.get("table_source"),
                                "table_status": chunk.get("table_status"),
                                "context_text": chunk.get("context_text"),
                                "context_source": chunk.get("context_source"),
                                "context_status": chunk.get("context_status"),
                                "retrieval_text": chunk.get("retrieval_text"),
                                "llm_status": chunk.get("llm_status"),
                                "llm_enrichment_uid": chunk.get("llm_enrichment_uid"),
                                "llm_model": chunk.get("llm_model"),
                                "llm_model_version": chunk.get("llm_model_version"),
                                "llm_prompt_version": chunk.get("llm_prompt_version"),
                                "llm_error": chunk.get("llm_error"),
                                "pages": chunk.get("pages"),
                                "bboxes": chunk.get("bboxes"),
                                "start_block": chunk.get("start_block"),
                                "end_block": chunk.get("end_block"),
                                "block_count": chunk.get("block_count"),
                                "run_id": context.run_id,
                                "batch_id": context.batch_id,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        utc_now(),
                    ),
                )

    def record_llm_governance(
        self,
        context: PipelineContext,
        entity_uid: str,
        stage: str,
        model: str,
        *,
        status: str,
        result: Mapping[str, Any] | None = None,
        error_msg: str | None = None,
    ) -> None:
        governance_uid = stable_uid(context.run_id, entity_uid, stage, model)
        self.connection.execute(
            """
            INSERT INTO llm_governance (
                governance_uid, run_id, entity_uid, stage, model,
                model_version, status, result_json, error_msg, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(governance_uid) DO UPDATE SET
                status = excluded.status,
                result_json = excluded.result_json,
                error_msg = excluded.error_msg,
                created_at = excluded.created_at
            """,
            (
                governance_uid,
                context.run_id,
                entity_uid,
                stage,
                model,
                (result or {}).get("model_version") if result else None,
                status,
                json.dumps(dict(result or {}), ensure_ascii=False, sort_keys=True)
                if result
                else None,
                error_msg,
                utc_now(),
            ),
        )
        self.connection.commit()

    def upsert_financial_metadata(
        self, context: PipelineContext, record: Mapping[str, Any]
    ) -> None:
        """Persist normalized financial metadata for one immutable PDF asset."""

        asset_uid = str(record.get("asset_uid") or "")
        document_uid = str(record.get("document_uid") or "")
        if not asset_uid or not document_uid:
            return
        metadata_json = json.dumps(dict(record), ensure_ascii=False, sort_keys=True)
        metadata_hash = str(
            record.get("metadata_hash")
            or stable_uid("financial-document-metadata", metadata_json)
        )
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO financial_document_metadata (
                asset_uid, document_uid, metadata_hash, metadata_json, status,
                first_seen_run_id, last_seen_run_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_uid) DO UPDATE SET
                document_uid = excluded.document_uid,
                metadata_hash = excluded.metadata_hash,
                metadata_json = excluded.metadata_json,
                status = excluded.status,
                last_seen_run_id = excluded.last_seen_run_id,
                updated_at = excluded.updated_at
            """,
            (
                asset_uid,
                document_uid,
                metadata_hash,
                metadata_json,
                str(record.get("status") or "active"),
                context.run_id,
                context.run_id,
                now,
            ),
        )
        self.connection.commit()

    def upsert_ocr_quality(self, context: PipelineContext, record: Mapping[str, Any]) -> None:
        """Persist non-blocking OCR quality metrics for one immutable PDF asset."""

        asset_uid = str(record.get("asset_uid") or "")
        document_uid = str(record.get("document_uid") or "")
        backend = str(record.get("ocr_backend") or "")
        input_hash = str(record.get("ocr_input_hash") or "")
        if not asset_uid or not document_uid or not backend or not input_hash:
            return
        self.connection.execute(
            """
            INSERT INTO ocr_quality (
                asset_uid, document_uid, backend, input_hash, metrics_json, checks_json,
                status, first_seen_run_id, last_seen_run_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_uid) DO UPDATE SET
                document_uid = excluded.document_uid,
                backend = excluded.backend,
                input_hash = excluded.input_hash,
                metrics_json = excluded.metrics_json,
                checks_json = excluded.checks_json,
                status = excluded.status,
                last_seen_run_id = excluded.last_seen_run_id,
                updated_at = excluded.updated_at
            """,
            (
                asset_uid,
                document_uid,
                backend,
                input_hash,
                json.dumps(dict(record.get("metrics") or {}), ensure_ascii=False, sort_keys=True),
                json.dumps(list(record.get("qc_checks") or ()), ensure_ascii=False, sort_keys=True),
                str(record.get("status") or "needs_review"),
                context.run_id,
                context.run_id,
                utc_now(),
            ),
        )
        self.connection.commit()

    def list_financial_metadata_overrides(self) -> dict[str, dict[str, Any]]:
        """Return the latest manual metadata override for each immutable asset."""

        rows = self.connection.execute(
            """
            SELECT asset_uid, document_uid, batch_id, overrides_json, reason, created_at, updated_at
            FROM financial_metadata_override
            ORDER BY asset_uid
            """
        ).fetchall()
        overrides: dict[str, dict[str, Any]] = {}
        for row in rows:
            record = dict(row)
            record["overrides"] = _json_mapping(record.pop("overrides_json"))
            overrides[str(record["asset_uid"])] = record
        return overrides

    def record_financial_metadata_override(
        self,
        *,
        asset_uid: str,
        document_uid: str,
        batch_id: str,
        overrides: Mapping[str, Any],
        reason: str,
        field_changes: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Upsert a current override and append an immutable audit event together."""

        now = utc_now()
        override_values = dict(overrides)
        change_values = [dict(change) for change in field_changes]
        audit_uid = stable_uid(
            "financial-metadata-override-audit", asset_uid, now, uuid.uuid4().hex
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO financial_metadata_override (
                    asset_uid, document_uid, batch_id, overrides_json, reason,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_uid) DO UPDATE SET
                    document_uid = excluded.document_uid,
                    batch_id = excluded.batch_id,
                    overrides_json = excluded.overrides_json,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (
                    asset_uid,
                    document_uid,
                    batch_id,
                    json.dumps(override_values, ensure_ascii=False, sort_keys=True),
                    reason,
                    now,
                    now,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO financial_metadata_override_audit (
                    audit_uid, asset_uid, document_uid, batch_id,
                    field_changes_json, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_uid,
                    asset_uid,
                    document_uid,
                    batch_id,
                    json.dumps(change_values, ensure_ascii=False, sort_keys=True),
                    reason,
                    now,
                ),
            )
        return {
            "audit_uid": audit_uid,
            "asset_uid": asset_uid,
            "document_uid": document_uid,
            "batch_id": batch_id,
            "overrides": override_values,
            "reason": reason,
            "field_changes": change_values,
            "created_at": now,
        }

    def list_financial_metadata_override_audits(
        self, asset_uids: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return audit events, optionally narrowed to a delivery's assets."""

        requested_assets = [str(asset_uid) for asset_uid in asset_uids or ()]
        if asset_uids is not None and not requested_assets:
            return []
        if requested_assets:
            placeholders = ", ".join("?" for _asset_uid in requested_assets)
            rows = self.connection.execute(
                f"""
                SELECT * FROM financial_metadata_override_audit
                WHERE asset_uid IN ({placeholders})
                ORDER BY created_at, audit_uid
                """,
                requested_assets,
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT * FROM financial_metadata_override_audit
                ORDER BY created_at, audit_uid
                """
            ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            try:
                parsed_changes = json.loads(record.pop("field_changes_json"))
            except json.JSONDecodeError:
                parsed_changes = []
            record["field_changes"] = (
                parsed_changes if isinstance(parsed_changes, list) else []
            )
            records.append(record)
        return records

    def sync_financial_report_versions(
        self, records: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Assign incoming assets to report-version groups while preserving selections."""

        prepared = [
            {
                "asset_uid": str(record.get("asset_uid") or ""),
                "report_group_uid": str(record.get("report_group_uid") or ""),
                "document_uid": str(record.get("document_uid") or ""),
                "metadata_hash": str(record.get("metadata_hash") or ""),
            }
            for record in records
            if record.get("asset_uid") and record.get("report_group_uid") and record.get("document_uid")
        ]
        if not prepared:
            return []
        affected_groups: set[str] = set()
        now = utc_now()
        with self.connection:
            for record in prepared:
                previous = self.connection.execute(
                    """
                    SELECT report_group_uid, version_status
                    FROM financial_report_version
                    WHERE asset_uid = ?
                    """,
                    (record["asset_uid"],),
                ).fetchone()
                changed_group = (
                    previous is not None
                    and previous["report_group_uid"] != record["report_group_uid"]
                )
                status = "candidate" if previous is None or changed_group else previous["version_status"]
                self.connection.execute(
                    """
                    INSERT INTO financial_report_version (
                        asset_uid, report_group_uid, document_uid, metadata_hash,
                        version_status, selection_reason, selected_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                    ON CONFLICT(asset_uid) DO UPDATE SET
                        report_group_uid = excluded.report_group_uid,
                        document_uid = excluded.document_uid,
                        metadata_hash = excluded.metadata_hash,
                        version_status = excluded.version_status,
                        selection_reason = CASE
                            WHEN financial_report_version.report_group_uid = excluded.report_group_uid
                            THEN financial_report_version.selection_reason
                            ELSE NULL
                        END,
                        selected_at = CASE
                            WHEN financial_report_version.report_group_uid = excluded.report_group_uid
                            THEN financial_report_version.selected_at
                            ELSE NULL
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (
                        record["asset_uid"],
                        record["report_group_uid"],
                        record["document_uid"],
                        record["metadata_hash"],
                        status,
                        now,
                        now,
                    ),
                )
                affected_groups.add(record["report_group_uid"])
                if previous is not None:
                    affected_groups.add(str(previous["report_group_uid"]))

            for group_uid in affected_groups:
                group_rows = self.connection.execute(
                    """
                    SELECT asset_uid, version_status, created_at
                    FROM financial_report_version
                    WHERE report_group_uid = ?
                    ORDER BY created_at, asset_uid
                    """,
                    (group_uid,),
                ).fetchall()
                active_rows = [row for row in group_rows if row["version_status"] == "active"]
                if active_rows:
                    selected_asset_uid = str(active_rows[0]["asset_uid"])
                    for row in active_rows[1:]:
                        self.connection.execute(
                            """
                            UPDATE financial_report_version
                            SET version_status = 'candidate', selection_reason = NULL,
                                selected_at = NULL, updated_at = ?
                            WHERE asset_uid = ?
                            """,
                            (now, row["asset_uid"]),
                        )
                elif group_rows:
                    selected_asset_uid = str(group_rows[0]["asset_uid"])
                    self.connection.execute(
                        """
                        UPDATE financial_report_version
                        SET version_status = 'active', selection_reason = 'automatic_first_seen',
                            selected_at = ?, updated_at = ?
                        WHERE asset_uid = ?
                        """,
                        (now, now, selected_asset_uid),
                    )
                else:
                    continue
                self.connection.execute(
                    """
                    UPDATE financial_report_version
                    SET version_status = 'candidate', selection_reason = NULL,
                        selected_at = NULL, updated_at = ?
                    WHERE report_group_uid = ? AND asset_uid != ? AND version_status = 'active'
                    """,
                    (now, group_uid, selected_asset_uid),
                )
        return self.list_financial_report_versions()

    def list_financial_report_versions(
        self, asset_uids: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return report-version state, optionally narrowed to delivery assets."""

        requested_assets = [str(asset_uid) for asset_uid in asset_uids or ()]
        if asset_uids is not None and not requested_assets:
            return []
        if requested_assets:
            placeholders = ", ".join("?" for _asset_uid in requested_assets)
            rows = self.connection.execute(
                f"""
                SELECT * FROM financial_report_version
                WHERE asset_uid IN ({placeholders})
                ORDER BY report_group_uid, created_at, asset_uid
                """,
                requested_assets,
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT * FROM financial_report_version
                ORDER BY report_group_uid, created_at, asset_uid
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def select_financial_report_version(
        self,
        *,
        report_group_uid: str,
        asset_uid: str,
        batch_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Mark one reviewed asset active and audit the version-selection decision."""

        now = utc_now()
        with self.connection:
            selected = self.connection.execute(
                """
                SELECT asset_uid FROM financial_report_version
                WHERE report_group_uid = ? AND asset_uid = ?
                """,
                (report_group_uid, asset_uid),
            ).fetchone()
            if selected is None:
                raise ValueError("selected asset is not part of the report-version group")
            previous = self.connection.execute(
                """
                SELECT asset_uid FROM financial_report_version
                WHERE report_group_uid = ? AND version_status = 'active'
                ORDER BY selected_at, asset_uid
                LIMIT 1
                """,
                (report_group_uid,),
            ).fetchone()
            previous_asset_uid = str(previous["asset_uid"]) if previous else None
            self.connection.execute(
                """
                UPDATE financial_report_version
                SET version_status = 'candidate', selection_reason = NULL,
                    selected_at = NULL, updated_at = ?
                WHERE report_group_uid = ?
                """,
                (now, report_group_uid),
            )
            self.connection.execute(
                """
                UPDATE financial_report_version
                SET version_status = 'active', selection_reason = 'manual_review',
                    selected_at = ?, updated_at = ?
                WHERE report_group_uid = ? AND asset_uid = ?
                """,
                (now, now, report_group_uid, asset_uid),
            )
            audit_uid = stable_uid(
                "financial-report-version-selection", report_group_uid, asset_uid, now, uuid.uuid4().hex
            )
            self.connection.execute(
                """
                INSERT INTO financial_report_version_selection_audit (
                    audit_uid, report_group_uid, selected_asset_uid, previous_asset_uid,
                    batch_id, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (audit_uid, report_group_uid, asset_uid, previous_asset_uid, batch_id, reason, now),
            )
        return {
            "audit_uid": audit_uid,
            "report_group_uid": report_group_uid,
            "selected_asset_uid": asset_uid,
            "previous_asset_uid": previous_asset_uid,
            "batch_id": batch_id,
            "reason": reason,
            "created_at": now,
        }

    def list_financial_report_version_selection_audits(
        self, report_group_uids: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return version-selection audit events for review and delivery exports."""

        requested_groups = [str(group_uid) for group_uid in report_group_uids or ()]
        if report_group_uids is not None and not requested_groups:
            return []
        if requested_groups:
            placeholders = ", ".join("?" for _group_uid in requested_groups)
            rows = self.connection.execute(
                f"""
                SELECT * FROM financial_report_version_selection_audit
                WHERE report_group_uid IN ({placeholders})
                ORDER BY created_at, audit_uid
                """,
                requested_groups,
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT * FROM financial_report_version_selection_audit
                ORDER BY created_at, audit_uid
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def start_workflow_run(
        self,
        *,
        batch_id: str,
        pipeline_name: str,
        stages: Iterable[str],
        parameters: Mapping[str, Any] | None = None,
        dry_run: bool,
        resume_from_run_id: str | None = None,
    ) -> str:
        """Create a top-level workflow run around existing domain runs."""

        workflow_run_id = f"workflow-{uuid.uuid4().hex}"
        stage_list = list(stages)
        self.connection.execute(
            """
            INSERT INTO workflow_run (
                workflow_run_id, batch_id, pipeline_name, stages_json, parameters_json,
                resume_from_run_id, status, dry_run, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_run_id,
                batch_id,
                pipeline_name,
                json.dumps(stage_list, ensure_ascii=False),
                json.dumps(dict(parameters or {}), ensure_ascii=False, sort_keys=True),
                resume_from_run_id,
                "running",
                int(dry_run),
                utc_now(),
            ),
        )
        self.connection.commit()
        log_event(
            LOGGER,
            "INFO",
            "workflow_run_started",
            workflow_run_id=workflow_run_id,
            batch_id=batch_id,
            pipeline_name=pipeline_name,
            stage_count=len(stage_list),
            dry_run=dry_run,
            resume_from_run_id=resume_from_run_id,
        )
        return workflow_run_id

    def start_workflow_stage(
        self, workflow_run_id: str, stage_name: str, stage_position: int
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO workflow_stage (
                workflow_run_id, stage_name, stage_position, status, started_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(workflow_run_id, stage_name) DO UPDATE SET
                stage_position = excluded.stage_position,
                attempt = workflow_stage.attempt + 1,
                status = excluded.status,
                output_json = NULL,
                error_msg = NULL,
                started_at = excluded.started_at,
                finished_at = NULL
            """,
            (workflow_run_id, stage_name, stage_position, "running", utc_now()),
        )
        self.connection.commit()
        workflow = self.connection.execute(
            "SELECT batch_id, pipeline_name FROM workflow_run WHERE workflow_run_id = ?",
            (workflow_run_id,),
        ).fetchone()
        log_event(
            LOGGER,
            "INFO",
            "workflow_stage_started",
            workflow_run_id=workflow_run_id,
            batch_id=workflow["batch_id"] if workflow else None,
            pipeline_name=workflow["pipeline_name"] if workflow else None,
            stage_name=stage_name,
            stage_position=stage_position,
        )

    def finish_workflow_stage(
        self,
        workflow_run_id: str,
        stage_name: str,
        status: str,
        *,
        output: Mapping[str, Any] | None = None,
        error_msg: str | None = None,
    ) -> None:
        workflow = self.connection.execute(
            "SELECT batch_id, pipeline_name FROM workflow_run WHERE workflow_run_id = ?",
            (workflow_run_id,),
        ).fetchone()
        self.connection.execute(
            """
            UPDATE workflow_stage
            SET status = ?, output_json = ?, error_msg = ?, finished_at = ?
            WHERE workflow_run_id = ? AND stage_name = ?
            """,
            (
                status,
                json.dumps(dict(output or {}), ensure_ascii=False, sort_keys=True)
                if output is not None
                else None,
                error_msg,
                utc_now(),
                workflow_run_id,
                stage_name,
            ),
        )
        self.connection.commit()
        log_event(
            LOGGER,
            _status_log_level(status),
            "workflow_stage_finished",
            workflow_run_id=workflow_run_id,
            batch_id=workflow["batch_id"] if workflow else None,
            pipeline_name=workflow["pipeline_name"] if workflow else None,
            stage_name=stage_name,
            status=status,
            error_msg=error_msg,
        )

    def finish_workflow_run(
        self, workflow_run_id: str, status: str, error_msg: str | None = None
    ) -> None:
        workflow = self.connection.execute(
            "SELECT batch_id, pipeline_name FROM workflow_run WHERE workflow_run_id = ?",
            (workflow_run_id,),
        ).fetchone()
        self.connection.execute(
            """
            UPDATE workflow_run
            SET status = ?, error_msg = ?, finished_at = ?
            WHERE workflow_run_id = ?
            """,
            (status, error_msg, utc_now(), workflow_run_id),
        )
        self.connection.commit()
        log_event(
            LOGGER,
            _status_log_level(status),
            "workflow_run_finished",
            workflow_run_id=workflow_run_id,
            batch_id=workflow["batch_id"] if workflow else None,
            pipeline_name=workflow["pipeline_name"] if workflow else None,
            status=status,
            error_msg=error_msg,
        )

    def get_workflow_run(self, workflow_run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM workflow_run WHERE workflow_run_id = ?", (workflow_run_id,)
        ).fetchone()
        if row is None:
            return None
        return self._workflow_run_mapping(row)

    def list_workflow_runs(
        self,
        *,
        limit: int = 20,
        batch_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent workflow executions, optionally filtered by batch or status."""

        limit = self._validated_limit(limit)
        clauses: list[str] = []
        parameters: list[Any] = []
        if batch_id:
            clauses.append("batch_id = ?")
            parameters.append(batch_id)
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            "SELECT * FROM workflow_run"
            f"{where} ORDER BY started_at DESC, workflow_run_id DESC LIMIT ?",
            [*parameters, limit],
        ).fetchall()
        return [self._workflow_run_mapping(row) for row in rows]

    @staticmethod
    def _workflow_run_mapping(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        stages_value = result.pop("stages_json")
        try:
            stages = json.loads(stages_value)
        except (TypeError, json.JSONDecodeError):
            stages = []
        result["stages"] = stages if isinstance(stages, list) else []
        result["parameters"] = _json_mapping(result.pop("parameters_json", None))
        result["dry_run"] = bool(result["dry_run"])
        return result

    def get_workflow_stages(self, workflow_run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM workflow_stage
            WHERE workflow_run_id = ?
            ORDER BY stage_position, id
            """,
            (workflow_run_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            output_json = item.pop("output_json")
            item["output"] = json.loads(output_json) if output_json else None
            result.append(item)
        return result

    def fetch_one(self, query: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.connection.execute(query, parameters).fetchone()

    def count(self, table_name: str) -> int:
        allowed = {
            "pipeline_batch",
            "pipeline_run",
            "pipeline_step",
            "report_asset",
            "scholarly_work",
            "scholarly_source_record",
            "scholarly_asset",
            "source_sync_state",
            "qc_result",
            "document_registry",
            "financial_document_metadata",
            "financial_metadata_override",
            "financial_metadata_override_audit",
            "financial_report_version",
            "financial_report_version_selection_audit",
            "ocr_quality",
            "governed_document",
            "document_chunk",
            "artifact",
            "llm_governance",
            "workflow_run",
            "workflow_stage",
        }
        if table_name not in allowed:
            raise ValueError(f"unsupported table: {table_name}")
        row = self.connection.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
        return int(row["count"])
